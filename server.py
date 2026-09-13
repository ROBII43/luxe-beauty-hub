from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse, unquote, quote
from pathlib import Path
import json
import csv
from http.cookies import SimpleCookie
import secrets
from datetime import datetime, timedelta
import hashlib
import base64
import os
import hmac
import time
import io
import shutil
import httpx
import socket
import threading
import subprocess
import sys
import smtplib
from email.message import EmailMessage
import db as database
from http.server import ThreadingHTTPServer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as ReportLabImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from openpyxl import Workbook
from delivery import SHIPPING_CLASSES, calculate_delivery, delivery_settings, product_shipping_class

try:
    from PIL import Image
except ImportError:
    Image = None

ROOT = Path(__file__).parent
DATA = ROOT / 'data.json'
SESSIONS = {}
RATE_LIMIT = {}
RATE_LIMIT_LOCK = threading.Lock()
MAX_REQUEST_BYTES = 5 * 1024 * 1024
SESSION_IDLE_SECONDS = 2 * 60 * 60
SESSION_MAX_SECONDS = 8 * 60 * 60
DISPLAY_CURRENCY = 'KSh'
ADMIN_ROLES = {'ADMIN', 'SUPER_ADMIN', 'SUPERADMIN', 'STORE_MANAGER', 'SALES_STAFF', 'INVENTORY_STAFF', 'MARKETING', 'FINANCE', 'DELIVERY_STAFF'}
ROLE_PERMISSIONS = {
    'STORE_MANAGER': {'products', 'orders', 'inventory', 'customers', 'reports'},
    'SALES_STAFF': {'orders', 'customers'},
    'INVENTORY_STAFF': {'products', 'inventory'},
    'MARKETING': {'promotions', 'content'},
    'FINANCE': {'payments', 'expenses', 'reports', 'refunds'},
    'DELIVERY_STAFF': {'deliveries'},
}
LAST_SCHEDULED_BACKUP = ''
MAX_CONCURRENT_REQUESTS = max(16, int(os.environ.get('MAX_CONCURRENT_REQUESTS', '128')))

class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 19\r\nContent-Type: text/plain\r\nRetry-After: 1\r\n\r\nServer is busy.\n')
            except OSError:
                pass
            finally:
                request.close()
            return
        thread = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
        thread.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
            self.shutdown_request(request)
        except Exception:
            self.handle_error(request, client_address)
            self.shutdown_request(request)
        finally:
            self.request_slots.release()

def read_db():
    global DISPLAY_CURRENCY
    data = database.read_state(DATA)
    for role, permissions in data.get('shop', {}).get('custom_roles', {}).items():
        if role not in {'SUPER_ADMIN', 'SUPERADMIN'}:
            ROLE_PERMISSIONS[role] = set(permissions)
    DISPLAY_CURRENCY = data.get('shop', {}).get('currency', 'KSh') or 'KSh'
    if not data.get('category_hierarchy'):
        category_file = ROOT / 'categories.json'
        if category_file.exists():
            data['category_hierarchy'] = json.loads(category_file.read_text(encoding='utf-8'))
    if 'CUTE LIFESTYLE ACCESSORIES' in data.get('category_hierarchy', {}) and 'WATCHES' not in data['category_hierarchy']:
        data['category_hierarchy']['WATCHES'] = data['category_hierarchy'].pop('CUTE LIFESTYLE ACCESSORIES')
    category_aliases = {
        'Perfume': 'PERFUMES & FRAGRANCES',
        'Skincare': 'SKINCARE',
        'Home & Kitchen': 'KITCHENWARE',
        'Lifestyle': 'GIFT & LIFESTYLE ITEMS',
    }
    for product in data.get('products', []):
        product['category'] = category_aliases.get(product.get('category'), product.get('category'))
    for user in data.get('users', []):
        if user.get('password_hash') == 'scrypt$admin-demo':
            password = os.environ.get('LUXE_ADMIN_PASSWORD')
            if os.environ.get('APP_ENV') == 'production' and not password:
                raise RuntimeError('LUXE_ADMIN_PASSWORD must be configured in production')
            user['password_hash'] = hash_password(password or 'ChangeMe123!')
    ensure_legacy_admin(data)
    return data

def write_db(data):
    data = dict(data)
    data.pop('_theme', None)
    database.write_state(data)

def audit(data, session, action, description, entity='', entity_id='', old_value=None, new_value=None):
    data.setdefault('audit_logs', []).append({'user': session.get('customer_name', 'Admin'), 'action': action, 'description': description, 'entity': entity, 'entity_id': str(entity_id) if entity_id != '' else '', 'old_value': json.dumps(old_value, ensure_ascii=False, default=str) if old_value is not None else '', 'new_value': json.dumps(new_value, ensure_ascii=False, default=str) if new_value is not None else '', 'ip_address': session.get('_ip', ''), 'date': datetime.now().isoformat(timespec='seconds')})

def notify_event(data, event_type, title, message, entity='', entity_id=''):
    channels = data.setdefault('shop', {}).setdefault('notification_channels', ['dashboard', 'email'])
    data.setdefault('notifications', []).append({'id': secrets.token_hex(6), 'type': event_type, 'title': title, 'message': message, 'entity': entity, 'entity_id': str(entity_id), 'channels': channels[:], 'read': False, 'created_at': datetime.now().isoformat(timespec='seconds')})

def backup_path():
    directory = ROOT / 'backups'; directory.mkdir(exist_ok=True)
    return directory / f'luxe_beauty_backup_{datetime.now().strftime("%Y-%m-%d_%H%M%S")}.sql'

def create_backup(data):
    target = backup_path()
    state = {key: value for key, value in data.items() if not key.startswith('_')}
    payload = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
    encoded = base64.b64encode(payload.encode('utf-8')).decode('ascii')
    escaped = payload.replace('\\', '\\\\').replace("'", "''")
    sql = f"-- Luxe Beauty Hub backup generated {datetime.now().isoformat(timespec='seconds')}\n-- LUXE_STATE_JSON:{encoded}\nCREATE TABLE IF NOT EXISTS app_state (state_key VARCHAR(40) PRIMARY KEY, state_json JSON NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP);\nINSERT INTO app_state (state_key, state_json) VALUES ('store', CAST('{escaped}' AS JSON)) ON DUPLICATE KEY UPDATE state_json = VALUES(state_json);\n"
    target.write_text(sql, encoding='utf-8')
    return target

def backup_history():
    files = list((ROOT / 'backups').glob('luxe_beauty_backup_*.sql')) + list((ROOT / 'backups').glob('luxe-backup-*.json'))
    return sorted(({'name': file.name, 'size': file.stat().st_size, 'created_at': datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec='seconds')} for file in files if file.is_file()), key=lambda item: item['created_at'], reverse=True)

def database_health(data):
    history = backup_history()
    latest = history[0] if history else None
    return {'status': 'healthy' if isinstance(data, dict) and isinstance(data.get('shop'), dict) else 'degraded', 'products': len(data.get('products', [])), 'orders': len(data.get('orders', [])), 'customers': len(data.get('customers', [])), 'latest_backup': latest}

def backup_task_command():
    python_bin = Path(sys.executable).resolve()
    return f'"{python_bin}" "{ROOT / "server.py"}" --backup-once'

def windows_backup_schedule_name():
    return 'LuxeBeautyHubBackup'

def apply_windows_backup_schedule(shop):
    if os.name != 'nt':
        return None
    schedule = shop.get('backup_schedule', 'disabled')
    task_name = windows_backup_schedule_name()
    task_user = os.environ.get('USERNAME') or os.environ.get('USER') or 'SYSTEM'
    if schedule == 'disabled':
        subprocess.run(['schtasks', '/Delete', '/TN', task_name, '/F'], capture_output=True, text=True, check=False, timeout=10)
        return None
    trigger = ['schtasks', '/Create', '/F', '/TN', task_name, '/TR', backup_task_command(), '/RU', task_user, '/NP']
    trigger += ['/ST', str(shop.get('backup_time', '02:00'))]
    if schedule == 'daily':
        trigger += ['/SC', 'DAILY']
    elif schedule == 'weekly':
        trigger += ['/SC', 'WEEKLY', '/D', str(shop.get('backup_day', 'Sunday')).upper()[:3]]
    else:
        return None
    result = subprocess.run(trigger, capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Windows backup task creation failed')
    return result

def update_windows_backup_schedule_background(shop):
    try:
        apply_windows_backup_schedule(shop)
    except Exception:
        pass

def scheduled_backup_loop():
    global LAST_SCHEDULED_BACKUP
    while True:
        time.sleep(30)
        try:
            data = read_db()
            shop = data.get('shop', {})
            schedule = shop.get('backup_schedule', 'disabled')
            now = datetime.now()
            schedule_time = shop.get('backup_time', '02:00')
            due = schedule in ('daily', 'weekly') and now.strftime('%H:%M') == schedule_time
            if schedule == 'weekly' and now.strftime('%A') != shop.get('backup_day', 'Sunday'):
                due = False
            marker = f'{schedule}:{now.strftime("%Y-%m-%d")}'
            if due and marker != LAST_SCHEDULED_BACKUP:
                create_backup(data)
                LAST_SCHEDULED_BACKUP = marker
                retention = max(1, int(shop.get('backup_retention', 7)))
                backups = sorted((ROOT / 'backups').glob('luxe-backup-*.json'), key=lambda item: item.stat().st_mtime, reverse=True)
                for old_backup in backups[retention:]:
                    old_backup.unlink(missing_ok=True)
        except Exception:
            continue

def category_groups(data):
    return data.get('category_hierarchy', {})

def category_options(data, selected=''):
    return ''.join(f'<option value="{esc(category)}" {"selected" if category == selected else ""}>{esc(category)}</option>' for category in category_groups(data))

def subcategory_options(data, selected=''):
    return ''.join(f'<option value="{esc(subcategory)}" {"selected" if subcategory == selected else ""}>{esc(subcategory)}</option>' for subcategories in category_groups(data).values() for subcategory in subcategories)

def esc(value):
    return str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def send_json(handler, payload, status=200, filename=None):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status); handler.send_header('Content-Type', 'application/json; charset=utf-8')
    if filename: handler.send_header('Content-Disposition', f'attachment; filename={filename}')
    handler.send_header('Cache-Control', 'no-store'); handler.end_headers(); handler.wfile.write(body)

def product_slug(product):
    return '-'.join(''.join(character.lower() if character.isalnum() else '-' for character in product.get('name', '')).split('-'))

def money(value):
    return f'{DISPLAY_CURRENCY} {value:,.0f}'

def shop_number(data, key, default):
    try:
        return float(data.get('shop', {}).get(key, default))
    except (TypeError, ValueError):
        return float(default)

def cart_delivery_items(data, cart):
    items = []
    for item in cart:
        product = next((product for product in data.get('products', []) if product.get('id') == item.get('id')), None)
        if product:
            items.append({'shipping_class': product_shipping_class(product), 'weight_kg': product.get('weight_kg', 0.5), 'quantity': item.get('quantity', 0)})
    return items

def delivery_quote(data, location, subtotal, cart):
    return calculate_delivery(delivery_settings(data.setdefault('shop', {})), location, subtotal, cart_delivery_items(data, cart))

def add_pending_cart(data, session):
    pending = session.pop('pending_cart', None)
    if not pending:
        return False
    product = next((p for p in data['products'] if p['id'] == pending['id']), None)
    variant = next((v for v in product.get('variants', []) if v.get('id') == pending.get('variant_id')), None) if product and pending.get('variant_id') else None
    available_stock = variant.get('stock', 0) if variant else product.get('stock', 0) if product else 0
    if not product or available_stock < pending['quantity']:
        return False
    item = next((entry for entry in session['cart'] if entry['id'] == product['id'] and entry.get('variant_id') == pending.get('variant_id')), None)
    if item:
        item['quantity'] = min(available_stock, item['quantity'] + pending['quantity'])
    else:
        session['cart'].append({'id': product['id'], 'variant_id': pending.get('variant_id'), 'quantity': pending['quantity']})
    return True

ORDER_STATUSES = ('Pending', 'Confirmed', 'Processing', 'Packed', 'Shipped', 'Out for Delivery', 'Delivered', 'Cancelled', 'Returned', 'Refunded')

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return 'scrypt$' + salt.hex() + '$' + digest.hex()

def check_password(password, stored):
    try:
        _, salt, digest = stored.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False

def ensure_legacy_admin(data):
    email = os.environ.get('LUXE_ADMIN_EMAIL', '').strip().lower()
    if not email:
        return
    password = os.environ.get('LUXE_ADMIN_PASSWORD', '')
    users = data.setdefault('users', [])
    customers = data.setdefault('customers', [])
    account = next((item for item in users + customers if item.get('email', '').lower() == email), None)
    changed = False
    if account and account in customers:
        customers.remove(account)
        users.append(account)
        changed = True
    if account:
        if account.get('role') not in ADMIN_ROLES:
            account['role'] = 'SUPER_ADMIN'
            changed = True
        if not account.get('active', True):
            account['active'] = True
            changed = True
        if password and not check_password(password, account.get('password_hash', '')):
            account['password_hash'] = hash_password(password)
            changed = True
    elif password:
        next_id = max([item.get('id', 0) for item in users + customers] or [0]) + 1
        users.append({'id': next_id, 'name': 'Store Administrator', 'email': email, 'phone': '', 'password_hash': hash_password(password), 'role': 'SUPER_ADMIN', 'active': True})
        changed = True
    if changed:
        write_db(data)

def send_smtp_email(data, recipients, subject, body):
    integrations = data.get('integrations', {})
    host = integrations.get('smtp_host') or os.environ.get('SMTP_HOST', '')
    port = int(integrations.get('smtp_port') or os.environ.get('SMTP_PORT', '587'))
    user = integrations.get('smtp_user') or os.environ.get('SMTP_USER', '')
    password = integrations.get('smtp_password') or os.environ.get('SMTP_PASSWORD', '')
    sender = integrations.get('smtp_from') or os.environ.get('SMTP_FROM', user)
    if not host or not sender:
        raise RuntimeError('SMTP email is not configured')
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = ', '.join(recipients)
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(message)

def email_document(data, order, document_label='Invoice'):
    recipient = order.get('email', '').strip()
    if not recipient:
        raise RuntimeError('Customer email is not available')
    integrations = data.get('integrations', {})
    host = integrations.get('smtp_host') or os.environ.get('SMTP_HOST', '')
    port = int(integrations.get('smtp_port') or os.environ.get('SMTP_PORT', '587'))
    user = integrations.get('smtp_user') or os.environ.get('SMTP_USER', '')
    password = integrations.get('smtp_password') or os.environ.get('SMTP_PASSWORD', '')
    sender = integrations.get('smtp_from') or os.environ.get('SMTP_FROM', user)
    if not host or not sender:
        raise RuntimeError('SMTP email is not configured')
    message = EmailMessage()
    message['Subject'] = f'{document_label} {order.get("order_number", "")} - {data.get("shop", {}).get("name", "Luxe Beauty Hub")}'
    message['From'] = sender; message['To'] = recipient
    message.set_content(f'Your {document_label.lower()} for order {order.get("order_number", "")} is attached.')
    message.add_attachment(receipt_pdf(data, order, document_label), maintype='application', subtype='pdf', filename=f'{document_label.lower()}-{order.get("order_number", "")}.pdf')
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if user and password: smtp.login(user, password)
        smtp.send_message(message)

def send_password_otp(data, email, code):
    send_smtp_email(data, [email], f'{data.get("shop", {}).get("name", "Luxe Beauty Hub")} password reset code', f'Your password reset code is {code}. It expires in 10 minutes. If you did not request this, ignore this email.')

def notify_order_by_email(data, order):
    recipients = [order.get('email', '').strip()]
    recipients.extend(user.get('email', '').strip() for user in data.get('users', []) if is_admin_role(user.get('role')) and user.get('active', True))
    recipients = list(dict.fromkeys(email for email in recipients if email))
    if not recipients:
        return
    items = '; '.join(f'{item.get("product_id")} x {item.get("quantity", 1)} at {money(item.get("unit_price", 0))}' for item in order.get('items', []))
    body = f'''Order {order.get('order_number')} was placed.

Customer: {order.get('customer_name', '')}
Email: {order.get('email', '')}
Phone: {order.get('phone', '')}
Delivery: {order.get('address', '')}, {order.get('location', '')}
Payment method: {order.get('payment_method', '')}
Payment status: {order.get('payment_status', '')}
Items: {items}
Subtotal: {money(order.get('subtotal', 0))}
Delivery: {money(order.get('delivery_fee', 0))}
Total: {money(order.get('total', 0))}'''
    try:
        send_smtp_email(data, recipients, f'New order {order.get("order_number", "")}', body)
    except (OSError, RuntimeError, smtplib.SMTPException):
        pass

def notify_order_delivered(data, order):
    recipients = [order.get('email', '').strip()]
    recipients.extend(user.get('email', '').strip() for user in data.get('users', []) if is_admin_role(user.get('role')) and user.get('active', True))
    recipients = list(dict.fromkeys(email for email in recipients if email))
    if not recipients:
        return
    body = f'''Order {order.get('order_number')} has been delivered.

Customer: {order.get('customer_name', '')}
Delivery location: {order.get('address', '')}, {order.get('location', '')}
Delivered at: {order.get('delivered_at', '')}

Thank you for shopping with us.'''
    try:
        send_smtp_email(data, recipients, f'Order {order.get("order_number", "")} delivered - thank you for shopping with us', body)
    except (OSError, RuntimeError, smtplib.SMTPException):
        pass

def forgot_password_page(data, message=''):
    notice = f'<p class="notice">{esc(message)}</p>' if message else ''
    body = f'''<main class="auth-page"><section class="auth-card"><div class="auth-card-heading"><span class="auth-icon">↗</span><div><p class="eyebrow">ACCOUNT RECOVERY</p><h2>Reset password</h2></div></div>{notice}<form method="post" class="auth-form"><input type="hidden" name="action" value="forgot_request"><div class="field"><label for="forgot-email">Registered email</label><input id="forgot-email" name="email" type="email" autocomplete="email" required></div><button class="primary auth-submit">Send OTP <span>↗</span></button></form><p class="auth-switch"><a href="/login">Back to sign in</a></p></section></main>'''
    return layout(data, body)

def reset_password_page(data, email, message=''):
    notice = f'<p class="notice">{esc(message)}</p>' if message else ''
    body = f'''<main class="auth-page"><section class="auth-card"><div class="auth-card-heading"><span class="auth-icon">↗</span><div><p class="eyebrow">ACCOUNT RECOVERY</p><h2>Choose a new password</h2></div></div>{notice}<form method="post" class="auth-form"><input type="hidden" name="action" value="forgot_reset"><input type="hidden" name="email" value="{esc(email)}"><div class="field"><label for="reset-code">OTP code</label><input id="reset-code" name="otp" inputmode="numeric" autocomplete="one-time-code" required></div><div class="field"><label for="reset-password">New password</label><input id="reset-password" name="password" type="password" minlength="8" autocomplete="new-password" required></div><button class="primary auth-submit">Reset password <span>↗</span></button></form></section></main>'''
    return layout(data, body)

def is_admin_role(role):
    return role in ADMIN_ROLES or role in ROLE_PERMISSIONS

def is_superadmin_role(role):
    return role in {'SUPER_ADMIN', 'SUPERADMIN'}

def role_can(data, session, permission):
    role = session.get('role', '')
    return is_superadmin_role(role) or role == 'ADMIN' or permission in ROLE_PERMISSIONS.get(role, set())

def action_permission(action):
    return {'branding': 'content', 'settings': 'content', 'search_settings': 'content', 'delivery_settings': 'deliveries', 'delivery_zone_save': 'deliveries', 'delivery_zone_toggle': 'deliveries', 'delivery_zone_delete': 'deliveries', 'backup_now': 'content', 'restore': 'content', 'clear_database': 'content', 'role_save': 'content', 'role_delete': 'content', 'notification_settings': 'content', 'email_invoice': 'orders', 'expense_create': 'expenses', 'expense_delete': 'expenses', 'inventory_adjust': 'inventory', 'stock': 'inventory', 'order_status': 'orders', 'payment_status': 'payments', 'delivery_status': 'deliveries', 'return_request': 'refunds', 'return_status': 'refunds', 'refund_create': 'refunds', 'coupon': 'promotions', 'campaign_create': 'promotions', 'coupon_toggle': 'promotions', 'coupon_delete': 'promotions', 'customer_toggle': 'customers', 'staff_create': 'content', 'staff_toggle': 'content', 'review_moderate': 'content', 'product_create': 'products', 'product_update': 'products', 'product_delete': 'products', 'categories_save': 'products', 'feature': 'content'}.get(action)
def online_account_emails():
    now = time.time()
    return {
        session.get('email', '').strip().lower()
        for session in SESSIONS.values()
        if session.get('email') and now - session.get('last_seen', now) <= SESSION_IDLE_SECONDS
    }

def validate_backup(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8-sig')
    if isinstance(value, str):
        marker = '-- LUXE_STATE_JSON:'
        if marker in value:
            encoded = value.split(marker, 1)[1].splitlines()[0].strip()
            value = base64.b64decode(encoded).decode('utf-8')
        value = json.loads(value.lstrip('\ufeff'))
    if not isinstance(value, dict) or not isinstance(value.get('products'), list) or not isinstance(value.get('shop'), dict):
        raise ValueError('Backup must contain products and shop data')
    value.setdefault('users', [])
    value.setdefault('customers', [])
    value.setdefault('orders', [])
    value.setdefault('categories', [])
    return value

def normalize_mpesa_phone(phone):
    digits = ''.join(character for character in phone if character.isdigit())
    if digits.startswith('0'):
        digits = '254' + digits[1:]
    return digits

def whatsapp_url(phone, message):
    digits = ''.join(character for character in phone if character.isdigit())
    if digits.startswith('0'):
        digits = '254' + digits[1:]
    if not digits.startswith('254') or len(digits) != 12 or digits[3] not in '17':
        return ''
    return f'https://wa.me/{digits}?{urlencode({"text": message})}'

def initiate_mpesa(data, phone, amount, order_number):
    integrations = data.get('integrations', {})
    values = {
        'environment': integrations.get('mpesa_environment') or os.environ.get('MPESA_ENVIRONMENT', 'sandbox'),
        'shortcode': integrations.get('mpesa_shortcode') or os.environ.get('MPESA_SHORTCODE'),
        'consumer_key': integrations.get('mpesa_consumer_key') or os.environ.get('MPESA_CONSUMER_KEY'),
        'consumer_secret': integrations.get('mpesa_consumer_secret') or os.environ.get('MPESA_CONSUMER_SECRET'),
        'passkey': integrations.get('mpesa_passkey') or os.environ.get('MPESA_PASSKEY'),
        'callback_url': integrations.get('mpesa_callback_url') or os.environ.get('MPESA_CALLBACK_URL'),
    }
    if not all(values.values()):
        raise RuntimeError('M-Pesa credentials and callback URL are not configured')
    normalized_phone = normalize_mpesa_phone(phone)
    if not normalized_phone.startswith('254') or len(normalized_phone) != 12 or normalized_phone[3] not in '17':
        raise RuntimeError('Enter a valid Kenyan M-Pesa number')
    host = 'https://api.safaricom.co.ke' if values['environment'] == 'production' else 'https://sandbox.safaricom.co.ke'
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    with httpx.Client(timeout=15.0) as client:
        auth = client.get(f'{host}/oauth/v1/generate', params={'grant_type': 'client_credentials'}, auth=(values['consumer_key'], values['consumer_secret']))
        auth.raise_for_status()
        password = base64.b64encode(f'{values["shortcode"]}{values["passkey"]}{timestamp}'.encode()).decode()
        response = client.post(f'{host}/mpesa/stkpush/v1/processrequest', headers={'Authorization': f'Bearer {auth.json()["access_token"]}'}, json={
            'BusinessShortCode': int(values['shortcode']), 'Password': password, 'Timestamp': timestamp,
            'TransactionType': 'CustomerPayBillOnline', 'Amount': max(1, int(round(amount))),
            'PartyA': normalized_phone, 'PartyB': int(values['shortcode']), 'PhoneNumber': normalized_phone,
            'CallBackURL': values['callback_url'], 'AccountReference': order_number, 'TransactionDesc': f'Luxe order {order_number}',
        })
        response.raise_for_status()
        result = response.json()
    if not result.get('CheckoutRequestID'):
        raise RuntimeError(result.get('errorMessage') or 'M-Pesa returned no checkout request ID')
    return result

def apply_mpesa_callback(data, payload):
    callback = payload.get('Body', {}).get('stkCallback', {})
    request_id = callback.get('CheckoutRequestID')
    if not request_id:
        return False
    order = next((item for item in data.get('orders', []) if item.get('mpesa_checkout_request_id') == request_id), None)
    if not order or order.get('payment_status') != 'Pending':
        return False
    result_code = callback.get('ResultCode')
    order['payment_status'] = 'Paid' if result_code == 0 else 'Failed'
    order['mpesa_result_description'] = callback.get('ResultDesc', '')
    if result_code == 0:
        order['status'] = 'Confirmed'
        order['mpesa_receipt'] = next((item.get('Value') for item in callback.get('CallbackMetadata', {}).get('Item', []) if item.get('Name') == 'MpesaReceiptNumber'), '')
    return True

def shop_categories(data):
    legacy_images = {category['name']: category['image'] for category in data.get('categories', [])}
    images = {
        'PERFUMES & FRAGRANCES': legacy_images.get('Perfume', ''),
        'SKINCARE': legacy_images.get('Skincare', ''),
        'KITCHENWARE': legacy_images.get('Home & Kitchen', ''),
        'HAIRCARE': 'https://images.unsplash.com/photo-1522338242992-e1a54906a8da?auto=format&fit=crop&w=700&q=85',
        'MAKEUP & COSMETICS': 'https://images.unsplash.com/photo-1512496015851-a90fb38ba796?auto=format&fit=crop&w=700&q=85',
        'JEWELLERY': 'https://images.unsplash.com/photo-1515562141207-7a88fb7ce338?auto=format&fit=crop&w=700&q=85',
        'GIFT & LIFESTYLE ITEMS': 'https://images.unsplash.com/photo-1549465220-1a8b9238cd48?auto=format&fit=crop&w=700&q=85',
        'WATCHES': 'https://images.unsplash.com/photo-1524805444758-089113d48a6d?auto=format&fit=crop&w=700&q=85',
        'DUVETS & DUVET COVERS': 'https://images.unsplash.com/photo-1584100936595-c0654b55a2e2?auto=format&fit=crop&w=700&q=85',
        'COFFEE CUPS': 'https://images.unsplash.com/photo-1514228742587-6b1558fcca3d?auto=format&fit=crop&w=700&q=85',
        'STANLEY CUPS': 'https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=700&q=85',
        'KITCHEN APPLIANCES': 'https://images.unsplash.com/photo-1556911220-e15b29be8c8f?auto=format&fit=crop&w=700&q=85',
        'WASHING MACHINES': 'https://images.unsplash.com/photo-1626806787461-102c1bfaaea1?auto=format&fit=crop&w=700&q=85',
    }
    descriptions = {'PERFUMES & FRAGRANCES': 'Scent stories for every mood.', 'SKINCARE': 'Thoughtful rituals for luminous skin.', 'HAIRCARE': 'Care for every texture and ritual.', 'JEWELLERY': 'Quiet details with a lasting point of view.', 'GIFT & LIFESTYLE ITEMS': 'Beautiful gestures for every occasion.', 'KITCHENWARE': 'Elevated essentials for daily living.'}
    fallback = 'https://images.unsplash.com/photo-1556228578-8c89e6adf883?auto=format&fit=crop&w=700&q=80'
    return ''.join(f'<a href="/category/{esc(category.lower().replace(" ", "-"))}"><img src="{esc(images.get(category, fallback))}" alt="{esc(category)}"><b>{esc(category)}</b><span>{esc(descriptions.get(category, "Curated essentials for everyday living."))}</span><i>View collection ↗</i></a>' for category, subcategories in category_groups(data).items())

def valid_image_upload(filename, content_type, size):
    allowed = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg', '.avif'}
    suffix = Path(filename).suffix.lower()
    return suffix in allowed and (content_type.startswith('image/') or content_type in ('application/octet-stream', '')) and 0 < size <= 5 * 1024 * 1024

def parse_form(content_type, raw):
    if not content_type.startswith('multipart/form-data'):
        return parse_qs(raw.decode('utf-8', errors='replace'))
    boundary = content_type.split('boundary=', 1)[-1].strip().strip('"').encode()
    fields = {}
    for part in raw.split(b'--' + boundary):
        if b'\r\n\r\n' not in part: continue
        header_bytes, value = part.split(b'\r\n\r\n', 1)
        if value.endswith(b'\r\n'):
            value = value[:-2]
        headers = header_bytes.decode('utf-8', errors='replace')
        disposition = next((line for line in headers.split('\r\n') if line.lower().startswith('content-disposition:')), '')
        name_match = __import__('re').search(r'name="([^"]+)"', disposition)
        if not name_match: continue
        name = name_match.group(1); filename_match = __import__('re').search(r'filename="([^"]*)"', disposition)
        if filename_match and filename_match.group(1):
            filename = Path(filename_match.group(1)).name; content_match = __import__('re').search(r'Content-Type:\s*([^\r\n]+)', headers, __import__('re').I)
            content = content_match.group(1).strip() if content_match else ''
            if name == 'backup_file' and 0 < len(value) <= MAX_REQUEST_BYTES:
                fields[name] = [value.decode('utf-8', errors='replace')]
                continue
            if not valid_image_upload(filename, content, len(value)):
                fields.setdefault('_upload_errors', []).append(f'Unsupported image upload: {filename}')
                continue
            upload_dir = ROOT / 'uploads'; upload_dir.mkdir(exist_ok=True)
            stem = secrets.token_hex(8)
            target = upload_dir / f'{stem}{Path(filename).suffix.lower()}'
            target.write_bytes(value)
            fields.setdefault(name, []).append('/uploads/' + target.name)
        else:
            fields[name] = [value.decode('utf-8', errors='replace')]
    return fields

def legacy_layout(data, body, theme=None):
    theme = theme or data.get('_theme', 'light')
    name = esc(data['shop']['name'])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{esc(data['shop'].get('description', 'Curated beauty, home and lifestyle essentials'))}"><title>{name}</title><link rel="stylesheet" href="/styles.css"></head><body class="theme-{theme}"><header class="header"><a class="logo" href="/"><b>L</b><span>{name}</span></a><nav><a href="/">Home</a><a href="/?category=Perfume">Shop</a><a href="/?category=Skincare">Self care</a><a href="/?category=Home+%26+Kitchen">Home edit</a><a href="/account">Account</a><a href="/admin">Admin</a></nav><div class="tools">⌕　<a href="/wishlist">♡</a>　<a href="/cart">Bag</a>　<a href="/?theme=light">☀ Light</a>　<a href="/?theme=dark">☾ Dark</a></div></header>{body}<footer><div><a class="logo"><b>L</b><span>{name}</span></a><p>{esc(data['shop']['tagline'])}</p></div><div><strong>Explore</strong><a href="/">Shop all</a><a href="/?category=Perfume">New arrivals</a><a href="/">Gifts</a></div><div><strong>Client care</strong><a>Delivery & returns</a><a>Contact us</a><a>WhatsApp concierge</a></div><div><strong>Stay in the know</strong><p>Notes on beauty, home and living.</p><p>Phone: {esc(data['shop'].get('phone', ''))}</p><p>Email: {esc(data['shop'].get('email', ''))}</p></div></footer></body></html>'''

def render_template(filename, values):
    template = (ROOT / 'templates' / filename).read_text(encoding='utf-8')
    for key, value in values.items():
        template = template.replace('{{' + key + '}}', str(value))
    return template

def logo_mark(shop, size='small'):
    image = shop.get('logo', '').strip()
    name = esc(shop.get('name', 'Luxe Beauty Hub'))
    if image and logo_source_available(image):
        return f'<img class="brand-logo {size}" src="{esc(image)}" alt="{name}" loading="lazy" referrerpolicy="no-referrer">'
    return f'<b>{esc(name[:1] or "L")}</b>'


def run_backup_once():
    data = read_db()
    backup = create_backup(data)
    retention = max(1, int(data.get('shop', {}).get('backup_retention', 7)))
    backup_dir = ROOT / 'backups'
    backup_dir.mkdir(exist_ok=True)
    backups = sorted(backup_dir.glob('luxe-backup-*.json'), key=lambda item: item.stat().st_mtime, reverse=True)
    for old_backup in backups[retention:]:
        old_backup.unlink(missing_ok=True)
    print(f'Backup created: {backup}')
    return backup

def layout(data, body, theme=None):
    theme = theme or data.get('_theme', 'light')
    shop = data['shop']
    admin_role = data.get('_admin_role')
    admin_label = 'Superadmin' if admin_role in ('SUPER_ADMIN', 'SUPERADMIN') else 'Admin' if admin_role == 'ADMIN' else ''
    assistant_link = '<a class="assistant-nav-link" href="/assistant">✦ Ask here</a>' if data.get('_customer_id') else ''
    rendered = render_template('base.html', {
        'description': esc(shop.get('description', 'Curated beauty, home and lifestyle essentials')),
        'title': esc(shop['name']),
        'theme': esc(theme),
        'header': render_template('header.html', {'shop_name': esc(shop['name']), 'logo_html': logo_mark(shop, 'small'), 'wishlist_badge': f'<sup>{data.get("_wishlist_count", 0)}</sup>' if data.get('_wishlist_count', 0) else '', 'cart_badge': f'<sup>{data.get("_cart_count", 0)}</sup>' if data.get('_cart_count', 0) else '', 'account_link': data.get('_account_link', '/login'), 'wishlist_link': data.get('_wishlist_link', '/login'), 'cart_link': data.get('_cart_link', '/login'), 'auth_link': data.get('_auth_link', ''), 'assistant_link': assistant_link, 'admin_link': f'<a class="admin-nav-link" href="/admin">{admin_label}</a>' if admin_label else ''}),
        'content': body,
        'footer': render_template('footer.html', {'shop_name': esc(shop['name']), 'logo_html': logo_mark(shop, 'small'), 'tagline': esc(shop.get('tagline', '')), 'phone': esc(shop.get('phone', '')), 'whatsapp': esc(shop.get('whatsapp', '')), 'email': esc(shop.get('email', '')), 'location': esc(shop.get('location', '')), 'delivery_information': esc(shop.get('delivery_information', '')), 'return_policy': esc(shop.get('return_policy', '')), 'refund_policy': esc(shop.get('return_policy', '')), 'privacy_policy': esc(shop.get('privacy_policy', '')), 'terms': esc(shop.get('terms', '')), 'instagram': esc(shop.get('social', {}).get('instagram', '')), 'facebook': esc(shop.get('social', {}).get('facebook', '')), 'tiktok': esc(shop.get('social', {}).get('tiktok', ''))}),
    })
    return rendered.replace('<form class="search">', '<form class="search" target="_blank" rel="noopener">')

def card(p):
    sale = f'<del>{money(p["old_price"])}</del>' if p.get('old_price') else ''
    sold = '<div class="sold">Out of stock</div>' if not p['stock'] else ''
    button = 'Unavailable' if not p['stock'] else 'Add to bag ↗'
    disabled = 'disabled' if not p['stock'] else ''
    return f'''<article class="product-card"><div class="product-image"><a href="/product?id={p['id']}"><img src="{esc(p['image'])}" alt="{esc(p['name'])}" loading="lazy"></a><span class="tag">{esc(p['tag'])}</span><form method="post" class="wishlist-card-form"><input type="hidden" name="action" value="wishlist"><input type="hidden" name="product_id" value="{p['id']}"><button class="heart" aria-label="Save {esc(p['name'])} to wishlist">♡</button></form>{sold}</div><div class="meta"><div><small>{esc(p['brand'])}</small><h3>{esc(p['name'])}</h3></div><span class="rating">★ {p['rating']}</span></div><div class="price"><b>{money(p['price'])}</b>{sale}<form method="post"><input type="hidden" name="action" value="cart"><input type="hidden" name="product_id" value="{p['id']}"><button {disabled}>{button}</button></form></div></article>'''

HOMEPAGE_SECTIONS = ('New Arrivals', 'Best Sellers', 'Featured Products', 'Trending Products', 'Special Offers', 'Recommended Products')

def homepage_sections(data):
    sections = []
    for section in HOMEPAGE_SECTIONS:
        products = [product for product in data.get('products', []) if section in product.get('featured_sections', [])]
        if products:
            sections.append(f'<section class="catalog homepage-section"><div class="section-head"><div><p class="eyebrow">CURATED FOR YOU</p><h2>{esc(section)}</h2></div></div><div class="product-grid">{"".join(card(product) for product in products[:8])}</div></section>')
    shop = data.get('shop', {})
    story = shop.get('story_description', 'Curated beauty. Thoughtful essentials. Everyday luxury.')
    newsletter = shop.get('newsletter_description', 'Join our circle for new arrivals, considered edits and occasional offers.')
    sections.append(f'<section class="manifesto brand-story"><p class="eyebrow">OUR POINT OF VIEW</p><h2>{esc(shop.get("story_heading", "Beautiful things for everyday living."))}</h2><p>{esc(story)}</p><a class="under" href="/about">Read our story ↗</a></section>')
    sections.append(f'<section class="newsletter"><div><p class="eyebrow">THE LUXE LETTER</p><h2>{esc(shop.get("newsletter_heading", "A little beauty, delivered."))}</h2><p>{esc(newsletter)}</p></div><form method="post"><input type="hidden" name="action" value="newsletter"><input name="email" type="email" placeholder="Your email address" required><button class="primary">Subscribe ↗</button></form></section>')
    return ''.join(sections)

def home(data, query):
    q = query.get('q',[''])[0].lower(); category = query.get('category',['All'])[0]; section = query.get('section',[''])[0]
    brand = query.get('brand',['All'])[0]; availability = query.get('availability',['All'])[0]; sort = query.get('sort',['latest'])[0]
    products = [p for p in data['products'] if (not section or section in p.get('featured_sections', [])) and (category == 'All' or p['category'] == category) and (brand == 'All' or p['brand'] == brand) and (availability == 'All' or availability == 'in-stock' and p['stock'] > 0 or availability == 'discount' and p.get('old_price')) and q in f"{p.get('name', '')} {p.get('brand', '')} {p.get('category', '')} {p.get('subcategory', '')} {p.get('sku', '')} {p.get('description', '')} {' '.join(p.get('tags', []))} {json.dumps(p.get('specifications', {}))} {json.dumps(p.get('variants', []))}".lower()]
    if sort == 'price-low': products.sort(key=lambda p: p['price'])
    elif sort == 'price-high': products.sort(key=lambda p: p['price'], reverse=True)
    elif sort == 'rating': products.sort(key=lambda p: p.get('rating', 0), reverse=True)
    elif sort == 'name': products.sort(key=lambda p: p['name'].lower())
    page_matches = ''
    searchable_pages = (('About us', '/about', data['shop'].get('description', '') + data['shop'].get('story_description', '')), ('Delivery information', '/delivery', data['shop'].get('delivery_information', '')), ('Returns and refunds', '/returns', data['shop'].get('return_policy', '')), ('FAQs', '/faq', 'Frequently asked questions'))
    if q:
        page_matches = ''.join(f'<a class="search-result-link" href="{href}"><b>{label}</b><small>Open page ↗</small></a>' for label, href, content in searchable_pages if q in content.lower())
        if page_matches: page_matches = f'<div class="search-page-results"><p class="eyebrow">WEBSITE PAGES</p>{page_matches}</div>'
    cards = page_matches + (''.join(card(p) for p in products) or '<p class="empty">Nothing found in this edit.</p>')
    pills = ''.join(f'<a class="pill {"active" if c == category else ""}" href="{("/shop" if c == "All" else "/category/" + c.lower().replace(" ", "-"))}">{c}</a>' for c in ['All'] + list(category_groups(data)))
    hero = data['shop'].get('hero', {})
    brands = sorted({p['brand'] for p in data['products']})
    brand_options = ''.join(f'<option value="{esc(item)}">' for item in brands)
    return layout(data, f'''<main><section class="hero"><div class="hero-copy"><p class="eyebrow">THE EVERYDAY EDIT / 01</p><h1>{esc(hero.get('heading', 'Discover something beautiful.')).replace(' ', '<br>')}</h1><p class="hero-text">{esc(hero.get('description', 'Thoughtful objects, sensory rituals and little luxuries for living well.'))}</p><a class="primary" href="{esc(hero.get('primary_link', '#catalog'))}">{esc(hero.get('primary_label', 'Shop the edit'))} ↗</a></div><div class="hero-image"><img src="{esc(hero.get('image', ''))}" alt="{esc(hero.get('heading', 'Shop collection'))}"><span class="stamp">BEAUTY<br>• HOME<br>• LIFE</span></div></section><section class="categories"><div class="section-head"><div><p class="eyebrow">SHOP BY MOOD</p><h2>Find your next <em>favourite.</em></h2></div><a class="under">View all categories ↗</a></div><div class="category-grid">{shop_categories(data)}</div></section>{homepage_sections(data)}<section class="catalog" id="catalog"><div class="section-head"><div><p class="eyebrow">{'SEARCH RESULTS FOR "' + esc(query.get('q',[''])[0]) + '"' if query.get('q',[''])[0] else 'THE CURRENT EDIT'}</p><h2>Pieces worth <em>keeping.</em></h2></div><form class="search"><input name="q" value="{esc(query.get('q',[''])[0])}" list="brand-list" placeholder="Search name, brand, SKU or tag"><datalist id="brand-list">{brand_options}</datalist><select name="brand"><option>All</option>{''.join(f'<option>{esc(item)}</option>' for item in brands)}</select><select name="availability"><option value="All">Any stock</option><option value="in-stock">In stock</option><option value="discount">Discounts</option></select><select name="sort"><option value="latest">Latest</option><option value="price-low">Price low to high</option><option value="price-high">Price high to low</option><option value="rating">Best rated</option><option value="name">Name A-Z</option></select><button>Search</button></form></div><div class="pills">{pills}</div><div class="product-grid">{cards}</div></section><section class="manifesto"><p class="eyebrow">OUR POINT OF VIEW</p><h2>Beauty is in the <em>details.</em></h2><p>We seek out the things that make an ordinary day feel considered.</p><a class="under">About Luxe ↗</a></section></main>''')

def legacy_admin(data, message=''):
    total_sales = sum(order.get('total', 0) for order in data.get('orders', []))
    low_stock = sum(1 for product in data['products'] if 0 < product.get('stock', 0) <= product.get('minimum_stock', 5))
    out_of_stock = sum(1 for product in data['products'] if product.get('stock', 0) == 0)
    section_options = ''.join(f'<option>{section}</option>' for section in HOMEPAGE_SECTIONS)
    rows = ''.join(f'''<div class="table-row"><span class="admin-product"><img src="{esc(p['image'])}" loading="lazy"><b>{esc(p['name'])}<small>{esc(p['brand'])} · SKU-00{p['id']}</small></b></span><span>{esc(p['category'])}</span><span>{money(p['price'])}</span><span>{p['stock']}</span><span><i class="status">{'Out' if not p['stock'] else 'Low stock' if p['stock'] <= 5 else 'In stock'}</i></span><span><a class="under" href="/admin/products/edit?id={p['id']}">Edit</a><form method="post"><input type="hidden" name="action" value="product_delete"><input type="hidden" name="product_id" value="{p['id']}"><button onclick="return confirm('Delete this product?')">Delete</button></form><form method="post"><input type="hidden" name="action" value="stock"><input type="hidden" name="product_id" value="{p['id']}"><button name="amount" value="1">＋</button><button name="amount" value="-1">−</button></form><form method="post"><input type="hidden" name="action" value="feature"><input type="hidden" name="product_id" value="{p['id']}"><select name="section">{section_options}</select><button>Feature</button></form></span></div>''' for p in data['products'])
    return layout(data, f'''<main class="admin-page"><div class="admin-shell"><aside class="sidebar"><div class="admin-logo">L　CONTROL<br>　 ROOM</div><p>WORKSPACE</p><a class="selected" href="/admin">▦ Overview</a><a href="/admin/products">□ Products <span>{len(data['products'])}</span></a><a href="/admin/categories">▤ Categories <span>{len(category_groups(data))}</span></a><a href="/admin/orders">▱ Orders <span>{len(data.get('orders', []))}</span></a><a href="/admin/payments">▣ Payments</a><a href="/admin/deliveries">▰ Deliveries</a><a href="/admin/customers">♙ Customers</a><p>MANAGE</p><a href="/admin/promotions">◇ Promotions</a><a href="/admin/settings">⚙ Settings</a><a href="/admin/staff">♙ Staff</a><a href="/admin/audit">▤ Audit logs</a><a href="/admin/backup">⇩ Backup database</a><a href="/admin/restore">⇧ Restore database</a></aside><section class="admin-content"><div class="section-head"><div><p class="eyebrow">THURSDAY, 03 SEPTEMBER 2026</p><h1>Good morning, <em>Admin.</em></h1></div><a class="under" href="/">View storefront ↗</a></div>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<div class="stats"><div><span>Total sales</span><b>{money(total_sales)}</b><small>Calculated from orders</small></div><div><span>Total orders</span><b>{len(data.get('orders', []))}</b><small>Managed from admin</small></div><div><span>Customers</span><b>{len(data.get('customers', []))}</b><small>Managed from admin</small></div><div><span>Products</span><b>{len(data['products'])}</b><small>{low_stock} low stock · {out_of_stock} out</small></div></div><div class="admin-grid"><section class="panel"><div class="section-head"><div><p class="eyebrow">CATALOGUE</p><h2>Product inventory</h2></div><a class="primary" href="/admin/products">＋ Add product</a></div><div class="table"><div class="table-row table-head"><span>Product</span><span>Category</span><span>Price</span><span>Stock</span><span>Status</span><span></span></div>{rows}</div></section><section class="panel"><p class="eyebrow">SETTINGS</p><h2>Shop information</h2><form method="post"><input type="hidden" name="action" value="settings"><label>Shop name<input name="shop_name" value="{esc(data['shop']['name'])}"></label><label>Currency<select disabled><option>KSh — Kenyan Shilling</option></select></label><label>WhatsApp number<input value="{esc(data['shop']['whatsapp'])}"></label><button class="primary">Save changes ✓</button></form><small class="note">All storefront prices and reports use Kenyan shillings.</small></section></div></section></div></main>''')

def admin(data, message=''):
    orders = data.get('orders', [])
    today = datetime.now().date().isoformat()
    today_sales = sum(order.get('total', 0) for order in orders if str(order.get('created_at', '')).startswith(today))
    pending = sum(1 for order in orders if order.get('status') == 'Pending')
    completed = sum(1 for order in orders if order.get('status') == 'Delivered')
    total_sales = sum(order.get('total', 0) for order in orders)
    low_stock = sum(1 for product in data.get('products', []) if 0 < product.get('stock', 0) <= product.get('minimum_stock', 5))
    out_of_stock = sum(1 for product in data.get('products', []) if product.get('stock', 0) == 0)
    sales_by_day = {}
    for order in orders:
        day = str(order.get('created_at', ''))[:10]
        sales_by_day[day] = sales_by_day.get(day, 0) + order.get('total', 0)
    recent_days = sorted(sales_by_day)[-7:]
    max_sales = max([sales_by_day.get(day, 0) for day in recent_days] or [1])
    chart = ''.join(f'<div class="chart-bar"><span style="height:{max(8, round(sales_by_day.get(day, 0) / max_sales * 100))}%"></span><small>{esc(day[-5:])}</small></div>' for day in recent_days) or '<p class="note">Sales activity will appear here after the first order.</p>'
    rows = ''.join(f'<div class="table-row"><span class="admin-product"><img src="{esc(product.get("image", ""))}" loading="lazy"><b>{esc(product.get("name", ""))}<small>{esc(product.get("sku", ""))}</small></b></span><span>{esc(product.get("category", ""))}</span><span>{money(product.get("price", 0))}</span><span>{product.get("stock", 0)}</span><span><i class="status">{"Out" if not product.get("stock") else "Low stock" if product.get("stock", 0) <= product.get("minimum_stock", 5) else "In stock"}</i></span><span><a class="under" href="/admin/products/edit?id={product.get("id")}">Edit</a>{f'<form method="post" class="inline-form"><input type="hidden" name="action" value="product_delete"><input type="hidden" name="product_id" value="{product.get("id")}"><button onclick="return confirm(\'Delete this product?\')">Delete</button></form>' if is_superadmin_role(data.get("_admin_role")) else ""}</span></div>' for product in data.get('products', [])[:12]) or '<p class="empty">No products have been added yet.</p>'
    sidebar = f'''<aside class="sidebar"><div class="admin-logo">L　CONTROL<br>　 ROOM</div><p>WORKSPACE</p><a class="selected" href="/admin">▦ Dashboard</a><a href="/admin/products">□ Products <span>{len(data.get('products', []))}</span></a><a href="/admin/categories">▤ Categories <span>{len(category_groups(data))}</span></a><a href="/admin/orders">▱ Orders <span>{len(orders)}</span></a><a href="/admin/customers">♙ Customers <span>{len(data.get('customers', []))}</span></a><a href="/admin/reviews">☆ Reviews</a><p>OPERATIONS</p><a href="/admin/payments">▣ Payments</a><a href="/admin/deliveries">▰ Deliveries</a><a href="/admin/promotions">◇ Coupons & promotions</a><a href="/admin/reports">◫ Reports</a><p>SETTINGS</p><a href="/admin/settings">⚙ Shop information</a><a href="/admin/staff">♙ Users & roles</a><a href="/admin/audit">▤ Audit logs</a><a href="/admin/backup">⇩ Backup database</a><a href="/admin/restore">⇧ Restore database</a></aside>'''
    body = f'''<main class="admin-page"><div class="admin-shell">{sidebar}<section class="admin-content"><div class="section-head"><div><p class="eyebrow">BUSINESS OVERVIEW</p><h1>Good morning, <em>Admin.</em></h1></div><a class="under" href="/">View storefront ↗</a></div>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<div class="stats"><div><span>Total sales</span><b>{money(total_sales)}</b><small>All recorded orders</small></div><div><span>Today's sales</span><b>{money(today_sales)}</b><small>{today}</small></div><div><span>Pending orders</span><b>{pending}</b><small>{completed} delivered</small></div><div><span>Customers</span><b>{len(data.get('customers', []))}</b><small>{len(data.get('products', []))} products</small></div><div><span>Low stock</span><b>{low_stock}</b><small>{out_of_stock} out of stock</small></div></div><div class="admin-grid"><section class="panel sales-chart"><div class="section-head"><div><p class="eyebrow">SALES PERFORMANCE</p><h2>Recent sales</h2></div><a class="under" href="/admin/reports">View reports ↗</a></div><div class="chart-bars">{chart}</div></section><section class="panel"><p class="eyebrow">QUICK ACTIONS</p><h2>Keep things moving.</h2><div class="quick-actions"><a class="primary" href="/admin/products">Add product ↗</a><a class="under" href="/admin/orders">Review orders</a><a class="under" href="/admin/settings">Edit storefront</a></div></section></div><section class="panel"><div class="section-head"><div><p class="eyebrow">INVENTORY</p><h2>Product health</h2></div><a class="primary" href="/admin/products">Manage products ↗</a></div><div class="table"><div class="table-row table-head"><span>Product</span><span>Category</span><span>Price</span><span>Stock</span><span>Status</span><span></span></div>{rows}</div></section></section></div></main>'''
    return layout(data, body)

def operations_dashboard(data, message='', params=None):
    params = params or {}
    now = datetime.now()
    period = params.get('period', ['today'])[0]
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    if period == 'yesterday':
        start -= timedelta(days=1); end = start + timedelta(days=1)
    elif period == '7days': start -= timedelta(days=6)
    elif period == '30days': start -= timedelta(days=29)
    elif period == 'month': start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == 'year': start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == 'custom':
        try: start = datetime.strptime(params.get('start', [''])[0], '%Y-%m-%d'); end = datetime.strptime(params.get('end', [''])[0], '%Y-%m-%d') + timedelta(days=1)
        except ValueError: period = 'today'

    def created_at(record):
        try: return datetime.fromisoformat(str(record.get('created_at', '')).replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError: return datetime.min

    orders = [order for order in data.get('orders', []) if start <= created_at(order) < end]
    all_orders = data.get('orders', [])
    order_total = lambda order: float(order.get('total', 0) or 0)
    revenue = sum(order_total(order) for order in orders if order.get('status') != 'Cancelled')
    today = now.date()
    def revenue_since(days=None, month=False, year=False):
        if year: threshold = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif month: threshold = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else: threshold = now - timedelta(days=days)
        return sum(order_total(order) for order in all_orders if created_at(order) >= threshold and order.get('status') != 'Cancelled')

    pending = sum(order.get('status', 'Pending') in ('Pending', 'Confirmed', 'Processing') for order in orders)
    awaiting_delivery = sum(order.get('status') in ('Packed', 'Ready for Delivery', 'Shipped', 'Out for Delivery') for order in orders)
    completed = sum(order.get('status') == 'Delivered' for order in orders)
    cancelled = sum(order.get('status') in ('Cancelled', 'Returned') for order in orders)
    refunds = sum(order_total(order) for order in orders if order.get('payment_status') == 'Refunded' or order.get('status') == 'Refunded')
    mpesa = sum(order_total(order) for order in orders if order.get('payment_method') == 'M-Pesa')
    failed_payments = sum(order.get('payment_status') == 'Failed' for order in orders)
    low_stock = [product for product in data.get('products', []) if 0 < product.get('stock', 0) <= product.get('minimum_stock', 5)]
    out_of_stock = [product for product in data.get('products', []) if product.get('stock', 0) == 0]
    best_sellers = {}
    for order in orders:
        for item in order.get('items', []): best_sellers[item.get('product_id')] = best_sellers.get(item.get('product_id'), 0) + int(item.get('quantity', 0))
    product_by_id = {product.get('id'): product for product in data.get('products', [])}
    best_rows = ''.join(f'<div class="table-row"><span>{esc(product_by_id.get(product_id, {}).get("name", f"Product #{product_id}"))}</span><span>{quantity} sold</span></div>' for product_id, quantity in sorted(best_sellers.items(), key=lambda pair: pair[1], reverse=True)[:5]) or '<p class="empty">Sales will appear here after the first order.</p>'
    recent_rows = ''.join(f'<div class="table-row"><span><b>{esc(order.get("order_number", ""))}</b><small>{esc(order.get("customer_name", "Guest"))}</small></span><span>{esc(order.get("status", "Pending"))}</span><span>{money(order_total(order))}</span></div>' for order in sorted(orders, key=created_at, reverse=True)[:8]) or '<p class="empty">No orders in this period.</p>'
    activity_rows = ''.join(f'<div class="table-row"><span>{esc(log.get("date", ""))}</span><span>{esc(log.get("user", "Admin"))}</span><span>{esc(log.get("description", log.get("action", "")))}</span></div>' for log in sorted(data.get('audit_logs', []), key=lambda log: str(log.get('date', '')), reverse=True)[:8]) or '<p class="empty">No recent admin activity.</p>'
    period_options = ''.join(f'<option value="{value}" {"selected" if value == period else ""}>{label}</option>' for value, label in (('today', 'Today'), ('yesterday', 'Yesterday'), ('7days', '7 Days'), ('30days', '30 Days'), ('month', 'This Month'), ('year', 'This Year'), ('custom', 'Custom')))
    cards = [("Today's sales", money(revenue)), ('Orders', len(orders)), ('Pending orders', pending), ('Awaiting delivery', awaiting_delivery), ('Completed', completed), ('Cancelled', cancelled), ('Refunds', money(refunds)), ('Customers', len(data.get('customers', []))), ('Low stock', len(low_stock)), ('Out of stock', len(out_of_stock)), ('M-Pesa payments', money(mpesa)), ('Failed payments', failed_payments), ('Week revenue', money(revenue_since(7))), ('Month revenue', money(revenue_since(month=True))), ('Year revenue', money(revenue_since(year=True)))]
    metric_cards = ''.join(f'<div><span>{label}</span><b>{value}</b></div>' for label, value in cards)
    body = f'''<main class="admin-page"><div class="admin-shell"><aside class="sidebar"><div class="admin-logo">L　CONTROL<br>　 ROOM</div><p>WORKSPACE</p><a class="selected" href="/admin">▦ Dashboard</a><a href="/admin/products">□ Products <span>{len(data.get('products', []))}</span></a><a href="/admin/categories">▤ Categories</a><a href="/admin/orders">▱ Orders <span>{len(all_orders)}</span></a><a href="/admin/customers">♙ Customers</a><p>OPERATIONS</p><a href="/admin/payments">▣ Payments</a><a href="/admin/deliveries">▰ Deliveries</a><a href="/admin/promotions">◇ Promotions</a><a href="/admin/reports">◫ Reports</a><p>SETTINGS</p><a href="/admin/settings">⚙ Settings</a><a href="/admin/staff">♙ Users & roles</a><a href="/admin/audit">▤ Audit logs</a><a href="/admin/backup">⇩ Backup</a></aside><section class="admin-content"><div class="section-head"><div><p class="eyebrow">BUSINESS OVERVIEW</p><h1>Operations <em>dashboard.</em></h1></div><a class="under" href="/">View storefront ↗</a></div>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<form class="dashboard-filter"><label>Date range<select name="period">{period_options}</select></label><label>From<input name="start" type="date" value="{esc(params.get("start", [""])[0])}"></label><label>To<input name="end" type="date" value="{esc(params.get("end", [""])[0])}"></label><button class="primary">Apply filter</button></form><div class="stats">{metric_cards}</div><div class="admin-grid"><section class="panel"><div class="section-head"><div><p class="eyebrow">TOP PRODUCTS</p><h2>Best sellers</h2></div><a class="under" href="/admin/reports?type=products">View report ↗</a></div><div class="table">{best_rows}</div></section><section class="panel"><p class="eyebrow">PROFIT ESTIMATE</p><h2>{money(revenue)}</h2><p class="note">Estimated from recorded order totals. Add product cost prices and expenses for true net profit.</p><p class="note">Selected period: {esc(period.title())}</p></section></div><section class="panel"><div class="section-head"><div><p class="eyebrow">RECENT ORDERS</p><h2>Order activity</h2></div><a class="primary" href="/admin/orders">Manage orders ↗</a></div><div class="table"><div class="table-row table-head"><span>Order / customer</span><span>Status</span><span>Total</span></div>{recent_rows}</div></section><section class="panel"><div class="section-head"><div><p class="eyebrow">ADMIN ACTIVITY</p><h2>Recent changes</h2></div><a class="under" href="/admin/audit">Open audit log ↗</a></div><div class="table"><div class="table-row table-head"><span>Date</span><span>User</span><span>Activity</span></div>{activity_rows}</div></section></section></div></main>'''
    body = body.replace('<a href="/admin/categories">▤ Categories</a>', '<a href="/admin/categories">▤ Categories</a><a href="/admin/inventory">▥ Inventory</a>')
    body = body.replace('<a href="/admin/deliveries">▰ Deliveries</a>', '<a href="/admin/deliveries">▰ Deliveries</a><a href="/admin/returns">↩ Returns & refunds</a><a href="/admin/notifications">◉ Notifications</a>')
    body = body.replace('<a href="/admin/promotions">◇ Promotions</a>', '<a href="/admin/promotions">◇ Promotions</a><a href="/admin/search">⌕ Search insights</a><a href="/admin/engagement">♡ Wishlist insights</a>')
    body = body.replace('<a href="/admin/reports">◫ Reports</a>', '<a href="/admin/reports">◫ Reports</a><a href="/admin/expenses">▤ Expenses & profit</a>')
    body = body.replace('<a href="/admin/audit">▤ Audit logs</a>', '<a href="/admin/audit">▤ Audit logs</a><a href="/admin/health">◉ System health</a><a href="/admin/security">⌁ Security center</a><a href="/admin/integrations">↗ Integrations</a><a href="/admin/assistant">✦ Business assistant</a>')
    return layout(data, body)

def product_page(data, product, session=None):
    images = product.get('images', [product['image']])
    gallery = ''.join(f'<img src="{esc(image)}" alt="{esc(product["name"])} thumbnail">' for image in images)
    original = f'<del>{money(product["old_price"])}</del>' if product.get('old_price') else ''
    discount = f'<span class="discount">{round((1 - product["price"] / product["old_price"]) * 100)}% off</span>' if product.get('old_price') else ''
    specifications = ''.join(f'<li><b>{esc(key.replace("_", " "))}</b> {esc(value)}</li>' for key, value in product.get('specifications', {}).items())
    variants = [variant for variant in product.get('variants', []) if variant.get('active', True)]
    variant_options = ''.join(f'<option value="{variant.get("id", index)}">{esc(variant.get("name", "Option"))} · {money(variant.get("price", product["price"]))}</option>' for index, variant in enumerate(variants, 1))
    variant_control = f'<label>Choose an option<select name="variant_id">{variant_options}</select></label>' if variants else ''
    purchase = f'''<form method="post"><input type="hidden" name="action" value="cart"><input type="hidden" name="product_id" value="{product['id']}">{variant_control}<label>Quantity<input name="quantity" type="number" value="1" min="1" max="{product['stock']}"></label><div class="detail-actions"><button class="primary" {'disabled' if not product['stock'] else ''}>{'Add to cart ↗' if product['stock'] else 'OUT OF STOCK'}</button><button class="buy-now" name="buy_now" value="1" {'disabled' if not product['stock'] else ''}>Buy now</button></div></form>'''
    customer_name = session.get('customer_name', 'Customer') if session else 'Customer'
    whatsapp_message = f"Is this product available? Product: {product['name']}. Brand: {product.get('brand', '')}. SKU: {product.get('sku', '')}. Price: {money(product['price'])}."
    whatsapp = whatsapp_url(data['shop'].get('whatsapp', ''), whatsapp_message)
    reviews = ''.join(f'<div class="review"><b>{esc(review.get("customer_name", "Customer"))} · {"★" * int(review.get("rating", 5))}</b><p>{esc(review.get("text", ""))}</p></div>' for review in data.get('reviews', []) if review.get('product_id') == product['id'] and review.get('approved', True))
    review_count = len([review for review in data.get('reviews', []) if review.get('product_id') == product['id'] and review.get('approved', True)])
    whatsapp_link = f'<a class="whatsapp-button" href="{esc(whatsapp)}" target="_blank" rel="noopener">◉ Ask about this product on WhatsApp</a>' if whatsapp else '<span class="note">WhatsApp contact is not configured.</span>'
    product_links = f'<form method="post"><input type="hidden" name="action" value="wishlist"><input type="hidden" name="product_id" value="{product["id"]}"><button class="under">Add to wishlist ♡</button></form><a class="under" href="/cart">View shopping bag ↗</a>{whatsapp_link}<a class="under" href="mailto:?subject={esc(product["name"])}">Share product</a>'
    review_form = f'<form method="post" class="review-form"><input type="hidden" name="action" value="review"><input type="hidden" name="product_id" value="{product["id"]}"><select name="rating"><option value="5">★★★★★</option><option value="4">★★★★</option><option value="3">★★★</option><option value="2">★★</option><option value="1">★</option></select><textarea name="text" placeholder="Share your experience" required></textarea><button class="under">Submit review</button></form>'
    related = ''.join(card(item) for item in data.get('products', []) if item.get('id') != product.get('id') and item.get('category') == product.get('category'))
    body = render_template('product.html', {'image': esc(product['image']), 'name': esc(product['name']), 'gallery': gallery, 'category': esc(product['category']), 'brand': esc(product['brand']), 'rating': product['rating'], 'review_count': review_count, 'price': money(product['price']), 'original': original, 'discount': discount, 'description': esc(product.get('description', 'A considered addition to your everyday ritual.')), 'stock_message': 'In stock · ships within 24 hours' if product['stock'] else 'OUT OF STOCK', 'purchase': purchase, 'product_links': product_links, 'specifications': specifications, 'reviews': reviews or '<p class="note">No approved reviews yet.</p>', 'review_form': review_form, 'related_products': related or '<p class="note">Explore the full collection for more considered pieces.</p>'})
    return layout(data, body)

def cart_page(data, session):
    items = []
    for item in session['cart']:
        product = next((p for p in data['products'] if p['id'] == item['id']), None)
        variant = next((v for v in product.get('variants', []) if str(v.get('id')) == str(item.get('variant_id'))) , None) if product and item.get('variant_id') else None
        if product: items.append((product, item['quantity'], variant, item))
    subtotal = sum((variant or {}).get('price', product['price']) * quantity for product, quantity, variant, item in items)
    discount = sum((product.get('old_price', (variant or {}).get('price', product['price'])) - (variant or {}).get('price', product['price'])) * quantity for product, quantity, variant, item in items)
    delivery = float(delivery_quote(data, '', subtotal, session.get('cart', []))['fee']) if items else 0
    total = subtotal + delivery
    rows = ''.join(f'''<div class="checkout-row cart-line"><span>{esc(product['name'])}{f' · {esc(variant.get("name", ""))}' if variant else ''} × {quantity}</span><b>{money((variant or {}).get('price', product['price']) * quantity)}</b><form method="post"><input type="hidden" name="action" value="cart_update"><input type="hidden" name="product_id" value="{product['id']}"><input type="hidden" name="variant_id" value="{item.get('variant_id', '')}"><button name="change" value="-1">−</button><button name="change" value="1">＋</button></form><form method="post"><input type="hidden" name="action" value="cart_remove"><input type="hidden" name="product_id" value="{product['id']}"><button class="under">Remove</button></form></div>''' for product, quantity, variant, item in items)
    whatsapp_text = '; '.join(f'{p["name"]} x {quantity} - {money((variant or {}).get("price", p["price"]) * quantity)}' for p, quantity, variant, item in items)
    whatsapp = urlencode({'phone': data['shop'].get('whatsapp', '').replace('+', ''), 'text': f'Hello, I would like to order: {whatsapp_text}. Order total: {money(total)}. Customer: {session.get("customer_name", "Customer")}'})
    actions = '<a class="primary" href="/checkout">Proceed to checkout ↗</a><a class="whatsapp-button" href="/checkout?payment=whatsapp">ORDER VIA WHATSAPP</a>' if items else '<p class="note">Your bag is empty.</p>'
    body = render_template('cart.html', {'rows': rows or '<p class="empty">Your bag is waiting.</p>', 'subtotal': money(subtotal), 'discount': money(discount), 'delivery': 'FREE' if delivery == 0 else money(delivery), 'total': money(total), 'actions': actions})
    return layout(data, body)

def checkout_page(data, session, message='', whatsapp_order=False):
    items = []
    subtotal = 0
    for item in session.get('cart', []):
        product = next((p for p in data['products'] if p['id'] == item['id']), None)
        variant = next((v for v in product.get('variants', []) if str(v.get('id')) == str(item.get('variant_id'))), None) if product and item.get('variant_id') else None
        if product:
            line_total = (variant or {}).get('price', product['price']) * item['quantity']
            subtotal += line_total
            items.append(f'<div class="checkout-row"><span>{esc(product["name"])} × {item["quantity"]}</span><b>{money(line_total)}</b></div>')
    quote = delivery_quote(data, '', subtotal, session.get('cart', []))
    delivery_fee = float(quote['fee'])
    return layout(data, render_template('checkout.html', {'items': ''.join(items), 'subtotal': money(subtotal), 'delivery': 'FREE' if delivery_fee == 0 else money(delivery_fee), 'total': money(subtotal + delivery_fee), 'message': f'<p class="notice">{esc(message)}</p>' if message else '', 'payment_method_m_pesa': '' if whatsapp_order else 'selected', 'payment_method_whatsapp': 'selected' if whatsapp_order else ''}))
def cart_item_variant(data, item):
    product = next((p for p in data.get('products', []) if p['id'] == item.get('id')), None)
    return next((variant for variant in product.get('variants', []) if str(variant.get('id')) == str(item.get('variant_id'))), None) if product and item.get('variant_id') else None

def cart_item_price(data, item):
    product = next((p for p in data.get('products', []) if p['id'] == item.get('id')), None)
    variant = cart_item_variant(data, item)
    return (variant or {}).get('price', product.get('price', 0)) if product else 0

def coupon_discount(data, coupon, subtotal, cart, customer_id=None):
    if not coupon or not coupon.get('active', True):
        return 0
    expiry = str(coupon.get('expiry_date', '')).strip()
    if expiry and expiry < datetime.now().strftime('%Y-%m-%d'):
        return 0
    if subtotal < float(coupon.get('minimum_order', 0) or 0):
        return 0
    if int(coupon.get('usage_limit', 0) or 0) and int(coupon.get('used', 0) or 0) >= int(coupon.get('usage_limit')):
        return 0
    if customer_id and int(coupon.get('per_customer_limit', 0) or 0):
        used_by_customer = sum(1 for order in data.get('orders', []) if order.get('customer_id') == customer_id and order.get('coupon_code') == coupon.get('code'))
        if used_by_customer >= int(coupon.get('per_customer_limit')):
            return 0
    product_ids = {int(value) for value in coupon.get('product_ids', []) if str(value).isdigit()}
    categories = {str(value).casefold() for value in coupon.get('categories', [])}
    if product_ids and not any(item.get('id') in product_ids for item in cart):
        return 0
    if categories and not any(str(next((product.get('category', '') for product in data.get('products', []) if product.get('id') == item.get('id')), '')).casefold() in categories for item in cart):
        return 0
    value = float(coupon.get('value', 0) or 0)
    discount = subtotal * value / 100 if coupon.get('type', 'percentage') == 'percentage' else value
    maximum = float(coupon.get('maximum_discount', 0) or 0)
    return max(0, min(discount, maximum if maximum else discount, subtotal))

def info_page(data, eyebrow, title, intro, body):
    return layout(data, render_template('info.html', {'eyebrow': esc(eyebrow), 'title': esc(title), 'intro': esc(intro), 'body': body}))

def category_page(data, category, query=''):
    descriptions = {'PERFUMES & FRAGRANCES': 'Scent stories for every mood.', 'SKINCARE': 'Thoughtful rituals for luminous skin.', 'HAIRCARE': 'Care for every texture and ritual.', 'JEWELLERY': 'Quiet details with a lasting point of view.', 'GIFT & LIFESTYLE ITEMS': 'Beautiful gestures for every occasion.', 'KITCHENWARE': 'Elevated essentials for daily living.'}
    normalized_query = query.lower().strip()
    products = [product for product in data.get('products', []) if product.get('category') == category and normalized_query in f'{product.get("name", "")} {product.get("brand", "")} {product.get("sku", "")} {" ".join(product.get("tags", []))}'.lower()]
    pills = ''.join(f'<a class="pill {"active" if item == category else ""}" href="/category/{item.lower().replace(" ", "-")}">{esc(item)}</a>' for item in category_groups(data))
    body = render_template('category.html', {'category': esc(category), 'description': esc(descriptions.get(category, 'Curated essentials for everyday living.')), 'count': len(products), 'query': esc(query), 'pills': pills, 'products': ''.join(card(product) for product in products) or '<p class="empty">Nothing found in this collection.</p>'})
    return layout(data, body)

def search_page(data, query):
    q = query.get('q', [''])[0].strip().lower()
    searchable_pages = (('About us', '/about', data['shop'].get('description', '') + data['shop'].get('story_description', '')), ('Delivery information', '/delivery', data['shop'].get('delivery_information', '')), ('Returns and refunds', '/returns', data['shop'].get('return_policy', '')), ('FAQs', '/faq', 'Frequently asked questions'))
    products = [product for product in data.get('products', []) if q and q in f"{product.get('name', '')} {product.get('brand', '')} {product.get('category', '')} {product.get('subcategory', '')} {product.get('sku', '')} {product.get('description', '')} {' '.join(product.get('tags', []))} {json.dumps(product.get('specifications', {}))} {json.dumps(product.get('variants', []))}".lower()]
    page_results = ''.join(f'<a class="search-result-link" href="{href}"><b>{label}</b><small>Open page ↗</small></a>' for label, href, content in searchable_pages if q in content.lower())
    page_results = f'<section class="search-pages"><p class="eyebrow">WEBSITE PAGES</p>{page_results}</section>' if page_results else ''
    return layout(data, render_template('search.html', {'query': esc(query.get('q', [''])[0]), 'count': len(products), 'results': ''.join(card(product) for product in products) or '<p class="empty">No matching products found.</p>', 'page_results': page_results}))

def record_search(data, query, result_count):
    query = str(query or '').strip().lower()[:120]
    if not query:
        return
    record = data.setdefault('search_terms', {}).setdefault(query, {'count': 0, 'no_results': 0, 'last_searched': ''})
    record['count'] += 1
    if result_count == 0: record['no_results'] += 1
    record['last_searched'] = datetime.now().isoformat(timespec='seconds')

def admin_search(data, message=''):
    terms = data.get('search_terms', {})
    rows = ''.join(f'<div class="table-row"><span>{esc(term)}</span><span>{stats.get("count", 0)}</span><span>{stats.get("no_results", 0)}</span><span>{esc(stats.get("last_searched", ""))}</span></div>' for term, stats in sorted(terms.items(), key=lambda item: item[1].get('count', 0), reverse=True)[:50]) or '<p class="empty">Search activity will appear here.</p>'
    tags = sorted({tag for product in data.get('products', []) for tag in product.get('tags', [])})
    seo_keywords = ', '.join(data.get('shop', {}).get('seo_keywords', [])) if isinstance(data.get('shop', {}).get('seo_keywords', []), list) else data.get('shop', {}).get('seo_keywords', '')
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / SEARCH</p><h1>Search <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Search keywords</h2><div class="table"><div class="table-row table-head"><span>Keyword</span><span>Searches</span><span>No results</span><span>Last searched</span></div>{rows}</div></section><section class="panel"><h2>SEO keywords & product tags</h2><form method="post" class="checkout-form"><input type="hidden" name="action" value="search_settings"><label>SEO keywords<textarea name="seo_keywords">{esc(seo_keywords)}</textarea></label><p class="note">Current product tags: {esc(', '.join(tags) or 'None')}</p><button class="primary">Save search settings</button></form></section></section></main>''')

def categories_page(data):
    body = f'<main class="catalog category-directory"><p class="eyebrow">SHOP BY CATEGORY</p><h1>Find your <em>collection.</em></h1><p class="hero-text">Explore beauty, home, lifestyle, and everyday essentials.</p><div class="category-grid">{shop_categories(data)}</div></main>'
    return layout(data, body)

def order_confirmation_page(data, order_number, whatsapp_order_url=''):
    whatsapp_link = f'<a class="whatsapp-button" href="{esc(whatsapp_order_url)}" target="_blank" rel="noopener">Open WhatsApp to send order ↗</a>' if whatsapp_order_url else ''
    return layout(data, render_template('order-confirmation.html', {'order_number': esc(order_number), 'whatsapp_link': whatsapp_link}))

def receipt_page(data, order):
    shop = data.get('shop', {})
    logo_source = shop.get('logo', '').strip()
    logo_url = quote(logo_source, safe='/:?&=#')
    logo = f'<img class="receipt-logo" src="{esc(logo_url)}" alt="{esc(shop.get("name", "Store"))}">' if logo_source and logo_source_available(logo_source) else ''
    def item_name(item):
        product = next((product for product in data.get('products', []) if product.get('id') == item.get('product_id')), {})
        return product.get('name', f'Product #{item.get("product_id", "")}'), product.get('sku', '')
    item_rows = ''.join(f'<tr><td>{esc(item_name(item)[0])}<small>{esc(item_name(item)[1])}</small></td><td>{item.get("quantity", 1)}</td><td>{money(item.get("unit_price", 0) * item.get("quantity", 1))}</td></tr>' for item in order.get('items', []))
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Receipt {esc(order.get("order_number", ""))}</title><style>@page{{size:80mm auto;margin:0}}*{{box-sizing:border-box}}body{{width:80mm;margin:0;padding:7mm 5mm;font:11px/1.4 Arial,sans-serif;color:#1f2924;background:#fff}}.receipt{{width:100%}}.receipt-head{{text-align:center;border-bottom:1px dashed #777;padding-bottom:10px;margin-bottom:10px}}.receipt-logo{{max-width:42mm;max-height:16mm;object-fit:contain;margin-bottom:5px}}h1{{font-size:17px;margin:2px 0}}.muted{{color:#68736b;font-size:10px}}.meta{{border-bottom:1px dashed #777;padding-bottom:8px;margin-bottom:8px}}.meta p{{margin:2px 0}}table{{width:100%;border-collapse:collapse}}th{{text-align:left;font-size:9px;border-bottom:1px solid #333;padding:3px 0}}td{{padding:5px 0;vertical-align:top;border-bottom:1px dotted #bbb}}th:nth-child(2),td:nth-child(2){{text-align:center;width:12mm}}th:last-child,td:last-child{{text-align:right}}td small{{display:block;color:#68736b;font-size:9px}}.totals{{border-top:1px solid #333;margin-top:8px;padding-top:6px}}.total{{font-size:14px;font-weight:700}}.footer{{border-top:1px dashed #777;margin-top:12px;padding-top:8px;text-align:center;font-size:9px}}.print{{display:block;margin:12px auto;padding:8px 12px;background:#3f5548;color:#fff;border:0}}@media print{{.print{{display:none}}}}</style></head><body><main class="receipt"><header class="receipt-head">{logo}<h1>{esc(shop.get("name", "Luxe Beauty Hub"))}</h1><div class="muted">{esc(shop.get("tagline", ""))}</div><div class="muted">{esc(shop.get("phone", ""))} · {esc(shop.get("email", ""))}</div></header><section class="meta"><p><b>Receipt:</b> {esc(order.get("order_number", ""))}</p><p><b>Date:</b> {esc(order.get("created_at", ""))}</p><p><b>Status:</b> {esc(order.get("status", "Pending"))}</p><p><b>Payment:</b> {esc(order.get("payment_method", ""))} · {esc(order.get("payment_status", "Pending"))}</p>{f'<p><b>M-Pesa:</b> {esc(order.get("mpesa_phone", ""))}</p>' if order.get("mpesa_phone") else ''}</section><section class="meta"><p><b>Customer:</b> {esc(order.get("customer_name", "Guest"))}</p><p>{esc(order.get("email", ""))}</p><p>{esc(order.get("phone", ""))}</p><p>{esc(order.get("address", ""))}, {esc(order.get("location", ""))}</p></section><table><thead><tr><th>Item</th><th>Qty</th><th>Amount</th></tr></thead><tbody>{item_rows or '<tr><td colspan="3">Order details available in admin.</td></tr>'}</tbody></table><section class="totals"><p>Subtotal <span style="float:right">{money(order.get("subtotal", order.get("total", 0)))}</span></p><p>Delivery <span style="float:right">{money(order.get("delivery_fee", 0))}</span></p><p class="total">TOTAL <span style="float:right">{money(order.get("total", 0))}</span></p></section><footer class="footer">Thank you for shopping with us.<br>{esc(shop.get("location", ""))}</footer><button class="print" onclick="window.print()">Print receipt</button></main></body></html>'''

def receipt_pdf(data, order, document_label='Receipt'):
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('ReceiptTitle', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#1f2924'), alignment=1, spaceAfter=4)
    center_style = ParagraphStyle('ReceiptCenter', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#68736b'), alignment=1, leading=12)
    small_style = ParagraphStyle('ReceiptSmall', parent=styles['Normal'], fontSize=9, leading=12)
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=f'{document_label} {order.get("order_number", "")}')
    shop = data.get('shop', {})
    story = []
    logo_source = shop.get('logo', '').strip()
    if logo_source.startswith('/uploads/') and logo_source_available(logo_source):
        logo_path = ROOT / 'uploads' / Path(unquote(logo_source).removeprefix('/uploads/')).name
        logo_image = ReportLabImage(str(logo_path), width=42 * mm, height=16 * mm, kind='proportional')
        logo_image.hAlign = 'CENTER'
        story.append(logo_image)
    story.extend([Paragraph(esc(shop.get('name', 'Luxe Beauty Hub')), title_style), Paragraph(esc(shop.get('tagline', '')), center_style), Paragraph(esc(f'{shop.get("phone", "")} · {shop.get("email", "")}'), center_style), Spacer(1, 8)])
    metadata = [[document_label, order.get('invoice_number' if document_label == 'Invoice' else 'receipt_number', order.get('order_number', ''))], ['Order', order.get('order_number', '')], ['Date', order.get('created_at', '')], ['Status', order.get('status', 'Pending')], ['Payment', f'{order.get("payment_method", "")} · {order.get("payment_status", "Pending")}'], ['Customer', order.get('customer_name', '')], ['Delivery', f'{order.get("address", "")}, {order.get("location", "")}']]
    story.append(Table([[Paragraph(esc(str(key)), small_style), Paragraph(esc(str(value)), small_style)] for key, value in metadata], colWidths=[28 * mm, 134 * mm], style=TableStyle([('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#d6ddd7')), ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f1f4f0')), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('PADDING', (0, 0), (-1, -1), 6)])))
    story.append(Spacer(1, 12))
    rows = [[Paragraph('<b>Item</b>', small_style), Paragraph('<b>Qty</b>', small_style), Paragraph('<b>Amount</b>', small_style)]]
    for item in order.get('items', []):
        product = next((product for product in data.get('products', []) if product.get('id') == item.get('product_id')), {})
        name = product.get('name', f'Product #{item.get("product_id", "")}')
        sku = product.get('sku', '')
        rows.append([Paragraph(esc(f'{name} ({sku})'), small_style), str(item.get('quantity', 1)), money(item.get('unit_price', 0) * item.get('quantity', 1))])
    story.append(Table(rows, colWidths=[112 * mm, 18 * mm, 32 * mm], style=TableStyle([('LINEBELOW', (0, 0), (-1, 0), 0.7, colors.HexColor('#3f5548')), ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#d6ddd7')), ('ALIGN', (1, 1), (-1, -1), 'RIGHT'), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('PADDING', (0, 0), (-1, -1), 6)])))
    totals = [['Subtotal', money(order.get('subtotal', 0))], ['Discount', f'- {money(order.get("discount", 0))}'], ['Delivery', money(order.get('delivery_fee', 0))], ['Total', money(order.get('total', 0))]]
    story.extend([Spacer(1, 10), Table(totals, colWidths=[130 * mm, 32 * mm], style=TableStyle([('LINEABOVE', (0, 0), (-1, 0), 0.7, colors.HexColor('#3f5548')), ('ALIGN', (1, 0), (-1, -1), 'RIGHT'), ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), ('FONTSIZE', (0, -1), (-1, -1), 12), ('PADDING', (0, 0), (-1, -1), 6)])), Spacer(1, 18), Paragraph(esc(f'Thank you for shopping with us. {shop.get("location", "")}'), center_style)])
    document.build(story)
    return output.getvalue()

def tracking_page(data, session, order_number=''):
    orders = [order for order in data.get('orders', []) if order.get('customer_id') == session.get('customer_id')]
    order = next((item for item in orders if item.get('order_number') == order_number), None) if order_number else (orders[-1] if orders else None)
    if not order:
        return info_page(data, 'ORDER TRACKING', 'Your orders, all in one place.', 'Sign in to view the live status of your Luxe order.', '<a class="primary" href="/login">Sign in to continue ↗</a>')
    delivery_status = order.get('delivery_status', order.get('status', 'Pending'))
    delivered_at = f'<div class="checkout-row"><span>Delivered</span><b>{esc(order.get("delivered_at", ""))}</b></div>' if order.get('delivered_at') else ''
    body = f'<div class="tracking-card"><div class="checkout-row"><span>Order</span><b>{esc(order.get("order_number", ""))}</b></div><div class="checkout-row"><span>Order status</span><b>{esc(order.get("status", "Pending"))}</b></div><div class="checkout-row"><span>Delivery status</span><b>{esc(delivery_status)}</b></div>{delivered_at}<div class="checkout-row"><span>Total</span><b>{money(order.get("total", 0))}</b></div><div class="checkout-row"><span>Delivery</span><b>{esc(order.get("location", order.get("address", "")))}</b></div></div>'
    return info_page(data, 'ORDER TRACKING', 'Your order is on its way.', 'Follow each step from confirmation to delivery.', body)

def login_page(data, error=''):
    error_html = f'<p class="notice">{esc(error)}</p>' if error else ''
    body = render_template('login.html', {'shop_name': esc(data['shop']['name']), 'error': error_html})
    return layout(data, body)

def register_page(data, error=''):
    error_html = f'<p class="notice">{esc(error)}</p>' if error else ''
    body = render_template('register.html', {'shop_name': esc(data['shop']['name']), 'error': error_html})
    return layout(data, body)

def current_account(data, session):
    email = session.get('email', '').lower()
    records = data.get('users', []) + data.get('customers', [])
    return next((record for record in records if record.get('email', '').lower() == email), None)

def account_page(data, session, message='', orders_view=False):
    account = current_account(data, session)
    if not account: return login_page(data, 'Please sign in to view your account.')
    orders = [o for o in data.get('orders', []) if o.get('customer_id') == account.get('id')]
    order_rows = ''.join(f'<div class="checkout-row"><span><a class="under" href="/tracking?order={esc(o["order_number"])}">{esc(o["order_number"])}</a><small class="muted-line">{esc(o["status"])} · {esc(o["created_at"])}</small><a class="under" href="/receipt?order={esc(o["order_number"])}">Download receipt</a></span><b>{money(o["total"])}</b></div>' for o in orders) or '<p class="empty">No orders yet.</p>'
    notice = f'<p class="notice">{esc(message)}</p>' if message else ''
    profile_class = 'active' if not orders_view else ''
    orders_class = 'active' if orders_view else ''
    orders_heading = 'My orders' if orders_view else 'Order tracking'
    return layout(data, f'''<main class="account-page"><section><p class="eyebrow">MY ACCOUNT</p><h1>Hello, <em>{esc(account.get('name', 'Customer'))}.</em></h1>{notice}<div class="account-nav"><a class="{profile_class}" href="/account">Profile</a><a class="{orders_class}" href="/account/orders#orders">My orders</a><a href="/wishlist">Wishlist</a><a href="/logout">Sign out</a></div><div class="account-grid"><section class="panel"><h2>Your details</h2><form method="post" class="checkout-form"><input type="hidden" name="action" value="account_update"><label>Name<input name="name" value="{esc(account.get('name', ''))}" required></label><label>Email<input value="{esc(account.get('email', ''))}" type="email" readonly></label><label>Phone<input name="phone" value="{esc(account.get('phone', ''))}"></label><button class="primary">Save details</button></form></section><section class="panel"><h2>Change password</h2><form method="post" class="checkout-form"><input type="hidden" name="action" value="account_password"><label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label><label>New password<input name="new_password" type="password" minlength="8" autocomplete="new-password" required></label><label>Confirm new password<input name="confirm_password" type="password" minlength="8" autocomplete="new-password" required></label><button class="primary">Change password</button></form></section></div><div class="panel account-orders" id="orders"><h2>{orders_heading}</h2><p class="hero-text">Track every order from confirmation to delivery.</p>{order_rows}</div></section></main>''')

def admin_orders(data, message=''):
    rows = ''.join(f'''<div class="table-row"><span><b>{esc(order['order_number'])}</b><small>{esc(order.get('customer_name', 'Guest'))} · {esc(order.get('email', ''))}</small><small>{esc(order.get('phone', ''))} · {esc(order.get('location', ''))}</small><small>{esc(order.get('address', ''))}</small>{f'<small>M-Pesa: {esc(order.get("mpesa_phone", ""))}</small>' if order.get('mpesa_phone') else ''}<a class="under" href="/receipt?order={esc(order['order_number'])}">Download receipt</a></span><span>{money(order['total'])}</span><span>{esc(order.get('payment_method', 'Pending'))}<small>{esc(order.get('payment_status', 'Pending'))}</small></span><span><form method="post"><input type="hidden" name="action" value="order_status"><input type="hidden" name="order_number" value="{esc(order['order_number'])}"><select name="status">{''.join(f'<option {"selected" if status == order.get("status") else ""}>{status}</option>' for status in ORDER_STATUSES)}</select><button>Save</button></form></span></div>''' for order in reversed(data.get('orders', []))) or '<p class="empty">No orders have been placed yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content"><p class="eyebrow">ADMIN PANEL / ORDERS</p><h1>Order <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><p class="note">Customer contact, M-Pesa number, delivery address, payment method, and payment status are shown on each order.</p><div class="table"><div class="table-row table-head"><span>Order / Customer</span><span>Total</span><span>Payment</span><span>Order status</span></div>{rows}</div></section></section></main>''')

def promotions_page(data, message=''):
    coupons = ''.join(f'''<div class="checkout-row"><span><b>{esc(c["code"])}</b> · {c["type"]}<small class="muted-line">{c.get("used", 0)} / {c.get("usage_limit", "∞")} uses · {"Active" if c.get("active", True) else "Disabled"}</small></span><b>{c["value"]}% off</b><span><form method="post" class="inline-form"><input type="hidden" name="action" value="coupon_toggle"><input type="hidden" name="code" value="{esc(c["code"])}"><button>{"Disable" if c.get("active", True) else "Activate"}</button></form><form method="post" class="inline-form"><input type="hidden" name="action" value="coupon_delete"><input type="hidden" name="code" value="{esc(c["code"])}"><button onclick="return confirm('Delete this coupon?')">Delete</button></form></span></div>''' for c in data.get('coupons', []))
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / MARKETING</p><h1>Coupons & <em>promotions.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Active coupons</h2>{coupons or '<p class="empty">No coupons yet.</p>'}<form method="post" class="checkout-form"><input type="hidden" name="action" value="coupon"><label>Coupon code<input name="code" placeholder="SAVE20" required></label><label>Percentage discount<input name="value" type="number" min="1" max="100" required></label><label>Usage limit<input name="usage_limit" type="number" min="1" value="100" required></label><button class="primary">Create coupon ↗</button></form></section></section></main>''')

def promotions_manager(data, message=''):
    coupons = ''.join(f'<div class="table-row"><span><b>{esc(c.get("code", ""))}</b><small>{esc(c.get("type", "percentage"))} · {esc(c.get("expiry_date", "No expiry"))}</small></span><span>{money(c.get("value", 0)) if c.get("type") == "fixed" else f"{c.get("value", 0)}%"}</span><span>{c.get("used", 0)} / {c.get("usage_limit", "∞")}</span><span>{"Active" if c.get("active", True) else "Disabled"}</span><span><form method="post" class="inline-form"><input type="hidden" name="action" value="coupon_toggle"><input type="hidden" name="code" value="{esc(c.get("code", ""))}"><button>{"Disable" if c.get("active", True) else "Activate"}</button></form><form method="post" class="inline-form"><input type="hidden" name="action" value="coupon_delete"><input type="hidden" name="code" value="{esc(c.get("code", ""))}"><button>Delete</button></form></span></div>' for c in data.get('coupons', [])) or '<p class="empty">No coupons yet.</p>'
    campaigns = ''.join(f'<div class="table-row"><span><b>{esc(campaign.get("name", ""))}</b><small>{esc(campaign.get("type", "Promotion"))}</small></span><span>{esc(campaign.get("starts_at", ""))} to {esc(campaign.get("ends_at", ""))}</span><span>{"Active" if campaign.get("active", True) else "Disabled"}</span></div>' for campaign in data.get('campaigns', [])) or '<p class="empty">No campaigns yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / MARKETING</p><h1>Coupons & <em>promotions.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Coupons</h2><div class="table"><div class="table-row table-head"><span>Code</span><span>Value</span><span>Usage</span><span>Status</span><span>Actions</span></div>{coupons}</div><h3>Create coupon</h3><form method="post" class="checkout-form"><input type="hidden" name="action" value="coupon"><label>Coupon code<input name="code" placeholder="LUXE20" required></label><label>Type<select name="coupon_type"><option value="percentage">Percentage</option><option value="fixed">Fixed amount</option></select></label><label>Value<input name="value" type="number" min="0.01" step="0.01" required></label><label>Minimum order (KSh)<input name="minimum_order" type="number" min="0" step="0.01" value="0"></label><label>Maximum discount (KSh)<input name="maximum_discount" type="number" min="0" step="0.01" value="0"></label><label>Expiry date<input name="expiry_date" type="date"></label><label>Usage limit<input name="usage_limit" type="number" min="0" value="100"></label><label>Per-customer limit<input name="per_customer_limit" type="number" min="0" value="0"></label><label>Categories<input name="coupon_categories" placeholder="Perfume, Skincare"></label><label>Product IDs<input name="coupon_products" placeholder="1, 2"></label><button class="primary">Create coupon</button></form></section><section class="panel"><h2>Campaigns</h2><div class="table"><div class="table-row table-head"><span>Campaign</span><span>Dates</span><span>Status</span></div>{campaigns}</div><form method="post" class="checkout-form"><input type="hidden" name="action" value="campaign_create"><label>Campaign name<input name="campaign_name" placeholder="Weekend sale" required></label><label>Type<select name="campaign_type"><option>Flash sale</option><option>Weekend sale</option><option>Black Friday</option><option>Valentine's promotion</option><option>Christmas promotion</option><option>Buy 1 Get 1</option><option>Bundle discount</option><option>Free delivery promotion</option></select></label><label>Starts<input name="campaign_starts" type="datetime-local" required></label><label>Ends<input name="campaign_ends" type="datetime-local" required></label><button class="primary">Create campaign</button></form></section></section></main>''')

def admin_reviews(data, message=''):
    rows = ''.join(f'''<div class="review admin-review"><b>{esc(review.get('customer_name', 'Customer'))} · {'★' * int(review.get('rating', 5))}</b><p>{esc(review.get('text', ''))}</p><small>{esc(review.get('created_at', ''))}</small><form method="post"><input type="hidden" name="action" value="review_moderate"><input type="hidden" name="review_id" value="{review['id']}"><button name="decision" value="approve">Approve</button><button name="decision" value="hide">Hide</button><button name="decision" value="delete">Delete</button></form></div>''' for review in data.get('reviews', [])) or '<p class="empty">No reviews waiting for moderation.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PRODUCTS / REVIEWS</p><h1>Review <em>moderation.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel">{rows}</section></section></main>''')

def reviews_manager(data, message=''):
    reviews = data.get('reviews', [])
    approved = [review for review in reviews if review.get('approved') and not review.get('hidden')]
    average = sum(float(review.get('rating', 0)) for review in approved) / len(approved) if approved else 0
    product_counts = {}
    for review in reviews: product_counts[review.get('product_id')] = product_counts.get(review.get('product_id'), 0) + 1
    rows = ''.join(f'<div class="review admin-review"><b>{esc(review.get("customer_name", "Customer"))} · {"★" * int(review.get("rating", 5))}</b><p>{esc(review.get("text", ""))}</p><small>{esc(review.get("created_at", ""))} · {"Approved" if review.get("approved") and not review.get("hidden") else "Pending/hidden"}</small><form method="post"><input type="hidden" name="action" value="review_moderate"><input type="hidden" name="review_id" value="{review.get("id", 0)}"><button name="decision" value="approve">Approve</button><button name="decision" value="reject">Reject</button><button name="decision" value="hide">Hide</button><button name="decision" value="delete">Delete</button></form></div>' for review in reversed(reviews)) or '<p class="empty">No reviews yet.</p>'
    stats = f'<div class="stats"><div><span>Total reviews</span><b>{len(reviews)}</b></div><div><span>Pending</span><b>{sum(1 for review in reviews if not review.get("approved") and not review.get("hidden"))}</b></div><div><span>Average rating</span><b>{average:.1f}</b></div><div><span>Most reviewed product</span><b>{max(product_counts.values(), default=0)}</b></div></div>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / REVIEWS</p><h1>Review <em>moderation.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}{stats}<section class="panel">{rows}</section></section></main>''')

def admin_engagement(data):
    product_counts = {}
    customer_interest = {}
    for customer_id, product_ids in data.get('wishlists', {}).items():
        customer_interest[customer_id] = len(product_ids)
        for product_id in product_ids: product_counts[product_id] = product_counts.get(product_id, 0) + 1
    products = {product.get('id'): product for product in data.get('products', [])}
    rows = ''.join(f'<div class="table-row"><span>{esc(products.get(product_id, {}).get("name", f"Product #{product_id}"))}</span><span>{count}</span><span>{money(products.get(product_id, {}).get("price", 0))}</span></div>' for product_id, count in sorted(product_counts.items(), key=lambda item: item[1], reverse=True)) or '<p class="empty">Wishlist activity will appear here.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / CUSTOMER ENGAGEMENT</p><h1>Wishlist <em>insights.</em></h1><section class="panel"><p class="hero-text">{len(product_counts)} products have been wishlisted by {len(customer_interest)} customers.</p><div class="table"><div class="table-row table-head"><span>Product</span><span>Wishlist additions</span><span>Price</span></div>{rows}</div></section></section></main>''')

def admin_customers(data, message=''):
    rows = ''
    for customer in data.get('customers', []):
        order_count = len([order for order in data.get('orders', []) if order.get('customer_id') == customer['id']])
        status = 'Active' if customer.get('active', True) else 'Disabled'
        action = 'Disable' if customer.get('active', True) else 'Activate'
        rows += f'<div class="table-row"><span><b>{esc(customer["name"])}</b><small>{esc(customer["email"])} · {esc(customer.get("phone", ""))}</small></span><span>{order_count} orders</span><span>{status}</span><span><form method="post"><input type="hidden" name="action" value="customer_toggle"><input type="hidden" name="customer_id" value="{customer["id"]}"><button>{action}</button></form></span></div>'
    rows = rows or '<p class="empty">No customers yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / CUSTOMERS</p><h1>Customer <em>directory.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><div class="table"><div class="table-row table-head"><span>Customer</span><span>Orders</span><span>Status</span><span>Action</span></div>{rows}</div></section></section></main>''')

def admin_customers(data, message=''):
    rows = ''
    for customer in data.get('customers', []):
        orders = [order for order in data.get('orders', []) if order.get('customer_id') == customer.get('id')]
        spending = sum(float(order.get('total', 0) or 0) for order in orders)
        last_order = max((str(order.get('created_at', '')) for order in orders), default='Never')
        segment = 'VIP' if spending >= 50000 else 'Returning' if len(orders) > 1 else 'New'
        rows += f'<div class="table-row"><span><b>{esc(customer.get("name", ""))}</b><small>{esc(customer.get("email", ""))} · {esc(customer.get("phone", ""))}</small></span><span>{len(orders)} orders</span><span>{money(spending)}</span><span>{esc(last_order)}</span><span>{segment} · {"Active" if customer.get("active", True) else "Disabled"}</span><span><form method="post"><input type="hidden" name="action" value="customer_toggle"><input type="hidden" name="customer_id" value="{customer.get("id")}"><button>{"Disable" if customer.get("active", True) else "Activate"}</button></form></span></div>'
    rows = rows or '<p class="empty">No customers yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / CUSTOMERS</p><h1>Customer <em>directory.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><div class="table"><div class="table-row table-head"><span>Customer</span><span>Orders</span><span>Spending</span><span>Last order</span><span>Segment/status</span><span>Action</span></div>{rows}</div></section></section></main>''')

def admin_payments(data, message=''):
    rows = ''.join(f'<div class="table-row"><span>{esc(order.get("order_number", ""))}<small>{esc(order.get("customer_name", "Guest"))} · {esc(order.get("email", ""))}</small><small>{esc(order.get("phone", ""))} · {esc(order.get("mpesa_phone", ""))}</small><a class="under" href="/receipt?order={esc(order.get("order_number", ""))}">Download receipt</a></span><span>{money(order.get("total", 0))}</span><span>{esc(order.get("payment_method", "Pending"))}</span><span><form method="post"><input type="hidden" name="action" value="payment_status"><input type="hidden" name="order_number" value="{esc(order["order_number"])}"><select name="payment_status"><option>Pending</option><option>Paid</option><option>Failed</option><option>Refunded</option></select><button>Save</button></form></span></div>' for order in reversed(data.get('orders', []))) or '<p class="empty">No payments yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PAYMENTS</p><h1>Payment <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><div class="table"><div class="table-row table-head"><span>Order</span><span>Total</span><span>Method</span><span>Status</span></div>{rows}</div></section></section></main>''')

def admin_deliveries(data, message=''):
    rows = ''.join(f'<div class="table-row"><span>{esc(order.get("order_number", ""))}<small>{esc(order.get("customer_name", "Guest"))} · {esc(order.get("location", ""))}</small></span><span>{esc(order.get("address", ""))}</span><span><form method="post"><input type="hidden" name="action" value="delivery_status"><input type="hidden" name="order_number" value="{esc(order["order_number"])}"><select name="delivery_status"><option>Pending</option><option>Ready for Delivery</option><option>Shipped</option><option>Delivered</option><option>Returned</option></select><button>Save</button></form></span></div>' for order in reversed(data.get('orders', []))) or '<p class="empty">No deliveries yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / DELIVERIES</p><h1>Delivery <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><div class="table"><div class="table-row table-head"><span>Order</span><span>Address</span><span>Status</span></div>{rows}</div></section></section></main>''')

def admin_returns(data, message=''):
    returns = ''.join(f'<div class="table-row"><span><b>{esc(item.get("id", ""))}</b><small>Order {esc(item.get("order_number", ""))} · {esc(item.get("customer_name", "Customer"))}</small></span><span>{esc(item.get("reason", ""))}<small>Product: {esc(item.get("product_name", "All items"))}</small></span><span>{esc(item.get("status", "Requested"))}</span><span><form method="post" class="inline-form"><input type="hidden" name="action" value="return_status"><input type="hidden" name="return_id" value="{esc(item.get("id", ""))}"><select name="return_status"><option>Requested</option><option>Approved</option><option>Rejected</option><option>Received</option><option>Refunded</option></select><button>Save</button></form></span></div>' for item in reversed(data.get('returns', []))) or '<p class="empty">No return requests yet.</p>'
    refunds = ''.join(f'<div class="table-row"><span><b>{esc(item.get("reference", ""))}</b><small>Order {esc(item.get("order_number", ""))}</small></span><span>{money(item.get("amount", 0))}</span><span>{esc(item.get("method", ""))}</span><span>{esc(item.get("status", "Pending"))}<small>{esc(item.get("reason", ""))}</small></span></div>' for item in reversed(data.get('refunds', []))) or '<p class="empty">No refunds recorded.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / RETURNS & REFUNDS</p><h1>Returns <em>and refunds.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Return requests</h2><div class="table"><div class="table-row table-head"><span>Request</span><span>Reason / product</span><span>Status</span><span>Action</span></div>{returns}</div><h3>Create return request</h3><form method="post" class="checkout-form"><input type="hidden" name="action" value="return_request"><label>Order number<input name="order_number" required></label><label>Product ID (optional)<input name="product_id" type="number" min="1"></label><label>Reason<textarea name="return_reason" required></textarea></label><button class="primary">Create return request</button></form></section><section class="panel"><h2>Refunds</h2><div class="table"><div class="table-row table-head"><span>Reference</span><span>Amount</span><span>Method</span><span>Status</span></div>{refunds}</div><h3>Record refund</h3><form method="post" class="checkout-form"><input type="hidden" name="action" value="refund_create"><label>Order number<input name="order_number" required></label><label>Amount (KSh)<input name="refund_amount" type="number" min="0.01" step="0.01" required></label><label>Reason<textarea name="refund_reason" required></textarea></label><button class="primary">Record pending refund</button></form></section></section></main>''')

def admin_notifications(data, message=''):
    channels = data.get('shop', {}).get('notification_channels', ['dashboard', 'email'])
    rows = ''.join(f'<div class="table-row"><span><b>{esc(item.get("title", "Notification"))}</b><small>{esc(item.get("created_at", ""))} · {esc(item.get("type", ""))}</small></span><span>{esc(item.get("message", ""))}</span><span>{"Read" if item.get("read") else "Unread"}</span></div>' for item in reversed(data.get('notifications', [])[-50:])) or '<p class="empty">No notifications yet.</p>'
    options = ''.join(f'<label><input type="checkbox" name="notification_channel" value="{channel}" {"checked" if channel in channels else ""}> {channel.title()}</label>' for channel in ('dashboard', 'email', 'sms', 'whatsapp'))
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / NOTIFICATIONS</p><h1>Alerts & <em>notifications.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Notification channels</h2><form method="post" class="checkout-form"><input type="hidden" name="action" value="notification_settings">{options}<button class="primary">Save channels</button></form><p class="note">Events are stored on the dashboard. Email requires SMTP configuration; SMS and WhatsApp require a provider integration.</p></section><section class="panel"><h2>Recent alerts</h2><div class="table"><div class="table-row table-head"><span>Alert</span><span>Message</span><span>Status</span></div>{rows}</div></section></section></main>''')

def admin_expenses(data, message=''):
    expenses = data.get('expenses', [])
    total = sum(float(item.get('amount', 0) or 0) for item in expenses)
    rows = ''.join(f'<div class="table-row"><span>{esc(item.get("date", ""))}</span><span>{esc(item.get("category", "Other"))}</span><span>{esc(item.get("description", ""))}</span><span>{money(float(item.get("amount", 0)))}</span><span><form method="post" class="inline-form"><input type="hidden" name="action" value="expense_delete"><input type="hidden" name="expense_id" value="{esc(item.get("id", ""))}"><button>Delete</button></form></span></div>' for item in reversed(expenses)) or '<p class="empty">No expenses recorded.</p>'
    categories = ('Stock purchases', 'Delivery', 'Advertising', 'Packaging', 'Salaries', 'Rent', 'Internet', 'Other')
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / FINANCE</p><h1>Expenses & <em>profit.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><h2>Recorded expenses</h2><p class="hero-text">Total expenses: {money(total)}</p><div class="table"><div class="table-row table-head"><span>Date</span><span>Category</span><span>Description</span><span>Amount</span><span></span></div>{rows}</div></section><section class="panel"><h2>Add expense</h2><form method="post" class="checkout-form"><input type="hidden" name="action" value="expense_create"><label>Date<input name="expense_date" type="date" value="{datetime.now():%Y-%m-%d}" required></label><label>Category<select name="expense_category">{"".join(f"<option>{category}</option>" for category in categories)}</select></label><label>Description<input name="expense_description" required></label><label>Amount (KSh)<input name="expense_amount" type="number" min="0.01" step="0.01" required></label><button class="primary">Record expense</button></form></section></section></main>''')

def admin_audit_logs(data):
    rows = ''.join(f'<div class="table-row"><span>{esc(log.get("date", ""))}<small>{esc(log.get("ip_address", ""))}</small></span><span>{esc(log.get("user", "Admin"))}</span><span>{esc(log.get("action", ""))}<small>{esc(log.get("entity", ""))} {esc(log.get("entity_id", ""))}</small></span><span>{esc(log.get("description", ""))}<small>Old: {esc(log.get("old_value", ""))}</small><small>New: {esc(log.get("new_value", ""))}</small></span></div>' for log in reversed(data.get('audit_logs', []))) or '<p class="empty">No audit events recorded.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / SECURITY</p><h1>Audit <em>logs.</em></h1><p class="hero-text">Every operational change records who acted, what changed, the affected record, source IP, and time.</p><section class="panel"><div class="table"><div class="table-row table-head"><span>Date / IP</span><span>User</span><span>Action / Record</span><span>Description / Values</span></div>{rows}</div></section></section></main>''')

def system_health(data):
    disk = shutil.disk_usage(ROOT)
    latest = (database_health(data).get('latest_backup') or {}).get('name', 'Never')
    smtp = data.get('integrations', {}).get('smtp_host') or os.environ.get('SMTP_HOST', '')
    mpesa = data.get('integrations', {}).get('mpesa_consumer_key') or os.environ.get('MPESA_CONSUMER_KEY', '')
    return {'Application': 'Healthy', 'Database': 'Connected' if data.get('shop') is not None else 'Unavailable', 'M-Pesa API': 'Configured' if mpesa else 'Not configured', 'Email / SMTP': 'Configured' if smtp else 'Not configured', 'Storage': f'{disk.free / (1024 ** 3):.1f} GB free of {disk.total / (1024 ** 3):.1f} GB', 'Database size': f'{len(json.dumps(data, default=str)) / 1024:.1f} KB state', 'Server': 'Running', 'Last backup': latest, 'Failed jobs': len(data.get('failed_jobs', [])), 'Error logs': len(data.get('error_logs', []))}

def admin_health(data):
    checks = system_health(data)
    rows = ''.join(f'<div class="table-row"><span>{esc(key)}</span><span class="health-value">{esc(value)}</span></div>' for key, value in checks.items())
    errors = ''.join(f'<div class="table-row"><span>{esc(item.get("created_at", ""))}</span><span>{esc(item.get("message", ""))}</span></div>' for item in reversed(data.get('error_logs', [])[-30:])) or '<p class="empty">No application errors recorded.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / SYSTEM</p><h1>System <em>health.</em></h1><section class="panel health-grid">{rows}</section><section class="panel"><h2>Error logs</h2><div class="table"><div class="table-row table-head"><span>Time</span><span>Message</span></div>{errors}</div></section></section></main>''')

def admin_security(data):
    login_events = [log for log in data.get('audit_logs', []) if log.get('action') in ('admin_login', 'login', 'logout', 'password_changed')]
    rows = ''.join(f'<div class="table-row"><span>{esc(log.get("date", ""))}</span><span>{esc(log.get("user", ""))}</span><span>{esc(log.get("action", ""))}</span><span>{esc(log.get("ip_address", ""))}</span></div>' for log in reversed(login_events[-50:])) or '<p class="empty">No login activity recorded.</p>'
    active = sum(1 for item in SESSIONS.values() if time.time() - item.get('last_seen', 0) <= SESSION_IDLE_SECONDS)
    failed = len([log for log in data.get('audit_logs', []) if log.get('action') == 'login_failed'])
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / SECURITY</p><h1>Security <em>center.</em></h1><div class="stats"><div><span>Active sessions</span><b>{active}</b></div><div><span>Failed logins</span><b>{failed}</b></div><div><span>2FA / MFA</span><b>Available for API settings</b></div><div><span>Role enforcement</span><b>Enabled</b></div></div><section class="panel"><h2>Login and security history</h2><div class="table"><div class="table-row table-head"><span>Date</span><span>User</span><span>Action</span><span>IP</span></div>{rows}</div></section><section class="panel"><h2>Security controls</h2><p class="note">Admin settings MFA, secure production cookies, role permissions, encrypted API settings, CSRF protection, rate limiting, and audit logging are enabled.</p><a class="primary" href="/admin/audit">Open complete audit log</a></section></section></main>''')

def admin_integrations(data):
    integrations = data.setdefault('integrations', {})
    services = [('M-Pesa', 'mpesa_environment', 'Payments and STK Push'), ('SMTP', 'smtp_host', 'Email and invoices'), ('WhatsApp', 'whatsapp', 'Customer messaging'), ('Google Analytics', 'google_analytics_id', 'Traffic analytics'), ('Meta Pixel', 'meta_pixel_id', 'Campaign attribution'), ('Google Search Console', 'search_console_verification', 'Search verification'), ('SMS provider', 'sms_provider', 'SMS notifications'), ('Delivery provider', 'delivery_provider', 'Courier integrations'), ('Cloud storage', 'cloud_storage', 'Media and backup storage')]
    rows = ''.join(f'<div class="table-row"><span><b>{label}</b><small>{description}</small></span><span>{"Configured" if integrations.get(key) or data.get("shop", {}).get(key) else "Not configured"}</span></div>' for label, key, description in services)
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / INTEGRATIONS</p><h1>Connected <em>services.</em></h1><p class="hero-text">Configuration is stored in the existing protected integration settings. Provider secrets are never displayed here.</p><section class="panel"><div class="table"><div class="table-row table-head"><span>Service</span><span>Status</span></div>{rows}</div></section><a class="primary" href="/admin/settings">Configure integrations</a></section></main>''')

def assistant_answer(data, question):
    question = question.casefold()
    if 'revenue' in question or 'sales' in question:
        total = sum(float(order.get('total', 0) or 0) for order in data.get('orders', []) if order.get('status') != 'Cancelled')
        return f'Recorded revenue is {money(total)} across {len(data.get("orders", []))} orders.'
    if 'profit' in question or 'margin' in question:
        revenue = sum(float(order.get('total', 0) or 0) for order in data.get('orders', []) if order.get('status') != 'Cancelled')
        costs = sum(float(product.get('cost_price', 0) or 0) * int(item.get('quantity', 0)) for order in data.get('orders', []) for item in order.get('items', []) for product in data.get('products', []) if product.get('id') == item.get('product_id'))
        expenses = sum(float(item.get('amount', 0) or 0) for item in data.get('expenses', []))
        return f'Estimated profit is {money(revenue - costs - expenses)} after recorded product costs and expenses.'
    orders = data.get('orders', [])
    if 'revenue yesterday' in question:
        day = (datetime.now() - timedelta(days=1)).date().isoformat()
        return money(sum(float(order.get('total', 0) or 0) for order in orders if str(order.get('created_at', '')).startswith(day)))
    if 'run out' in question:
        products = [product.get('name', '') for product in data.get('products', []) if product.get('stock', 0) <= product.get('minimum_stock', 5)]
        return ', '.join(products) or 'No products are currently below their stock thresholds.'
    if 'best-selling' in question or 'best selling' in question:
        counts = {}
        for order in orders:
            for item in order.get('items', []): counts[item.get('product_id')] = counts.get(item.get('product_id'), 0) + item.get('quantity', 0)
        products = {product.get('id'): product.get('name', '') for product in data.get('products', [])}
        return ', '.join(f'{products.get(product_id, "Product")} ({quantity})' for product_id, quantity in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:5]) or 'There are no sales to analyze yet.'
    if '90 days' in question or 'inactive' in question:
        cutoff = datetime.now() - timedelta(days=90)
        active_ids = {order.get('customer_id') for order in orders if str(order.get('created_at', '')) and datetime.fromisoformat(str(order['created_at'])[:19]) >= cutoff}
        return ', '.join(customer.get('name', '') for customer in data.get('customers', []) if customer.get('id') not in active_ids) or 'No inactive customers found.'
    return 'I can answer revenue, best-selling products, low-stock risk, inactive customers, and promotion suggestions from the current store data.'

def customer_assistant_answer(data, session, question):
    question = question.casefold()
    if any(term in question for term in ('revenue', 'profit', 'sales report', 'admin', 'staff', 'customer list', 'stock valuation')):
        return 'I can help with products, your orders, payments, delivery, returns, and store policies. That question is available only to store administrators.'
    if 'delivery' in question or 'shipping' in question:
        return data.get('shop', {}).get('delivery_information', 'Delivery information is confirmed during checkout.')
    if 'return' in question or 'refund' in question:
        return data.get('shop', {}).get('return_policy', 'Please contact customer care with your order number for return assistance.')
    if 'order' in question or 'tracking' in question:
        orders = [order for order in data.get('orders', []) if order.get('customer_id') == session.get('customer_id')]
        if not orders:
            return 'You do not have any orders yet. You can browse the shop and place an order when ready.'
        latest = max(orders, key=lambda order: str(order.get('created_at', '')))
        return f'Your latest order {latest.get("order_number", "")} is {latest.get("status", "Pending")}. Payment: {latest.get("payment_status", "Pending")}. Total: {money(latest.get("total", 0))}.'
    matches = [product for product in data.get('products', []) if any(term in f'{product.get("name", "")} {product.get("brand", "")} {product.get("category", "")} {product.get("tags", [])}'.casefold() for term in question.split() if len(term) > 2)]
    if matches:
        return 'Here are matching products: ' + ', '.join(f'{product.get("name", "Product")} ({money(product.get("price", 0))})' for product in matches[:5])
    if any(term in question for term in ('product', 'perfume', 'serum', 'skin', 'makeup', 'hair')):
        return 'Tell me a product name, brand, category, or concern and I will help you find a match.'
    return 'I can help with products, your orders, payments, delivery, returns, refunds, and store policies. Please ask a customer-care question.'

def assistant_report_type(question):
    question = question.casefold()
    if 'payment' in question: return 'payments'
    if 'inventory' in question or 'stock' in question: return 'inventory'
    if 'customer' in question: return 'customers'
    if 'product' in question or 'best-selling' in question: return 'products'
    if 'financial' in question or 'profit' in question or 'revenue' in question: return 'financial'
    if 'order' in question or 'sales' in question: return 'orders'
    return None

def admin_assistant(data, answer=''):
    question = answer[0] if isinstance(answer, tuple) else ''
    response = answer[1] if isinstance(answer, tuple) else ''
    report = assistant_report_type(question) if question else None
    report_links = f'<p class="note">Download generated report: <a class="under" href="/admin/reports.csv?type={report}">CSV</a> <a class="under" href="/admin/reports.xlsx?type={report}">Excel</a> <a class="under" href="/admin/reports.pdf?type={report}">PDF</a> <a class="under" href="/admin/reports.print?type={report}">Print</a></p>' if report and response else ''
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / ASSISTANT</p><h1>Business <em>assistant.</em></h1><p class="hero-text">Ask operational questions and download a report generated from the current store data.</p><section class="panel"><form method="get" class="checkout-form"><label>Your question<input name="q" placeholder="What were my best-selling perfumes this month?" value="{esc(question)}"></label><button class="primary">Ask assistant</button></form>{f'<p class="notice">{esc(response)}</p>{report_links}' if response else ''}</section></section></main>''')

def customer_assistant(data, session, question=''):
    response = customer_assistant_answer(data, session, question) if question else ''
    return layout(data, f'''<main class="info-page"><section class="info-hero"><p class="eyebrow">CUSTOMER CARE</p><h1>Ask <em>Luxe.</em></h1><p class="hero-text">Ask about products, your orders, payments, delivery, returns, and store policies.</p><form method="get" class="checkout-form"><label>Your question<input name="q" value="{esc(question)}" placeholder="Where is my latest order?"></label><button class="primary">Ask here</button></form>{f'<p class="notice">{esc(response)}</p>' if response else ''}</section></main>''')

def admin_staff(data, message=''):
    rows = ''.join(f'<div class="table-row"><span>{esc(user.get("name", ""))}<small>{esc(user.get("email", ""))}</small></span><span>{esc(user.get("role", "STAFF"))}</span><span>{"Active" if user.get("active", True) else "Disabled"}</span><span><form method="post"><input type="hidden" name="action" value="staff_toggle"><input type="hidden" name="staff_id" value="{user.get("id", 0)}"><button>{"Disable" if user.get("active", True) else "Activate"}</button></form></span></div>' for user in data.get('users', []))
    online_emails = online_account_emails()
    accounts = {}
    for account in data.get('users', []) + data.get('customers', []):
        email = account.get('email', '').strip().lower()
        if email:
            accounts[email] = account
    groups = (
        ('SUPERADMINS', lambda account: is_superadmin_role(account.get('role'))),
        ('ADMINS', lambda account: account.get('role') == 'ADMIN'),
        ('USERS', lambda account: not is_admin_role(account.get('role'))),
    ) + tuple((role, lambda account, role=role: account.get('role') == role) for role in sorted(data.get('shop', {}).get('custom_roles', {})))
    can_manage = is_superadmin_role(data.get('_admin_role'))

    def account_rows(accounts_for_group):
        return ''.join(f'<div class="table-row"><span><b>{esc(account.get("name", ""))}</b><small>{esc(account.get("email", ""))}</small></span><span>{esc(account.get("role", "CUSTOMER"))}</span><span><i class="status {"online" if account.get("email", "").strip().lower() in online_emails else "offline"}">{"Online" if account.get("email", "").strip().lower() in online_emails else "Offline"}</i></span><span>{"Active" if account.get("active", True) else "Disabled"}</span><span>{f'<form method="post" class="inline-form"><input type="hidden" name="action" value="staff_toggle"><input type="hidden" name="staff_id" value="{account.get("id", 0)}"><button>{"Disable" if account.get("active", True) else "Activate"}</button></form>' if can_manage and account in data.get("users", []) and account.get("email", "").strip().lower() != data.get("_admin_email", "").lower() else ""}</span></div>' for account in accounts_for_group) or '<p class="empty">No accounts in this group.</p>'

    sections = ''.join(f'<section class="panel staff-group"><div class="section-head"><div><p class="eyebrow">ACCOUNT GROUP</p><h2>{label.title()}</h2></div><span class="note">{sum(1 for account in accounts.values() if predicate(account))} accounts</span></div><div class="table"><div class="table-row table-head"><span>Name</span><span>Role</span><span>Presence</span><span>State</span><span>Action</span></div>{account_rows([account for account in accounts.values() if predicate(account)])}</div></section>' for label, predicate in groups)
    role_options = ''.join(f'<option>{role}</option>' for role in sorted(set(ROLE_PERMISSIONS) | {'ADMIN'}))
    create_form = f'<form method="post" class="checkout-form"><input type="hidden" name="action" value="staff_create"><label>Name<input name="staff_name" required></label><label>Email<input name="staff_email" type="email" required></label><label>Temporary password<input name="staff_password" type="password" required></label><label>Role<select name="staff_role">{role_options}</select></label><button class="primary">Create staff account</button></form>' if can_manage else '<p class="note">Only a superadmin can create or change managed accounts.</p>'
    permissions = ('products', 'orders', 'inventory', 'customers', 'payments', 'deliveries', 'promotions', 'expenses', 'refunds', 'reports', 'content')
    role_checkboxes = ''.join(f'<label><input type="checkbox" name="role_permission" value="{permission}"> {permission.title()}</label>' for permission in permissions)
    role_form = f'<section class="panel"><h2>Create custom role</h2><p class="note">Choose which admin dashboards this role can open and operate.</p><form method="post" class="checkout-form"><input type="hidden" name="action" value="role_save"><label>Role name<input name="role_name" placeholder="Regional Manager" required></label><div class="role-permissions">{role_checkboxes}</div><button class="primary">Save role</button></form></section>' if can_manage else ''
    custom_roles = ''.join(f'<div class="table-row"><span><b>{esc(role)}</b></span><span>{esc(", ".join(sorted(perms)))}</span><span><form method="post" class="inline-form"><input type="hidden" name="action" value="role_delete"><input type="hidden" name="role_name" value="{esc(role)}"><button>Delete role</button></form></span></div>' for role, perms in data.get('shop', {}).get('custom_roles', {}).items()) or '<p class="empty">No custom roles yet.</p>'
    roles_panel = f'<section class="panel"><h2>Custom roles</h2><div class="table"><div class="table-row table-head"><span>Role</span><span>Dashboards</span><span>Action</span></div>{custom_roles}</div></section>' if can_manage else ''
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / USERS & ROLES</p><h1>Account <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<p class="hero-text">Live presence is based on the current role policy. Custom roles can be assigned to staff accounts without code changes.</p><div class="staff-groups">{sections}</div><section class="panel"><h2>Add staff account</h2>{create_form}</section>{role_form}{roles_panel}</section></main>''')

def admin_restore(data, message=''):
    clear_control = '''<section class="panel danger-panel"><p class="eyebrow">SUPERADMIN ONLY</p><h2>Clear operational data</h2><p class="note">This removes products, orders, customers, payments, reviews, coupons, wishlists, and activity history. Shop settings, categories, and staff accounts are preserved.</p><form method="post" class="checkout-form" onsubmit="return confirm('This will permanently clear operational data. Continue?')"><input type="hidden" name="action" value="clear_database"><label>Type CLEAR DATABASE to confirm<input name="clear_confirmation" required autocomplete="off" pattern="CLEAR DATABASE"></label><button class="primary danger-button">CLEAR DATABASE</button></form></section>''' if is_superadmin_role(data.get('_admin_role')) else ''
    health = database_health(data)
    history = ''.join(f'<div class="table-row"><span>{esc(item["name"])}</span><span>{item["size"]:,} bytes</span><span>{esc(item["created_at"])}</span><span><a class="under" href="/admin/backup?download={quote(item["name"])}">Download</a></span></div>' for item in backup_history()[:12]) or '<p class="empty">No backups have been created yet.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / DATABASE</p><h1>Backup & <em>restore.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><div class="section-head"><div><h2>Database health</h2><p class="note">Status: <b>{esc(health["status"])}</b> · {health["products"]} products · {health["orders"]} orders · {health["customers"]} customers</p></div><form method="post"><input type="hidden" name="action" value="backup_now"><button class="primary">Create backup now</button></form></div><p class="note">Automatic backup: {esc(data.get("shop", {}).get("backup_schedule", "disabled"))} at {esc(data.get("shop", {}).get("backup_time", "02:00"))}. Latest: {esc((health["latest_backup"] or {}).get("name", "Never"))}</p></section><section class="panel"><h2>Backup history</h2><div class="table"><div class="table-row table-head"><span>File</span><span>Size</span><span>Created</span><span></span></div>{history}</div></section><section class="panel"><h2>Restore backup</h2><form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="restore"><label>Backup SQL or JSON file<input name="backup_file" type="file" accept=".sql,.json,application/sql,application/json" required></label><button class="primary" onclick="return confirm('Restore this database backup?')">RESTORE DATABASE</button></form><small class="note">SQL backups are generated for the application state table and remain compatible with older JSON backups.</small></section>{clear_control}</section></main>''')

def admin_products(data, message=''):
    fields = [('name', 'Product name'), ('sku', 'SKU'), ('brand', 'Brand'), ('barcode', 'Barcode'), ('price', 'Selling price'), ('discount_price', 'Sale/original price'), ('cost_price', 'Cost price'), ('stock', 'Stock'), ('minimum_stock', 'Low-stock threshold')]
    controls = ''.join(f'<label>{label}<input name="{key}" type="number" step="0.01" {"required" if key in ("price", "stock") else ""}></label>' if key in ('price', 'discount_price', 'cost_price', 'stock', 'minimum_stock') else f'<label>{label}<input name="{key}" {"required" if key in ("name", "sku") else ""}></label>' for key, label in fields)
    shipping_options = ''.join(f'<option>{shipping_class}</option>' for shipping_class in SHIPPING_CLASSES)
    controls += f'<label>Category<select name="category" required><option value="">Select category</option>{category_options(data)}</select></label><label>Subcategory<select name="subcategory" required><option value="">Select subcategory</option>{subcategory_options(data)}</select></label><label>Shipping class<select name="shipping_class">{shipping_options}</select></label><label>Status<select name="status"><option>Active</option><option>Draft</option><option>Archived</option></select></label><label>Weight (kg)<input name="weight_kg" type="number" min="0" step="0.1" value="0.5"></label><label>Dimensions<input name="dimensions" placeholder="Length x width x height"></label><label>Product video URL<input name="video_url"></label>'
    controls += '<label>Product images (JPG, JPEG, PNG, WEBP)<input id="image-file" name="image_file" type="file" accept=".jpg,.jpeg,.png,.webp" multiple required><small class="note">The first image is primary. Maximum 5 MB per image.</small></label><label>Variants<small class="note">One per line: Name | Price | Stock</small><textarea name="variants" placeholder="50ml | 12400 | 8"></textarea></label>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PRODUCTS</p><h1>Add a <em>product.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="product_create">{controls}<label>Description<textarea name="description"></textarea></label><label>Tags<input name="tags" placeholder="gift, new, featured"></label><button class="primary">SAVE PRODUCT ↗</button><a class="under" href="/admin">Cancel</a></form></section></section></main>''')

def admin_product_edit(data, product, message=''):
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PRODUCTS / EDIT</p><h1>Edit <em>{esc(product['name'])}.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="product_update"><input type="hidden" name="product_id" value="{product['id']}"><label>Product name<input name="name" value="{esc(product['name'])}" required></label><label>SKU<input name="sku" value="{esc(product.get('sku', ''))}" required></label><label>Brand<input name="brand" value="{esc(product.get('brand', ''))}"></label><label>Selling price (KSh)<input name="price" type="number" step="0.01" value="{product['price']}" required></label><label>Original price (KSh)<input name="discount_price" type="number" step="0.01" value="{product.get('old_price') or ''}"></label><label>Stock<input name="stock" type="number" value="{product['stock']}" required></label><label>Minimum stock<input name="minimum_stock" type="number" value="{product.get('minimum_stock', 5)}"></label><label>Category<select name="category" required>{category_options(data, product.get('category', ''))}</select></label><label>Subcategory<select name="subcategory" required>{subcategory_options(data, product.get('subcategory', ''))}</select></label><label>Product image<input id="image-file" name="image_file" type="file" accept=".jpg,.jpeg,.png,.webp"><img id="image-preview" class="upload-preview" src="{esc(product.get('image', ''))}" alt="Product preview"></label><label>Description<textarea name="description">{esc(product.get('description', ''))}</textarea></label><label>Tags<input name="tags" value="{esc(', '.join(product.get('tags', [])))}"></label><button class="primary">SAVE PRODUCT CHANGES ✓</button><a class="under" href="/admin">Cancel</a></form></section></section><script>document.querySelector('#image-file').onchange=event=>document.querySelector('#image-preview').src=URL.createObjectURL(event.target.files[0]);</script></main>''')

def admin_product_edit(data, product, message=''):
    shipping_options = ''.join(f'<option {"selected" if product_shipping_class(product) == shipping_class else ""}>{shipping_class}</option>' for shipping_class in SHIPPING_CLASSES)
    status = product.get('status', 'Active')
    status_options = ''.join(f'<option {"selected" if status == value else ""}>{value}</option>' for value in ('Active', 'Draft', 'Archived'))
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PRODUCTS / EDIT</p><h1>Edit <em>{esc(product.get('name', 'Product'))}.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<section class="panel"><form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="product_update"><input type="hidden" name="product_id" value="{product.get('id')}"><label>Product name<input name="name" value="{esc(product.get('name', ''))}" required></label><label>Short description<textarea name="short_description">{esc(product.get('short_description', ''))}</textarea></label><label>Description<textarea name="description">{esc(product.get('description', ''))}</textarea></label><label>SKU<input name="sku" value="{esc(product.get('sku', ''))}" required></label><label>Barcode<input name="barcode" value="{esc(product.get('barcode', ''))}"></label><label>Brand<input name="brand" value="{esc(product.get('brand', ''))}"></label><label>Selling price<input name="price" type="number" step="0.01" value="{product.get('price', 0)}" required></label><label>Sale/original price<input name="discount_price" type="number" step="0.01" value="{product.get('old_price') or ''}"></label><label>Cost price<input name="cost_price" type="number" step="0.01" value="{product.get('cost_price', 0)}"></label><label>Stock quantity<input name="stock" type="number" value="{product.get('stock', 0)}" required></label><label>Low-stock threshold<input name="minimum_stock" type="number" value="{product.get('minimum_stock', 5)}"></label><label>Category<select name="category" required>{category_options(data, product.get('category', ''))}</select></label><label>Subcategory<select name="subcategory" required>{subcategory_options(data, product.get('subcategory', ''))}</select></label><label>Shipping class<select name="shipping_class">{shipping_options}</select></label><label>Status<select name="status">{status_options}</select></label><label>Weight (kg)<input name="weight_kg" type="number" step="0.1" value="{product.get('weight_kg', 0.5)}"></label><label>Dimensions<input name="dimensions" value="{esc(product.get('dimensions', ''))}"></label><label>Product video URL<input name="video_url" value="{esc(product.get('video_url', ''))}"></label><label>Product images<input id="image-file" name="image_file" type="file" accept=".jpg,.jpeg,.png,.webp" multiple><img id="image-preview" class="upload-preview" src="{esc(product.get('image', ''))}" alt="Product preview"></label><label>Tags<input name="tags" value="{esc(', '.join(product.get('tags', [])))}"></label><label>Variants<small class="note">One per line: Name | Price | Stock</small><textarea name="variants">{esc('\n'.join(f'{variant.get("name", "")} | {variant.get("price", 0)} | {variant.get("stock", 0)}' for variant in product.get('variants', [])))}</textarea></label><button class="primary">Save product changes</button><a class="under" href="/admin">Cancel</a></form></section></section></main>''')

def admin_inventory(data, message=''):
    products = data.get('products', [])
    valuation = sum(float(product.get('cost_price', 0) or 0) * int(product.get('stock', 0) or 0) for product in products)
    rows = ''.join(f'<div class="table-row"><span><b>{esc(product.get("name", ""))}</b><small>{esc(product.get("sku", ""))} · {esc(product.get("barcode", ""))}</small></span><span>{product.get("stock", 0)}</span><span>{"Out of stock" if not product.get("stock") else "Low stock" if product.get("stock", 0) <= product.get("minimum_stock", 5) else "In stock"}</span><span><form method="post" class="inline-form"><input type="hidden" name="action" value="inventory_adjust"><input type="hidden" name="product_id" value="{product.get("id")}"><input name="amount" type="number" placeholder="+/- qty" required><input name="reason" placeholder="Reason" required><button>Adjust</button></form></span></div>' for product in products) or '<p class="empty">No products in inventory.</p>'
    movements = ''.join(f'<div class="table-row"><span>{esc(item.get("date", ""))}</span><span>{esc(item.get("product_id", ""))}</span><span>{item.get("quantity_change", 0)}</span><span>{esc(item.get("reason", ""))}</span><span>{esc(item.get("user", "Admin"))}</span></div>' for item in reversed(data.get('inventory_movements', [])[-30:])) or '<p class="empty">No inventory movements recorded.</p>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / INVENTORY</p><h1>Stock <em>management.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<div class="stats"><div><span>Products</span><b>{len(products)}</b></div><div><span>Low stock</span><b>{sum(1 for product in products if 0 < product.get('stock', 0) <= product.get('minimum_stock', 5))}</b></div><div><span>Out of stock</span><b>{sum(1 for product in products if not product.get('stock'))}</b></div><div><span>Stock valuation</span><b>{money(valuation)}</b></div></div><section class="panel"><h2>Inventory</h2><div class="table"><div class="table-row table-head"><span>Product / SKU</span><span>Stock</span><span>Status</span><span>Adjust</span></div>{rows}</div></section><section class="panel"><h2>Stock history</h2><div class="table"><div class="table-row table-head"><span>Date</span><span>Product</span><span>Change</span><span>Reason</span><span>User</span></div>{movements}</div></section></section></main>''')

def category_manager(data, message=''):
    groups = ''.join(f'<fieldset><legend>{esc(category)}</legend><textarea name="category_{index}" rows="{max(4, len(subcategories))}" aria-label="{esc(category)} subcategories">{esc("\n".join(subcategories))}</textarea><small class="note">One subcategory per line.</small></fieldset>' for index, (category, subcategories) in enumerate(category_groups(data).items()))
    names = ''.join(f'<input type="hidden" name="name_{index}" value="{esc(category)}">' for index, category in enumerate(category_groups(data)))
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / CATALOGUE / CATEGORIES</p><h1>Product <em>categories.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<p class="hero-text">Manage the approved category hierarchy used by product entry and storefront filters.</p><form method="post" class="branding-form category-manager"><input type="hidden" name="action" value="categories_save">{names}{groups}<button class="primary">Save category hierarchy ✓</button></form></section></main>''')

def admin_products(data, message='', query=''):
    normalized_query = str(query or '').strip().casefold()
    products = [product for product in data.get('products', []) if not normalized_query or normalized_query in f'{product.get("name", "")} {product.get("brand", "")} {product.get("sku", "")} {product.get("category", "")}'.casefold()]
    rows = ''.join(f'<div class="table-row"><span><b>{esc(product.get("name", ""))}</b><small>{esc(product.get("brand", ""))} · {esc(product.get("sku", ""))}</small></span><span>{esc(product.get("category", ""))}</span><span>{money(product.get("price", 0))}</span><span>{product.get("stock", 0)}</span><span>{esc(product.get("status", "Active"))}</span><span><a class="under" href="/admin/products/edit?id={product.get("id")}">Edit</a>{f'<form method="post" class="inline-form"><input type="hidden" name="action" value="product_delete"><input type="hidden" name="product_id" value="{product.get("id")}"><button>Delete</button></form>' if is_superadmin_role(data.get('_admin_role')) else ''}</span></div>' for product in products) or '<p class="empty">No products yet. Add your first product below.</p>'
    search = f'<form class="dashboard-filter" method="get"><label>Search products<input name="q" value="{esc(query)}" placeholder="Name, SKU, brand or category"></label><button class="primary">Search</button></form>'
    fields = [('name', 'Product name'), ('sku', 'SKU'), ('brand', 'Brand'), ('barcode', 'Barcode'), ('price', 'Selling price'), ('discount_price', 'Sale/original price'), ('cost_price', 'Cost price'), ('stock', 'Stock'), ('minimum_stock', 'Low-stock threshold')]
    controls = ''.join(f'<label>{label}<input name="{key}" type="number" step="0.01" {"required" if key in ("price", "stock") else ""}></label>' if key in ('price', 'discount_price', 'cost_price', 'stock', 'minimum_stock') else f'<label>{label}<input name="{key}" {"required" if key in ("name", "sku") else ""}></label>' for key, label in fields)
    shipping_options = ''.join(f'<option>{shipping_class}</option>' for shipping_class in SHIPPING_CLASSES)
    controls += f'<label>Category<select name="category" required><option value="">Select category</option>{category_options(data)}</select></label><label>Subcategory<select name="subcategory" required><option value="">Select subcategory</option>{subcategory_options(data)}</select></label><label>Shipping class<select name="shipping_class">{shipping_options}</select></label><label>Status<select name="status"><option>Active</option><option>Draft</option><option>Archived</option></select></label><label>Weight (kg)<input name="weight_kg" type="number" min="0" step="0.1" value="0.5"></label><label>Dimensions<input name="dimensions" placeholder="Length x width x height"></label><label>Product video URL<input name="video_url"></label><label>Product images<input name="image_file" type="file" accept=".jpg,.jpeg,.png,.webp" multiple required></label><label>Variants<small class="note">One per line: Name | Price | Stock</small><textarea name="variants"></textarea></label>'
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / PRODUCTS</p><h1>Product <em>catalogue.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}{search}<section class="panel"><div class="section-head"><div><h2>All products</h2><p class="note">{len(products)} products in the catalogue</p></div><a class="primary" href="#add-product">Add product</a></div><div class="table"><div class="table-row table-head"><span>Product</span><span>Category</span><span>Price</span><span>Stock</span><span>Status</span><span>Actions</span></div>{rows}</div></section><section class="panel" id="add-product"><h2>Add new product</h2><form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="product_create">{controls}<label>Description<textarea name="description"></textarea></label><label>Short description<textarea name="short_description"></textarea></label><label>Tags<input name="tags" placeholder="gift, new, featured"></label><button class="primary">Save product</button></form></section></section></main>''')

def report_csv(data, report):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    def safe(value):
        text = str(value or '')
        return "'" + text if text[:1] in ('=', '+', '-', '@') else text
    if report == 'sales':
        totals = {}
        for order in data.get('orders', []):
            day = str(order.get('created_at', ''))[:10] or 'Unknown'
            summary = totals.setdefault(day, {'orders': 0, 'revenue': 0, 'discounts': 0, 'delivery': 0})
            summary['orders'] += 1
            summary['revenue'] += order.get('total', 0)
            summary['discounts'] += order.get('discount', 0)
            summary['delivery'] += order.get('delivery_fee', 0)
        writer.writerow(['Date', 'Orders', 'Revenue', 'Discounts', 'Delivery'])
        for day, summary in sorted(totals.items(), reverse=True):
            writer.writerow([day, summary['orders'], summary['revenue'], summary['discounts'], summary['delivery']])
    elif report == 'users':
        writer.writerow(['Name', 'Email', 'Role', 'Status', 'Created'])
        for user in data.get('users', []):
            writer.writerow([safe(user.get('name')), safe(user.get('email')), user.get('role', 'STAFF'), 'Active' if user.get('active', True) else 'Disabled', user.get('created_at', '')])
    elif report == 'payments':
        writer.writerow(['Order', 'Customer', 'Method', 'Payment status', 'Amount', 'Date'])
        for order in data.get('orders', []):
            writer.writerow([safe(order.get('order_number')), safe(order.get('customer_name')), safe(order.get('payment_method')), order.get('payment_status', 'Pending'), order.get('total', 0), order.get('created_at', '')])
    elif report == 'customers':
        writer.writerow(['Customer', 'Email', 'Orders', 'Total spending'])
        for customer in data.get('customers', []):
            orders = [order for order in data.get('orders', []) if order.get('customer_id') == customer['id']]
            writer.writerow([safe(customer.get('name')), safe(customer.get('email')), len(orders), sum(order.get('total', 0) for order in orders)])
    elif report == 'inventory':
        writer.writerow(['Product', 'SKU', 'Stock', 'Status'])
        for product in data['products']:
            status = 'OUT OF STOCK' if not product['stock'] else 'LOW STOCK' if product['stock'] <= 5 else 'IN STOCK'
            writer.writerow([safe(product.get('name')), safe(product.get('sku')), product['stock'], status])
    elif report == 'products':
        writer.writerow(['Product', 'SKU', 'Category', 'Price', 'Stock', 'Status'])
        for product in data.get('products', []):
            status = 'OUT OF STOCK' if not product.get('stock') else 'LOW STOCK' if product.get('stock', 0) <= product.get('minimum_stock', 5) else 'IN STOCK'
            writer.writerow([safe(product.get('name')), safe(product.get('sku')), safe(product.get('category')), product.get('price', 0), product.get('stock', 0), status])
    elif report == 'financial':
        product_costs = {product.get('id'): float(product.get('cost_price', 0) or 0) for product in data.get('products', [])}
        expenses = sum(float(item.get('amount', 0) or 0) for item in data.get('expenses', []))
        refunds = sum(float(item.get('amount', 0) or 0) for item in data.get('refunds', []) if item.get('status') in ('Processed', 'Refunded'))
        revenue = sum(float(order.get('total', 0) or 0) for order in data.get('orders', []) if order.get('status') != 'Cancelled')
        cogs = sum(product_costs.get(item.get('product_id'), 0) * int(item.get('quantity', 0)) for order in data.get('orders', []) for item in order.get('items', []))
        writer.writerow(['Revenue', 'Cost of goods', 'Expenses', 'Refunds', 'Estimated net profit'])
        writer.writerow([revenue, cogs, expenses, refunds, revenue - cogs - expenses - refunds])
    else:
        writer.writerow(['Order', 'Customer', 'Status', 'Payment', 'Total', 'Date'])
        for order in data.get('orders', []):
            writer.writerow([safe(order.get('order_number')), safe(order.get('customer_name')), order.get('status'), safe(order.get('payment_method')), order.get('total'), order.get('created_at')])
    return output.getvalue()

def report_rows(data, report):
    parsed = list(csv.reader(io.StringIO(report_csv(data, report))))
    return parsed[0], parsed[1:]

def report_xlsx(data, report):
    header, rows = report_rows(data, report)
    workbook = Workbook(); sheet = workbook.active; sheet.title = report[:31]
    sheet.append(header)
    for row in rows: sheet.append(row)
    sheet.freeze_panes = 'A2'; sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO(); workbook.save(output); return output.getvalue()

def report_pdf(data, report):
    header, rows = report_rows(data, report)
    output = io.BytesIO(); document = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    table = Table([header] + rows, repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3f5548')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#dfe5dc')), ('FONTSIZE', (0, 0), (-1, -1), 7), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    document.build([table]); return output.getvalue()

def report_print_page(data, report):
    header, rows = report_rows(data, report)
    table = '<table><thead><tr>' + ''.join(f'<th>{esc(value)}</th>' for value in header) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{esc(value)}</td>' for value in row) + '</tr>' for row in rows) + '</tbody></table>'
    return f'<!doctype html><html><head><title>{esc(report.title())} report</title><style>body{{font:12px Arial;color:#1f2924}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd5cc;padding:6px;text-align:left}}th{{background:#3f5548;color:#fff}}@media print{{button{{display:none}}}}</style></head><body><button onclick="window.print()">Print</button><h1>{esc(report.title())}</h1>{table}</body></html>'

def report_dashboard(data, report='sales'):
    report_types = ('sales', 'users', 'payments', 'orders', 'inventory', 'customers', 'products', 'financial')
    report = report if report in report_types else 'sales'
    orders = data.get('orders', [])
    revenue = sum(order.get('total', 0) for order in orders)
    paid = sum(order.get('total', 0) for order in orders if order.get('payment_status') == 'Paid')
    pending_payments = sum(1 for order in orders if order.get('payment_status', 'Pending') == 'Pending')
    metrics = [('Revenue', money(revenue)), ('Orders', len(orders)), ('Users', len(data.get('users', [])) + len(data.get('customers', []))), ('Paid payments', money(paid)), ('Online now', len(online_account_emails()))]
    if report == 'sales':
        daily = {}
        for order in orders:
            day = str(order.get('created_at', ''))[:10] or 'Unknown'
            daily.setdefault(day, {'orders': 0, 'revenue': 0})
            daily[day]['orders'] += 1
            daily[day]['revenue'] += order.get('total', 0)
        headings = ('Date', 'Orders', 'Revenue')
        records = [(day, values['orders'], money(values['revenue'])) for day, values in sorted(daily.items(), reverse=True)]
    elif report == 'users':
        headings = ('Name', 'Email', 'Role', 'Status')
        records = [(user.get('name', ''), user.get('email', ''), user.get('role', 'STAFF'), 'Active' if user.get('active', True) else 'Disabled') for user in data.get('users', [])]
    elif report == 'payments':
        headings = ('Order', 'Customer', 'Method', 'Status', 'Amount')
        records = [(order.get('order_number', ''), order.get('customer_name', 'Guest'), order.get('payment_method', ''), order.get('payment_status', 'Pending'), money(order.get('total', 0))) for order in reversed(orders)]
    elif report == 'orders':
        headings = ('Order', 'Customer', 'Status', 'Payment', 'Total')
        records = [(order.get('order_number', ''), order.get('customer_name', 'Guest'), order.get('status', 'Pending'), order.get('payment_status', 'Pending'), money(order.get('total', 0))) for order in reversed(orders)]
    elif report == 'inventory':
        headings = ('Product', 'SKU', 'Stock', 'Status')
        records = [(product.get('name', ''), product.get('sku', ''), product.get('stock', 0), 'Out of stock' if not product.get('stock') else 'Low stock' if product.get('stock', 0) <= product.get('minimum_stock', 5) else 'In stock') for product in data.get('products', [])]
    elif report == 'customers':
        headings = ('Customer', 'Email', 'Orders', 'Spending')
        records = [(customer.get('name', ''), customer.get('email', ''), len([order for order in orders if order.get('customer_id') == customer.get('id')]), money(sum(order.get('total', 0) for order in orders if order.get('customer_id') == customer.get('id')))) for customer in data.get('customers', [])]
    elif report == 'financial':
        headings = ('Revenue', 'Cost of goods', 'Expenses', 'Refunds', 'Estimated net profit')
        revenue = sum(float(order.get('total', 0) or 0) for order in orders if order.get('status') != 'Cancelled')
        expenses = sum(float(item.get('amount', 0) or 0) for item in data.get('expenses', []))
        refunds = sum(float(item.get('amount', 0) or 0) for item in data.get('refunds', []) if item.get('status') in ('Processed', 'Refunded'))
        cogs = sum(float(next((product.get('cost_price', 0) for product in data.get('products', []) if product.get('id') == item.get('product_id')), 0) or 0) * int(item.get('quantity', 0)) for order in orders for item in order.get('items', []))
        records = [(money(revenue), money(cogs), money(expenses), money(refunds), money(revenue - cogs - expenses - refunds))]
    else:
        headings = ('Product', 'SKU', 'Category', 'Price', 'Stock')
        records = [(product.get('name', ''), product.get('sku', ''), product.get('category', ''), money(product.get('price', 0)), product.get('stock', 0)) for product in data.get('products', [])]
    tabs = ''.join(f'<a class="pill {"active" if item == report else ""}" href="/admin/reports?type={item}">{item.title()}</a>' for item in report_types)
    header = ''.join(f'<span>{esc(heading)}</span>' for heading in headings)
    rows = ''.join('<div class="table-row">' + ''.join(f'<span>{esc(value)}</span>' for value in record) + '</div>' for record in records) or '<p class="empty">No data is available for this report yet.</p>'
    metric_cards = ''.join(f'<div><span>{esc(label)}</span><b>{esc(value)}</b></div>' for label, value in metrics)
    return layout(data, f'''<main class="admin-page"><section class="admin-content reports-page"><p class="eyebrow">ADMIN PANEL / REPORTS</p><h1>Business <em>reports.</em></h1><p class="hero-text">Review sales, users, payments, orders, customers, products, and inventory from one place.</p><div class="stats report-stats">{metric_cards}</div><div class="pills report-tabs">{tabs}</div><section class="panel"><div class="section-head"><div><p class="eyebrow">CURRENT REPORT</p><h2>{report.title()}</h2></div><a class="primary" href="/admin/reports.csv?type={report}">Download CSV ↗</a></div><div class="table"><div class="table-row table-head">{header}</div>{rows}</div></section><p class="note">Pending payments: {pending_payments}. Exported CSV files contain the full rows for the selected report.</p></section></main>''')

def wishlist_page(data, session):
    ids = data.get('wishlists', {}).get(str(session.get('customer_id', session.get('token', 'guest'))), [])
    products = [p for p in data['products'] if p['id'] in ids]
    return layout(data, f'''<main class="catalog wishlist-page"><p class="eyebrow">SAVED FOR LATER</p><h1>Your <em>wishlist.</em></h1><div class="product-grid">{''.join(card(p) for p in products) or '<p class="empty">Your wishlist is empty.</p>'}</div></main>''')

def logo_source_available(image):
    image = (image or '').strip()
    if image.startswith(('https://', 'http://')):
        return True
    if image.startswith('/uploads/'):
        return (ROOT / 'uploads' / Path(image).name).is_file()
    return False

def branding_settings(data, message='', edit_zone_id=''):
    shop = data['shop']
    textarea_keys = {'description', 'delivery_information', 'return_policy', 'privacy_policy', 'terms', 'story_description', 'newsletter_description'}

    def field(key, label):
        if key in textarea_keys:
            control = f'<textarea name="{key}">{esc(shop.get(key, ""))}</textarea>'
        else:
            control = f'<input name="{key}" value="{esc(shop.get(key, ""))}">' 
        return f'<label>{label}{control}</label>'

    def section(title, fields):
        return f'<section class="settings-section"><h2>{title}</h2>{"".join(field(key, label) for key, label in fields)}</section>'

    controls = section('Store information', [('name','Shop name'),('tagline','Tagline'),('description','Description'),('phone','Phone'),('whatsapp','WhatsApp'),('email','Email'),('location','Physical location'),('business_hours','Business hours'),('country','Country'),('currency','Currency'),('delivery_fee','Delivery fee'),('free_delivery_threshold','Free delivery threshold'),('order_prefix','Order number prefix'),('invoice_prefix','Invoice number prefix'),('receipt_prefix','Receipt number prefix')])
    controls += f'<section class="settings-section"><h2>Brand assets</h2><label>Logo file<input name="logo_file" type="file" accept="image/*"><small class="note">Upload any image format, maximum 5 MB.</small></label><label>Favicon URL<input name="favicon" value="{esc(shop.get("favicon", ""))}"></label>{f'<img class="settings-logo-preview" src="{esc(shop.get("logo", ""))}" alt="Current logo">' if logo_source_available(shop.get("logo", "")) else ""}</section>'
    controls += section('Policies', [('delivery_information','Delivery information'),('return_policy','Return policy'),('privacy_policy','Privacy policy'),('terms','Terms and conditions')])
    controls += section('Brand story', [('story_heading','Brand story heading'),('story_description','Brand story description'),('newsletter_heading','Newsletter heading'),('newsletter_description','Newsletter description')])
    social_fields = ''.join(f'<label>{label}<input name="social_{key}" value="{esc(shop.get("social", {}).get(key, ""))}"></label>' for key, label in (('instagram', 'Instagram URL'), ('facebook', 'Facebook URL'), ('tiktok', 'TikTok URL'), ('x', 'X URL'), ('youtube', 'YouTube URL')))
    controls += f'<section class="settings-section"><h2>Social links</h2>{social_fields}</section>'
    integrations = data.setdefault('integrations', {})
    mpesa_fields = ''.join(f'<label>{label}<input name="mpesa_{key}" type="{"password" if "secret" in key or key == "passkey" else "text"}" value="{esc(integrations.get(f"mpesa_{key}", "")) if "secret" not in key and key != "passkey" else ""}" placeholder="{"Configured" if integrations.get(f"mpesa_{key}") and ("secret" in key or key == "passkey") else ""}"></label>' for key, label in (('environment', 'Environment'), ('shortcode', 'Shortcode'), ('consumer_key', 'Consumer key'), ('consumer_secret', 'Consumer secret'), ('passkey', 'Passkey'), ('callback_url', 'Callback URL')))
    smtp_fields = ''.join(f'<label>{label}<input name="smtp_{key}" type="{"password" if key == "password" else "text"}" value="{esc(integrations.get(f"smtp_{key}", "")) if key != "password" else ""}" placeholder="{"Configured" if key == "password" and integrations.get("smtp_password") else ""}"></label>' for key, label in (('host', 'SMTP host'), ('port', 'SMTP port'), ('user', 'SMTP username'), ('password', 'SMTP password'), ('from', 'From address')))
    controls += f'<section class="settings-section"><h2>Integrations</h2><h3>M-Pesa</h3>{mpesa_fields}<h3>SMTP email</h3>{smtp_fields}</section>'
    backup_schedule = shop.get('backup_schedule', 'disabled')
    backup_day = shop.get('backup_day', 'Sunday')
    controls += f'<section class="settings-section"><h2>Database backups</h2><label>Automatic backup schedule<select name="backup_schedule"><option value="disabled" {"selected" if backup_schedule == "disabled" else ""}>Disabled</option><option value="daily" {"selected" if backup_schedule == "daily" else ""}>Daily</option><option value="weekly" {"selected" if backup_schedule == "weekly" else ""}>Weekly</option></select></label><label>Backup time<input name="backup_time" type="time" value="{esc(shop.get("backup_time", "02:00"))}"></label><label>Backup day<select name="backup_day">{''.join(f'<option {"selected" if backup_day == day else ""}>{day}</option>' for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}</select></label><label>Keep recent backups<input name="backup_retention" type="number" min="1" max="100" value="{esc(shop.get("backup_retention", 7))}"></label><small class="note">Backups are written to the server backups folder. Keep an off-server copy for disaster recovery.</small></section>'
    theme = f'<label>Default theme<select name="default_theme"><option value="system" {"selected" if shop.get("default_theme", "system") == "system" else ""}>Use device preference</option><option value="light" {"selected" if shop.get("default_theme") == "light" else ""}>Light</option><option value="dark" {"selected" if shop.get("default_theme") == "dark" else ""}>Dark</option></select></label>'
    hero = shop.setdefault('hero', {})
    hero_fields = ''.join(f'<label>Hero {label}<input name="hero_{key}" value="{esc(hero.get(key, ""))}"></label>' for key, label in (('heading', 'heading'), ('image', 'image URL'), ('primary_label', 'primary button label'), ('primary_link', 'primary button link'), ('secondary_label', 'secondary button label'), ('secondary_link', 'secondary button link')))
    controls += f'<section class="settings-section"><h2>Homepage hero</h2>{hero_fields}<label>Hero description<textarea name="hero_description">{esc(hero.get("description", ""))}</textarea></label>{theme}</section>'
    shipping = delivery_settings(shop)
    edit_zone = next((zone for zone in shipping['zones'] if str(zone.get('id', '')) == str(edit_zone_id)), None)
    zone_rows = ''.join(f'<div class="table-row"><span><b>{esc(zone.get("name", "Unnamed zone"))}</b><small>{esc(", ".join(zone.get("locations", [])) if isinstance(zone.get("locations", []), list) else zone.get("locations", ""))}</small></span><span>{money(float(zone.get("base_fee", 0)))}</span><span>{money(float(zone.get("free_threshold", 0)))}</span><span>{"Active" if zone.get("active", True) else "Disabled"}</span><span><a class="under" href="/admin/settings?edit_zone={esc(zone.get("id", ""))}">Edit</a> <form method="post" class="inline-form"><input type="hidden" name="action" value="delivery_zone_toggle"><input type="hidden" name="zone_id" value="{esc(zone.get("id", ""))}"><button>{"Disable" if zone.get("active", True) else "Enable"}</button></form> <form method="post" class="inline-form"><input type="hidden" name="action" value="delivery_zone_delete"><input type="hidden" name="zone_id" value="{esc(zone.get("id", ""))}"><button>Delete</button></form></span></div>' for zone in shipping['zones']) or '<p class="empty">No delivery zones configured. The default fee will be used until you add one.</p>'
    zone = edit_zone or {}
    locations = zone.get('locations', [])
    locations = ', '.join(locations) if isinstance(locations, list) else locations
    class_fee_fields = ''.join(f'<label>{shipping_class} fee<input name="class_fee_{shipping_class.lower().replace(" ", "_")}" type="number" min="0" step="0.01" value="{esc(zone.get("shipping_class_fees", {}).get(shipping_class, ""))}"></label>' for shipping_class in SHIPPING_CLASSES if shipping_class not in ("Free Delivery", "Pickup Only"))
    delivery_controls = f'''<section class="panel"><h2>Delivery & shipping</h2><form method="post" class="branding-form"><input type="hidden" name="action" value="delivery_settings"><div class="settings-section"><label>Enable delivery<select name="delivery_enabled"><option value="1" {"selected" if shipping.get("enabled", True) else ""}>On</option><option value="0" {"selected" if not shipping.get("enabled", True) else ""}>Off</option></select></label><label>Default method<select name="delivery_method"><option value="zone" {"selected" if shipping.get("method", "zone") == "zone" else ""}>Zone based</option><option value="weight" {"selected" if shipping.get("method") == "weight" else ""}>Weight based</option></select></label><label>Free delivery<select name="free_delivery_enabled"><option value="1" {"selected" if shipping.get("free_delivery_enabled", True) else ""}>On</option><option value="0" {"selected" if not shipping.get("free_delivery_enabled", True) else ""}>Off</option></select></label><label>Default free-delivery threshold (KSh)<input name="default_free_threshold" type="number" min="0" step="0.01" value="{esc(shipping.get("default_free_threshold", 10000))}"></label><label>Default delivery fee (KSh)<input name="default_fee" type="number" min="0" step="0.01" value="{esc(shipping.get("default_fee", 350))}"></label><button class="primary">Save delivery settings</button></div></form><div class="table"><div class="table-row table-head"><span>Zone / locations</span><span>Base fee</span><span>Free from</span><span>Status</span><span>Actions</span></div>{zone_rows}</div><h3>{"Edit" if edit_zone else "Add"} delivery zone</h3><form method="post" class="branding-form"><input type="hidden" name="action" value="delivery_zone_save"><input type="hidden" name="zone_id" value="{esc(zone.get("id", ""))}"><label>Zone name<input name="zone_name" required value="{esc(zone.get("name", ""))}"></label><label>Counties / towns<input name="zone_locations" required value="{esc(locations)}" placeholder="Nairobi County, Nairobi CBD"></label><label>Base delivery fee (KSh)<input name="zone_base_fee" type="number" min="0" step="0.01" required value="{esc(zone.get("base_fee", shipping.get("default_fee", 350)))}"></label><label>Free-delivery threshold (KSh)<input name="zone_free_threshold" type="number" min="0" step="0.01" required value="{esc(zone.get("free_threshold", shipping.get("default_free_threshold", 10000)))}"></label><label>Calculation type<select name="zone_calculation_type"><option value="fixed" {"selected" if zone.get("calculation_type", "fixed") == "fixed" else ""}>Fixed fee</option><option value="weight" {"selected" if zone.get("calculation_type") == "weight" else ""}>Weight based</option></select></label><label>First weight (kg)<input name="zone_first_weight" type="number" min="0" step="0.1" value="{esc(zone.get("first_weight_kg", 2))}"></label><label>Additional weight fee (KSh/kg)<input name="zone_additional_weight_fee" type="number" min="0" step="0.01" value="{esc(zone.get("additional_weight_fee", 0))}"></label>{class_fee_fields}<label>Active<select name="zone_active"><option value="1" selected>Active</option><option value="0" {"selected" if zone and not zone.get("active", True) else ""}>Disabled</option></select></label><button class="primary">Save zone</button>{f'<a class="under" href="/admin/settings">Cancel</a>' if edit_zone else ''}</form></section>'''
    return layout(data, f'''<main class="admin-page"><section class="admin-content settings-page"><p class="eyebrow">ADMIN PANEL / SETTINGS / SHOP INFORMATION</p><h1>Shop <em>branding.</em></h1>{f'<p class="notice">✓ {esc(message)}</p>' if message else ''}<form method="post" enctype="multipart/form-data" class="branding-form"><input type="hidden" name="action" value="branding">{controls}<button class="primary">Save branding changes ✓</button></form>{delivery_controls}</section></main>''', 'Shop branding settings')

class Store(BaseHTTPRequestHandler):
    def send_error(self, code, message=None, explain=None):
        labels = {403: 'Access denied', 404: 'Page not found', 413: 'Request too large', 429: 'Too many requests', 500: 'Something went wrong'}
        body = layout(read_db(), f'<main class="empty"><h1>{labels.get(code, "Request error")}</h1><p>{esc(message or "Please try again later.")}</p><a class="primary" href="/">Return home</a></main>')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body.encode())

    def rate_limited(self):
        now = time.time(); address = self.client_address[0]
        with RATE_LIMIT_LOCK:
            recent = [stamp for stamp in RATE_LIMIT.get(address, []) if now - stamp < 60]
            recent.append(now)
            RATE_LIMIT[address] = recent[-121:]
            return len(recent) > 120

    def session(self):
        now = time.time()
        cookies = SimpleCookie(self.headers.get('Cookie', ''))
        token = cookies.get('luxe_session')
        current = SESSIONS.get(token.value) if token else None
        expired = current and (now - current.get('last_seen', now) > SESSION_IDLE_SECONDS or now - current.get('created_at', now) > SESSION_MAX_SECONDS)
        if not token or not current or expired:
            if token: SESSIONS.pop(token.value, None)
            self.session_token = secrets.token_urlsafe(18)
            SESSIONS[self.session_token] = {'cart': [], 'created_at': now, 'last_seen': now}
        else:
            self.session_token = token.value
            current['last_seen'] = now
        return SESSIONS[self.session_token]

    def csrf_token(self, session):
        session.setdefault('csrf', secrets.token_urlsafe(24))
        return session['csrf']

    def secure_cookie(self):
        forwarded_proto = self.headers.get('X-Forwarded-Proto', '').split(',', 1)[0].strip().lower()
        return os.environ.get('APP_ENV') == 'production' and forwarded_proto == 'https'

    def do_PUT(self):
        if not self.path.startswith('/api/products/'):
            return send_json(self, {'error': 'Not found'}, 404)
        session = self.session()
        if not session.get('admin'): return send_json(self, {'error': 'Admin sign-in required'}, 403)
        length = int(self.headers.get('Content-Length', 0)); raw = self.rfile.read(min(length, MAX_REQUEST_BYTES))
        try: updates = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError): return send_json(self, {'error': 'Invalid JSON'}, 400)
        data = read_db(); identifier = self.path.rstrip('/').rsplit('/', 1)[-1]; product = next((item for item in data.get('products', []) if str(item.get('id')) == identifier), None)
        if not product: return send_json(self, {'error': 'Product not found'}, 404)
        before = {key: product.get(key) for key in ('name', 'sku', 'price', 'old_price', 'stock', 'active')}
        for key in ('name', 'sku', 'brand', 'category', 'subcategory', 'description', 'image'):
            if key in updates: product[key] = str(updates[key]).strip()
        for key in ('price', 'old_price', 'stock', 'minimum_stock'):
            if key in updates: product[key] = float(updates[key]) if key in ('price', 'old_price') else int(updates[key])
        after = {key: product.get(key) for key in before}
        audit(data, session, 'product_update', f'{product["name"]} updated via API', 'product', product['id'], before, after); write_db(data); return send_json(self, product)

    def do_DELETE(self):
        if not self.path.startswith('/api/products/'):
            return send_json(self, {'error': 'Not found'}, 404)
        session = self.session()
        if not session.get('admin'): return send_json(self, {'error': 'Admin sign-in required'}, 403)
        data = read_db(); identifier = self.path.rstrip('/').rsplit('/', 1)[-1]; product = next((item for item in data.get('products', []) if str(item.get('id')) == identifier), None)
        if not product: return send_json(self, {'error': 'Product not found'}, 404)
        data['products'].remove(product); audit(data, session, 'product_delete', f'{product["name"]} deleted via API'); write_db(data); return send_json(self, {'deleted': product['id']})

    def do_HEAD(self):
        """
        Handle HEAD requests used by Render health checks
        and web clients.

        HEAD returns the same headers as GET but without
        sending the response body.
        """
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.send_header(
                "Cache-Control",
                "no-cache"
            )
            self.end_headers()

        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.rate_limited(): return self.send_error(429, 'Please slow down and try again.')
        parsed = urlparse(self.path); data = read_db(); session = self.session(); session['_ip'] = self.client_address[0]
        if session.get('customer_id') and not session.get('admin') and session.get('email'):
            legacy_user = next((item for item in data.get('users', []) if item.get('email', '').lower() == session.get('email', '').lower() and is_admin_role(item.get('role'))), None)
            if legacy_user:
                session['role'] = legacy_user.get('role', 'CUSTOMER')
                session['admin'] = is_admin_role(session['role'])
        requested_theme = parse_qs(parsed.query).get('theme', [session.get('theme', data.get('shop', {}).get('default_theme', 'system'))])[0]
        session['theme'] = requested_theme if requested_theme in ('light', 'dark', 'system') else 'system'
        data['_theme'] = session['theme']
        authenticated = bool(session.get('customer_id'))
        data['_cart_count'] = sum(item.get('quantity', 0) for item in session.get('cart', [])) if authenticated else 0
        wishlist_key = str(session.get('customer_id')) if authenticated else ''
        data['_wishlist_count'] = len(data.get('wishlists', {}).get(wishlist_key, [])) if authenticated else 0
        data['_account_link'] = '/account' if authenticated else '/login'
        data['_wishlist_link'] = '/wishlist' if authenticated else '/login?message=Please+sign+in+to+view+your+wishlist'
        data['_cart_link'] = '/cart' if authenticated else '/login?message=Please+sign+in+to+view+your+bag'
        data['_auth_link'] = '<a href="/logout" aria-label="Sign out" title="Sign out">↪<span>Sign out</span></a>' if authenticated else ''
        data['_is_admin'] = bool(session.get('admin'))
        data['_admin_role'] = session.get('role')
        data['_admin_email'] = session.get('email', '')
        data['_customer_id'] = session.get('customer_id') if session.get('customer_id') and not session.get('admin') else None
        if parsed.path == '/styles.css':
            self.send_response(200); self.send_header('Content-Type','text/css'); self.end_headers(); self.wfile.write((ROOT/'styles.css').read_bytes()); return
        if parsed.path == '/robots.txt':
            self.send_response(200); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.end_headers(); self.wfile.write(b'User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api\nSitemap: /sitemap.xml\n'); return
        if parsed.path == '/sitemap.xml':
            urls = ['<url><loc>http://localhost:8000/</loc></url>'] + [f'<url><loc>http://localhost:8000/product?id={product["id"]}</loc></url>' for product in data.get('products', [])]
            body = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(urls) + '</urlset>').encode()
            self.send_response(200); self.send_header('Content-Type', 'application/xml'); self.end_headers(); self.wfile.write(body); return
        if parsed.path.startswith('/uploads/'):
            filename = Path(unquote(parsed.path.removeprefix('/uploads/'))).name; target = ROOT / 'uploads' / filename
            if not target.is_file(): return self.send_error(404, 'Image not found.')
            self.send_response(200); self.send_header('Content-Type', 'image/' + target.suffix.lower().removeprefix('.')); self.send_header('Cache-Control', 'public, max-age=86400'); self.end_headers(); self.wfile.write(target.read_bytes()); return
        message = parse_qs(parsed.query).get('message',[''])[0]
        params = parse_qs(parsed.query)
        page_permission = {'/admin': 'reports', '/admin/products': 'products', '/admin/products/edit': 'products', '/admin/categories': 'products', '/admin/inventory': 'inventory', '/admin/orders': 'orders', '/admin/customers': 'customers', '/admin/payments': 'payments', '/admin/deliveries': 'deliveries', '/admin/returns': 'refunds', '/admin/notifications': 'content', '/admin/search': 'content', '/admin/engagement': 'content', '/admin/expenses': 'expenses', '/admin/promotions': 'promotions', '/admin/reviews': 'content', '/admin/health': 'content', '/admin/security': 'content', '/admin/integrations': 'content', '/admin/assistant': 'reports', '/admin/reports': 'reports', '/admin/reports.csv': 'reports', '/admin/reports.xlsx': 'reports', '/admin/reports.pdf': 'reports', '/admin/reports.print': 'reports', '/admin/settings': 'content', '/admin/staff': 'content', '/admin/audit': 'content', '/admin/backup': 'content', '/admin/restore': 'content'}.get(parsed.path)
        if page_permission and session.get('admin') and not role_can(data, session, page_permission):
            return self.send_error(403, 'Your role does not have access to this admin area.')
        if parsed.path == '/api/categories': return send_json(self, [{'name': name, 'subcategories': subcategories} for name, subcategories in category_groups(data).items()])
        if parsed.path == '/api/settings': return send_json(self, data.get('shop', {}))
        if parsed.path == '/api/delivery-quote':
            if not session.get('customer_id'): return send_json(self, {'error': 'Sign in required'}, 401)
            subtotal = sum(cart_item_price(data, item) * item.get('quantity', 0) for item in session.get('cart', []))
            quote = delivery_quote(data, params.get('location', [''])[0], subtotal, session.get('cart', []))
            return send_json(self, {'zone': (quote['zone'] or {}).get('name', 'Other Kenya'), 'fee': float(quote['fee']), 'reason': quote['reason'], 'subtotal': subtotal, 'total': subtotal + float(quote['fee'])})
        if parsed.path == '/api/products':
            page = max(1, int(params.get('page', ['1'])[0])); per_page = min(100, max(1, int(params.get('per_page', ['24'])[0])))
            search = params.get('q', [''])[0].lower(); category = params.get('category', [''])[0]; brand = params.get('brand', [''])[0]
            products = [product for product in data.get('products', []) if (not search or search in f'{product.get("name", "")} {product.get("brand", "")} {product.get("sku", "")}'.lower()) and (not category or product.get('category') == category) and (not brand or product.get('brand') == brand) and product.get('active', True)]
            start = (page - 1) * per_page
            return send_json(self, {'items': products[start:start + per_page], 'page': page, 'per_page': per_page, 'total': len(products)})
        if parsed.path.startswith('/api/products/'):
            identifier = parsed.path.rsplit('/', 1)[-1]
            product = next((product for product in data.get('products', []) if str(product.get('id')) == identifier or product_slug(product) == identifier), None)
            return send_json(self, product or {'error': 'Product not found'}, 200 if product else 404)
        if parsed.path == '/api/orders':
            if not session.get('admin'): return send_json(self, {'error': 'Admin sign-in required'}, 403)
            return send_json(self, data.get('orders', []))
        if parsed.path == '/admin' and not session.get('admin'):
            body = login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin': body = operations_dashboard(data, message, params)
        elif parsed.path == '/admin/settings': body = branding_settings(data, message, params.get('edit_zone', [''])[0]) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/orders': body = admin_orders(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/payments': body = admin_payments(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/deliveries': body = admin_deliveries(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/returns': body = admin_returns(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/notifications': body = admin_notifications(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/search': body = admin_search(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/expenses': body = admin_expenses(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/health': body = admin_health(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/security': body = admin_security(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/integrations': body = admin_integrations(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/assistant':
            question = params.get('q', [''])[0]
            body = admin_assistant(data, (question, assistant_answer(data, question))) if session.get('admin') and question else admin_assistant(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/promotions': body = promotions_manager(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/reviews': body = reviews_manager(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/engagement': body = admin_engagement(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/customers': body = admin_customers(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/products': body = admin_products(data, message, params.get('q', [''])[0]) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/inventory': body = admin_inventory(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/products/edit':
            product = next((p for p in data['products'] if str(p['id']) == params.get('id', [''])[0]), None)
            body = login_page(data, 'Admin sign-in required.') if not session.get('admin') else admin_product_edit(data, product, message) if product else layout(data, '<main class="empty"><h1>Product not found</h1><a class="primary" href="/admin">Return to admin</a></main>')
        elif parsed.path == '/admin/categories': body = category_manager(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/audit': body = admin_audit_logs(data) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/staff': body = admin_staff(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/restore': body = admin_restore(data, message) if session.get('admin') else login_page(data, 'Admin sign-in required.')
        elif parsed.path == '/admin/backup':
            if not session.get('admin'): body = login_page(data, 'Admin sign-in required.')
            else:
                requested = params.get('download', [''])[0]
                target = (ROOT / 'backups' / Path(requested).name).resolve() if requested else None
                if target and target.parent == (ROOT / 'backups').resolve() and target.is_file():
                    self.send_response(200); self.send_header('Content-Type', 'application/sql' if target.suffix == '.sql' else 'application/json'); self.send_header('Content-Disposition', f'attachment; filename={target.name}'); self.end_headers(); self.wfile.write(target.read_bytes()); return
                body = admin_restore(data, message)
        elif parsed.path == '/admin/reports' and session.get('admin'):
            report = params.get('type', ['sales'])[0]
            body = report_dashboard(data, report)
        elif parsed.path == '/admin/reports.csv' and session.get('admin'):
            csv = report_csv(data, params.get('type', ['orders'])[0]); self.send_response(200); self.send_header('Content-Type', 'text/csv'); self.send_header('Content-Disposition', 'attachment; filename=report.csv'); self.end_headers(); self.wfile.write(csv.encode()); return
        elif parsed.path == '/admin/reports.xlsx' and session.get('admin'):
            report = params.get('type', ['orders'])[0]; output = report_xlsx(data, report); self.send_response(200); self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'); self.send_header('Content-Disposition', f'attachment; filename={report}-report.xlsx'); self.end_headers(); self.wfile.write(output); return
        elif parsed.path == '/admin/reports.pdf' and session.get('admin'):
            report = params.get('type', ['orders'])[0]; output = report_pdf(data, report); self.send_response(200); self.send_header('Content-Type', 'application/pdf'); self.send_header('Content-Disposition', f'attachment; filename={report}-report.pdf'); self.end_headers(); self.wfile.write(output); return
        elif parsed.path == '/admin/reports.print' and session.get('admin'):
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write(report_print_page(data, params.get('type', ['orders'])[0]).encode()); return
        elif parsed.path == '/login': body = login_page(data, message)
        elif parsed.path == '/forgot-password': body = forgot_password_page(data, message)
        elif parsed.path == '/reset-password': body = reset_password_page(data, params.get('email', [''])[0], message)
        elif parsed.path == '/register': body = register_page(data, message)
        elif parsed.path in ('/account', '/account/orders'): body = account_page(data, session, message, parsed.path == '/account/orders')
        elif parsed.path == '/tracking': body = tracking_page(data, session, params.get('order', [''])[0]) if session.get('customer_id') else login_page(data, 'Please sign in to track your orders.')
        elif parsed.path == '/wishlist': body = wishlist_page(data, session) if authenticated else login_page(data, 'Please sign in to view your wishlist.')
        elif parsed.path == '/logout':
            session.clear(); session['cart'] = []; body = login_page(data, 'You have been signed out.')
        elif parsed.path == '/product':
            product = next((p for p in data['products'] if str(p['id']) == params.get('id', [''])[0]), None)
            body = product_page(data, product, session) if product else layout(data, '<main class="empty"><h1>Product not found</h1></main>')
        elif parsed.path.startswith('/shop/'):
            product = next((p for p in data['products'] if product_slug(p) == parsed.path.rstrip('/').rsplit('/', 1)[-1]), None)
            body = product_page(data, product, session) if product else layout(data, '<main class="empty"><h1>Product not found</h1></main>')
        elif parsed.path == '/cart': body = cart_page(data, session) if authenticated else login_page(data, 'Please sign in to view your bag.')
        elif parsed.path == '/checkout': body = checkout_page(data, session, message, params.get('payment', [''])[0] == 'whatsapp') if session.get('customer_id') else login_page(data, 'Please sign in or create an account before placing an order.')
        elif parsed.path in ('/receipt', '/invoice'):
            order_number = params.get('order', [''])[0]
            order = next((item for item in data.get('orders', []) if item.get('order_number') == order_number), None)
            owns_order = order and (session.get('admin') or order.get('customer_id') == session.get('customer_id'))
            if not owns_order:
                return self.send_error(404, 'Receipt not found.')
            document_label = 'Invoice' if parsed.path == '/invoice' else 'Receipt'
            document = receipt_pdf(data, order, document_label)
            self.send_response(200); self.send_header('Content-Type', 'application/pdf'); self.send_header('Content-Disposition', f'attachment; filename={document_label.lower()}-{order_number}.pdf'); self.send_header('Content-Length', str(len(document))); self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(document); return
        elif parsed.path == '/order-confirmation': body = order_confirmation_page(data, params.get('order', [''])[0], session.pop('whatsapp_order_url', '')) if params.get('order', [''])[0] else info_page(data, 'ORDER CONFIRMATION', 'Thank you for your order.', 'Your order has been received.', '<a class="primary" href="/account/orders">View my orders ↗</a>')
        elif parsed.path == '/assistant': body = customer_assistant(data, session, params.get('q', [''])[0]) if session.get('customer_id') and not session.get('admin') else login_page(data, 'Please sign in to use customer care.')
        elif parsed.path == '/search':
            query_text = params.get('q', [''])[0].strip().lower()
            result_count = sum(1 for product in data.get('products', []) if query_text and query_text in f'{product.get("name", "")} {product.get("brand", "")} {product.get("category", "")} {product.get("subcategory", "")} {product.get("sku", "")} {product.get("description", "")} {" ".join(product.get("tags", []))}'.lower())
            record_search(data, query_text, result_count); write_db(data); body = search_page(data, params)
        elif parsed.path == '/shop': body = home(data, params)
        elif parsed.path == '/categories': body = categories_page(data)
        elif parsed.path.startswith('/category/'):
            requested_category = unquote(parsed.path.removeprefix('/category/')).replace('-', ' ').lower()
            category = next((name for name in category_groups(data) if name.lower() == requested_category), requested_category.upper())
            body = category_page(data, category, params.get('q', [''])[0])
        elif parsed.path == '/about': body = info_page(data, 'OUR STORY', 'Beautiful things for everyday living.', data['shop'].get('description', ''), f'<p>{esc(data["shop"].get("tagline", "Thoughtful objects and considered rituals for living well."))}</p>')
        elif parsed.path == '/contact': body = info_page(data, 'CONTACT', 'We are here to help.', 'Questions about an order, product or delivery? Our team would love to hear from you.', f'<div class="contact-list"><p><strong>Email</strong><br>{esc(data["shop"].get("email", ""))}</p><p><strong>Phone</strong><br>{esc(data["shop"].get("phone", ""))}</p><p><strong>WhatsApp</strong><br>{esc(data["shop"].get("whatsapp", ""))}</p><p><strong>Visit</strong><br>{esc(data["shop"].get("location", ""))}</p></div>')
        elif parsed.path == '/faq': body = info_page(data, 'FAQ', 'A little clarity goes a long way.', 'Answers to the questions our clients ask most.', '<div class="faq-list"><details open><summary>How quickly do you deliver?</summary><p>{}</p></details><details><summary>Can I return an item?</summary><p>{}</p></details><details><summary>How can I contact the team?</summary><p>Reach us by phone, email or WhatsApp and we will be happy to help.</p></details></div>'.format(esc(data['shop'].get('delivery_information', 'We deliver as quickly as possible.')), esc(data['shop'].get('return_policy', 'Returns are accepted for eligible unused items.'))))
        elif parsed.path == '/delivery': body = info_page(data, 'DELIVERY', 'The details, beautifully handled.', 'Everything you need to know about receiving your order.', f'<p>{esc(data["shop"].get("delivery_information", "Delivery information will be confirmed with your order."))}</p>')
        elif parsed.path == '/returns': body = info_page(data, 'RETURNS & REFUNDS', 'A considered returns policy.', 'We want every purchase to feel right.', f'<p>{esc(data["shop"].get("return_policy", "Returns information will be confirmed with your order."))}</p>')
        elif parsed.path == '/privacy': body = info_page(data, 'PRIVACY', 'Your trust matters.', 'How we use and protect your information.', f'<p>{esc(data["shop"].get("privacy_policy", "Your information is used only to process orders and provide support."))}</p>')
        elif parsed.path == '/terms': body = info_page(data, 'TERMS', 'Clear terms for a better experience.', 'The simple principles behind every order.', f'<p>{esc(data["shop"].get("terms", "Orders are subject to availability and confirmation."))}</p>')
        elif parsed.path in ('/', ''): body = home(data, params)
        else: return self.send_error(404, 'That page does not exist.')
        token = self.csrf_token(session)
        body = body.replace('method="post"', f'method="post"><input type="hidden" name="csrf" value="{token}"') if 'method="post"' in body else body
        secure = '; Secure' if self.secure_cookie() else ''
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Set-Cookie', f'luxe_session={self.session_token}; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_SECONDS}; Path=/{secure}'); self.send_header('X-Content-Type-Options', 'nosniff'); self.send_header('X-Frame-Options', 'DENY'); self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin'); self.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains' if os.environ.get('APP_ENV') == 'production' else 'max-age=0'); self.send_header('Content-Security-Policy', "default-src 'self' https://images.unsplash.com https://wa.me https://fonts.googleapis.com https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; form-action 'self' https://wa.me; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(body.encode())
    def do_POST(self):
        if self.rate_limited(): return self.send_error(429, 'Please slow down and try again.')
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_REQUEST_BYTES: return self.send_error(413)
        data = read_db(); session = self.session(); session['_ip'] = self.client_address[0]; raw = self.rfile.read(length); form = parse_form(self.headers.get('Content-Type', ''), raw); action = form.get('action',[''])[0]; message = ''
        if self.path == '/api/payments/mpesa/callback':
            try:
                callback_data = json.loads(raw.decode('utf-8'))
                if apply_mpesa_callback(data, callback_data): write_db(data)
            except (ValueError, UnicodeDecodeError, TypeError):
                pass
            self.send_response(204); self.end_headers(); return
        if self.path == '/api/products' and self.headers.get('Content-Type', '').startswith('application/json'):
            if not session.get('admin'): return send_json(self, {'error': 'Admin sign-in required'}, 403)
            try: payload = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError): return send_json(self, {'error': 'Invalid JSON'}, 400)
            product_id = max([item.get('id', 0) for item in data.get('products', [])] or [0]) + 1; payload['id'] = product_id; payload.setdefault('stock', 0); payload.setdefault('price', 0); payload.setdefault('active', True); data.setdefault('products', []).append(payload); audit(data, session, 'product_create', f'{payload.get("name", "Product")} created via API'); write_db(data); return send_json(self, payload, 201)
        csrf_valid = hmac.compare_digest(form.get('csrf', [''])[0], self.csrf_token(session))
        if not csrf_valid and action not in ('login', 'register'):
            return self.send_error(403, 'Your form session expired. Please refresh and try again.')
        if action in ('settings', 'branding', 'search_settings', 'delivery_settings', 'delivery_zone_save', 'delivery_zone_toggle', 'delivery_zone_delete', 'backup_now', 'role_save', 'role_delete', 'notification_settings', 'email_invoice', 'expense_create', 'expense_delete', 'inventory_adjust', 'stock', 'order_status', 'payment_status', 'delivery_status', 'return_request', 'return_status', 'refund_create', 'coupon', 'campaign_create', 'coupon_toggle', 'coupon_delete', 'customer_toggle', 'staff_create', 'staff_toggle', 'review_moderate', 'product_create', 'product_update', 'product_delete', 'categories_save', 'feature', 'restore', 'clear_database') and not session.get('admin'): return self.send_error(403, 'Admin sign-in required.')
        permission = action_permission(action)
        if permission and session.get('admin') and not role_can(data, session, permission):
            return self.send_error(403, f'Your role cannot perform {action.replace("_", " ")}.')
        if action in ('role_save', 'role_delete') and not is_superadmin_role(session.get('role')):
            return self.send_error(403, 'Only a superadmin can manage roles.')
        if action == 'settings': data['shop']['name'] = form.get('shop_name',[data['shop']['name']])[0].strip() or data['shop']['name']; message = 'Shop information saved'
        if action == 'delivery_settings':
            shipping = delivery_settings(data['shop'])
            old_shipping = dict(shipping)
            shipping['enabled'] = form.get('delivery_enabled', ['1'])[0] == '1'
            shipping['method'] = form.get('delivery_method', ['zone'])[0] if form.get('delivery_method', ['zone'])[0] in ('zone', 'weight') else 'zone'
            shipping['free_delivery_enabled'] = form.get('free_delivery_enabled', ['1'])[0] == '1'
            shipping['default_free_threshold'] = max(0, float(form.get('default_free_threshold', [shipping['default_free_threshold']])[0]))
            shipping['default_fee'] = max(0, float(form.get('default_fee', [shipping['default_fee']])[0]))
            data['shop']['free_delivery_threshold'] = shipping['default_free_threshold']
            data['shop']['delivery_fee'] = shipping['default_fee']
            audit(data, session, 'settings_changed', 'Delivery settings changed', 'delivery_settings', 'default', old_shipping, shipping)
            message = 'Delivery settings saved'; destination = '/admin/settings'
        if action == 'notification_settings':
            old_channels = data.setdefault('shop', {}).get('notification_channels', ['dashboard', 'email'])
            allowed_channels = {'dashboard', 'email', 'sms', 'whatsapp'}
            new_channels = [channel for channel in form.get('notification_channel', []) if channel in allowed_channels]
            data['shop']['notification_channels'] = new_channels or ['dashboard']
            audit(data, session, 'settings_changed', 'Notification channels changed', 'notifications', 'channels', old_channels, data['shop']['notification_channels'])
            message = 'Notification channels saved'; destination = '/admin/notifications'
        if action == 'role_save' and is_superadmin_role(session.get('role')):
            role_name = form.get('role_name', [''])[0].strip().upper().replace(' ', '_')
            permissions = [permission for permission in form.get('role_permission', []) if permission in {'products', 'orders', 'inventory', 'customers', 'payments', 'deliveries', 'promotions', 'expenses', 'refunds', 'reports', 'content'}]
            if not role_name or role_name in {'SUPER_ADMIN', 'SUPERADMIN', 'ADMIN'}:
                message = 'Choose a different custom role name'
            else:
                data.setdefault('shop', {}).setdefault('custom_roles', {})[role_name] = permissions
                ROLE_PERMISSIONS[role_name] = set(permissions)
                audit(data, session, 'role_created', f'{role_name} role created', 'role', role_name, None, {'permissions': permissions}); message = 'Custom role saved'
            destination = '/admin/staff'
        if action == 'role_delete' and is_superadmin_role(session.get('role')):
            role_name = form.get('role_name', [''])[0].strip().upper()
            if role_name in data.setdefault('shop', {}).setdefault('custom_roles', {}):
                data['shop']['custom_roles'].pop(role_name, None); ROLE_PERMISSIONS.pop(role_name, None); audit(data, session, 'role_deleted', f'{role_name} role deleted', 'role', role_name); message = 'Custom role deleted'
            destination = '/admin/staff'
        if action == 'search_settings':
            old_keywords = data.setdefault('shop', {}).get('seo_keywords', [])
            keywords = [item.strip() for item in form.get('seo_keywords', [''])[0].replace('\n', ',').split(',') if item.strip()]
            data['shop']['seo_keywords'] = keywords
            audit(data, session, 'settings_changed', 'Search SEO keywords changed', 'search', 'seo_keywords', old_keywords, keywords)
            message = 'Search settings saved'; destination = '/admin/search'
        if action == 'expense_create':
            try: amount = round(float(form.get('expense_amount', ['0'])[0]), 2)
            except ValueError: amount = 0
            if amount <= 0:
                message = 'Expense amount must be greater than zero'
            else:
                expense_id = f'EXP-{datetime.now():%Y%m%d}-{secrets.token_hex(2).upper()}'
                expense = {'id': expense_id, 'date': form.get('expense_date', [f'{datetime.now():%Y-%m-%d}'])[0], 'category': form.get('expense_category', ['Other'])[0], 'description': form.get('expense_description', [''])[0].strip(), 'amount': amount, 'created_by': session.get('customer_name', 'Admin')}
                data.setdefault('expenses', []).append(expense); audit(data, session, 'expense_created', f'{expense_id} recorded', 'expense', expense_id, None, expense); message = 'Expense recorded'
            destination = '/admin/expenses'
        if action == 'expense_delete':
            expense_id = form.get('expense_id', [''])[0]; expense = next((item for item in data.get('expenses', []) if item.get('id') == expense_id), None)
            if expense:
                data['expenses'].remove(expense); audit(data, session, 'expense_deleted', f'{expense_id} deleted', 'expense', expense_id, expense, None); message = 'Expense deleted'
            destination = '/admin/expenses'
        if action == 'delivery_zone_save':
            shipping = delivery_settings(data['shop'])
            zone_id = form.get('zone_id', [''])[0].strip() or secrets.token_hex(4)
            zone = next((item for item in shipping['zones'] if str(item.get('id')) == zone_id), None)
            if not zone:
                zone = {'id': zone_id}
                shipping['zones'].append(zone)
            zone.update({'name': form.get('zone_name', [''])[0].strip(), 'locations': [place.strip() for place in form.get('zone_locations', [''])[0].split(',') if place.strip()], 'base_fee': max(0, float(form.get('zone_base_fee', ['0'])[0])), 'free_threshold': max(0, float(form.get('zone_free_threshold', ['0'])[0])), 'calculation_type': form.get('zone_calculation_type', ['fixed'])[0] if form.get('zone_calculation_type', ['fixed'])[0] in ('fixed', 'weight') else 'fixed', 'first_weight_kg': max(0, float(form.get('zone_first_weight', ['2'])[0])), 'additional_weight_fee': max(0, float(form.get('zone_additional_weight_fee', ['0'])[0])), 'active': form.get('zone_active', ['1'])[0] == '1'})
            zone['shipping_class_fees'] = {shipping_class: max(0, float(form.get(f'class_fee_{shipping_class.lower().replace(" ", "_")}', ['0'])[0] or 0)) for shipping_class in SHIPPING_CLASSES if shipping_class not in ('Free Delivery', 'Pickup Only') and form.get(f'class_fee_{shipping_class.lower().replace(" ", "_")}', [''])[0].strip()}
            message = 'Delivery zone saved'; destination = '/admin/settings'
        if action in ('delivery_zone_toggle', 'delivery_zone_delete'):
            shipping = delivery_settings(data['shop'])
            zone = next((item for item in shipping['zones'] if str(item.get('id')) == form.get('zone_id', [''])[0]), None)
            if zone and action == 'delivery_zone_toggle': zone['active'] = not zone.get('active', True); message = 'Delivery zone status updated'
            elif zone: shipping['zones'].remove(zone); message = 'Delivery zone deleted'
            destination = '/admin/settings'
        if action == 'branding':
            old_shop = {key: data['shop'].get(key) for key in ('name', 'currency', 'delivery_fee', 'free_delivery_threshold', 'logo', 'default_theme')}
            for key in ('name','tagline','description','phone','whatsapp','email','location','business_hours','currency','country','delivery_fee','free_delivery_threshold','order_prefix','invoice_prefix','receipt_prefix','backup_schedule','backup_time','backup_day','backup_retention','logo','favicon','delivery_information','return_policy','privacy_policy','terms','story_heading','story_description','newsletter_heading','newsletter_description'):
                if key in form: data['shop'][key] = form[key][0].strip()
            for key in ('instagram', 'facebook', 'tiktok', 'x', 'youtube'):
                form_key = f'social_{key}'
                if form_key in form: data['shop'].setdefault('social', {})[key] = form[form_key][0].strip()
            logo_files = [item.strip() for item in form.get('logo_file', []) if item.strip()]
            if logo_files:
                logo = logo_files[0]
                if not logo.startswith(('/uploads/', 'https://', 'http://')):
                    logo = '/uploads/' + Path(logo).name
                data['shop']['logo'] = logo
            integrations = data.setdefault('integrations', {})
            for key in ('environment', 'shortcode', 'consumer_key', 'callback_url'):
                form_key = f'mpesa_{key}'
                if form_key in form: integrations[form_key] = form[form_key][0].strip()
            for key in ('consumer_secret', 'passkey'):
                form_key = f'mpesa_{key}'
                if form.get(form_key, [''])[0].strip(): integrations[form_key] = form[form_key][0].strip()
            for key in ('host', 'port', 'user', 'from'):
                form_key = f'smtp_{key}'
                if form_key in form: integrations[form_key] = form[form_key][0].strip()
            if form.get('smtp_password', [''])[0].strip(): integrations['smtp_password'] = form['smtp_password'][0].strip()
            if form.get('default_theme', ['system'])[0] in ('system', 'light', 'dark'):
                data['shop']['default_theme'] = form['default_theme'][0]
            if form.get('_upload_errors'):
                message = form['_upload_errors'][0]
            for key in ('heading', 'description', 'image', 'primary_label', 'primary_link', 'secondary_label', 'secondary_link'):
                form_key = f'hero_{key}'
                if form_key in form: data['shop'].setdefault('hero', {})[key] = form[form_key][0].strip()
            if os.name == 'nt':
                threading.Thread(target=update_windows_backup_schedule_background, args=(dict(data['shop']),), daemon=True).start()
            message = 'Shop branding saved'; destination = '/admin/settings'
            new_shop = {key: data['shop'].get(key) for key in old_shop}
            audit(data, session, 'settings_changed', 'Shop settings changed', 'shop', 'settings', old_shop, new_shop)
        if action == 'categories_save':
            updated_categories = {}
            for index, category in enumerate(category_groups(data)):
                subcategories = [line.strip() for line in form.get(f'category_{index}', [''])[0].splitlines() if line.strip()]
                if subcategories: updated_categories[category] = subcategories
            if len(updated_categories) != len(category_groups(data)):
                message = 'Each category must contain at least one subcategory'; destination = '/admin/categories'
            else:
                data['category_hierarchy'] = updated_categories; message = 'Category hierarchy saved'; destination = '/admin/categories'
        if action == 'backup_now':
            target = create_backup(data)
            audit(data, session, 'backup_created', f'Backup {target.name} created')
            message = f'Backup created: {target.name}'; destination = '/admin/restore'
        if action == 'restore':
            try:
                restored = validate_backup(form.get('backup_file', [''])[0])
                data = restored; audit(data, session, 'restore', 'Database backup restored'); message = 'Database restored'; destination = '/admin'
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                message = 'Invalid backup file'; destination = '/admin/restore'
        if action == 'clear_database':
            if not is_superadmin_role(session.get('role')):
                message = 'Only a superadmin can clear operational data'
            elif form.get('clear_confirmation', [''])[0] != 'CLEAR DATABASE':
                message = 'Type CLEAR DATABASE to confirm'
            else:
                preserved = {key: data[key] for key in ('shop', 'category_hierarchy', 'categories', 'users', 'integrations') if key in data}
                data = preserved
                for key in ('customers', 'orders', 'products', 'wishlists', 'reviews', 'coupons', 'audit_logs', 'inventory_movements', 'newsletter_subscribers', 'password_reset_otps'):
                    data[key] = [] if key not in ('wishlists', 'password_reset_otps') else {}
                audit(data, session, 'database_cleared', 'Operational database data cleared by superadmin')
                message = 'Operational database data cleared'; destination = '/admin'
        if action == 'stock':
            product = next((p for p in data['products'] if str(p['id']) == form.get('product_id',[''])[0]), None)
            if product:
                previous = product['stock']; product['stock'] = max(0, product['stock'] + int(form.get('amount',[0])[0])); data.setdefault('inventory_movements', []).append({'product_id': product['id'], 'quantity_change': product['stock'] - previous, 'previous_stock': previous, 'new_stock': product['stock'], 'reason': 'Admin adjustment', 'user': session.get('customer_id'), 'date': datetime.now().isoformat(timespec='seconds')}); audit(data, session, 'stock_adjusted', f'{product["name"]} stock adjusted', 'product', product['id'], {'stock': previous}, {'stock': product['stock'], 'reason': 'Admin adjustment'}); notify_event(data, 'out_of_stock' if product['stock'] == 0 else 'low_stock' if product['stock'] <= product.get('minimum_stock', 5) else 'stock_adjusted', 'Inventory alert', f'{product["name"]} stock is now {product["stock"]}', 'product', product['id']); message = f"{product['name']} inventory updated"
        if action == 'inventory_adjust':
            product = next((item for item in data.get('products', []) if str(item.get('id')) == form.get('product_id', [''])[0]), None)
            try: amount = int(form.get('amount', ['0'])[0])
            except ValueError: amount = 0
            reason = form.get('reason', ['Admin adjustment'])[0].strip() or 'Admin adjustment'
            if product and amount:
                previous = product.get('stock', 0); product['stock'] = max(0, previous + amount); change = product['stock'] - previous
                movement = {'product_id': product['id'], 'quantity_change': change, 'previous_stock': previous, 'new_stock': product['stock'], 'reason': reason, 'user': session.get('customer_name', 'Admin'), 'date': datetime.now().isoformat(timespec='seconds')}
                data.setdefault('inventory_movements', []).append(movement); audit(data, session, 'stock_adjusted', f'{product["name"]} stock adjusted', 'product', product['id'], {'stock': previous}, {'stock': product['stock'], 'reason': reason}); message = f'{product["name"]} inventory updated'
            else: message = 'Enter a valid stock adjustment'
            destination = '/admin/inventory'
        if action == 'feature':
            product = next((p for p in data['products'] if str(p['id']) == form.get('product_id', [''])[0]), None)
            section = form.get('section', [''])[0]
            if product and section in HOMEPAGE_SECTIONS:
                sections = product.setdefault('featured_sections', [])
                if section in sections: sections.remove(section); message = f'{product["name"]} removed from {section}'
                else: sections.append(section); message = f'{product["name"]} added to {section}'
        if action == 'cart':
            product = next((p for p in data['products'] if str(p['id']) == form.get('product_id',[''])[0]), None)
            quantity = max(1, int(form.get('quantity', ['1'])[0]))
            variant_id = form.get('variant_id', [''])[0]
            variant = next((v for v in product.get('variants', []) if str(v.get('id')) == variant_id and v.get('active', True)), None) if product and variant_id else None
            available_stock = variant.get('stock', 0) if variant else product.get('stock', 0) if product else 0
            if not session.get('customer_id'):
                if product and available_stock >= quantity:
                    session['pending_cart'] = {'id': product['id'], 'variant_id': int(variant_id) if variant else None, 'quantity': quantity}
                message = 'Please sign in or create an account before adding items to your bag'
                destination = '/login'
            elif product and available_stock >= quantity:
                existing = next((item for item in session['cart'] if item['id'] == product['id'] and item.get('variant_id') == (int(variant_id) if variant else None)), None)
                if existing: existing['quantity'] = min(available_stock, existing['quantity'] + quantity)
                else: session['cart'].append({'id': product['id'], 'variant_id': int(variant_id) if variant else None, 'quantity': quantity})
                message = 'Product added to bag'
            else: message = 'Product is out of stock'
        if action == 'cart_remove':
            if not session.get('customer_id'):
                message = 'Please sign in to manage your bag'; destination = '/login'
            else:
                product_id = int(form.get('product_id', [0])[0]); variant_id = form.get('variant_id', [''])[0]; session['cart'] = [item for item in session['cart'] if not (item['id'] == product_id and (not variant_id or str(item.get('variant_id', '')) == variant_id))]
        if action == 'cart_update':
            if not session.get('customer_id'):
                message = 'Please sign in to manage your bag'; destination = '/login'
            else:
                product_id = int(form.get('product_id', [0])[0]); variant_id = form.get('variant_id', [''])[0]; change = int(form.get('change', [0])[0])
                product = next((p for p in data['products'] if p['id'] == product_id), None)
                variant = next((v for v in product.get('variants', []) if str(v.get('id')) == variant_id), None) if product and variant_id else None
                item = next((item for item in session['cart'] if item['id'] == product_id and str(item.get('variant_id', '')) == variant_id), None)
                available_stock = variant.get('stock', 0) if variant else product.get('stock', 0) if product else 0
                if product and item: item['quantity'] = min(available_stock, max(0, item['quantity'] + change))
                session['cart'] = [item for item in session['cart'] if item['quantity'] > 0]
        if action == 'register':
            email = form.get('email', [''])[0].strip().lower()
            if any(c['email'] == email for c in data.get('customers', [])):
                message = 'An account with that email already exists'; destination = '/register'
            else:
                customer = {'id': max([c['id'] for c in data.get('customers', [])] or [0]) + 1, 'name': form.get('name', [''])[0].strip(), 'email': email, 'phone': form.get('phone', [''])[0].strip(), 'password_hash': hash_password(form.get('password', [''])[0]), 'active': True, 'created_at': datetime.now().isoformat(timespec='seconds')}
                data.setdefault('customers', []).append(customer); notify_event(data, 'new_customer', 'New customer', f'{customer["name"]} created an account', 'customer', customer['id']); session['customer_id'] = customer['id']; session['customer_name'] = customer['name']; session['email'] = email; had_pending = bool(session.get('pending_cart')); add_pending_cart(data, session); message = 'Account created. Your item is in the bag.' if had_pending else 'Account created'; destination = '/cart' if had_pending else '/account'
        if action == 'forgot_request':
            email = form.get('email', [''])[0].strip().lower()
            user = next((u for u in data.get('users', []) + data.get('customers', []) if u.get('email', '').lower() == email and u.get('active', True)), None)
            if user:
                code = f'{secrets.randbelow(1000000):06d}'
                try:
                    send_password_otp(data, email, code)
                    data.setdefault('password_reset_otps', {})[email] = {'hash': hashlib.sha256(code.encode()).hexdigest(), 'expires_at': time.time() + 600, 'attempts': 0}
                except (OSError, RuntimeError, smtplib.SMTPException):
                    pass
            destination = '/forgot-password?message=' + urlencode({'message': 'If that email is registered, an OTP has been sent.'}).split('=', 1)[-1]
            if user and email in data.get('password_reset_otps', {}):
                destination = '/reset-password?email=' + urlencode({'email': email}).split('=', 1)[-1]
        if action == 'forgot_reset':
            email = form.get('email', [''])[0].strip().lower()
            otp = form.get('otp', [''])[0].strip()
            reset = data.get('password_reset_otps', {}).get(email)
            if not reset or reset.get('expires_at', 0) < time.time() or reset.get('attempts', 0) >= 5:
                message = 'This OTP is invalid or expired'; destination = '/forgot-password'
            else:
                reset['attempts'] = reset.get('attempts', 0) + 1
                if not hmac.compare_digest(reset.get('hash', ''), hashlib.sha256(otp.encode()).hexdigest()):
                    message = 'This OTP is invalid or expired'; destination = '/reset-password?email=' + urlencode({'email': email}).split('=', 1)[-1]
                else:
                    user = next((u for u in data.get('users', []) + data.get('customers', []) if u.get('email', '').lower() == email and u.get('active', True)), None)
                    if not user or len(form.get('password', [''])[0]) < 8:
                        message = 'Enter a valid new password'; destination = '/reset-password?email=' + urlencode({'email': email}).split('=', 1)[-1]
                    else:
                        user['password_hash'] = hash_password(form['password'][0]); data['password_reset_otps'].pop(email, None); message = 'Password reset successfully'; destination = '/login'
        if action == 'login':
            email = form.get('email', [''])[0].strip().lower()
            user = next((u for u in data.get('users', []) + data.get('customers', []) if u.get('email') == email and u.get('active', True)), None)
            if user and check_password(form.get('password', [''])[0], user.get('password_hash', '')):
                session.pop('admin', None); session.pop('role', None); session.pop('email', None)
                session['customer_id'] = user['id']; session['customer_name'] = user.get('name', 'Customer'); session['email'] = user.get('email', email); session['role'] = user.get('role', 'CUSTOMER'); session['admin'] = is_admin_role(session['role']); audit(data, session, 'admin_login' if session['admin'] else 'login', f'{user.get("email", email)} signed in', 'user', user['id'], None, {'role': session['role']}); had_pending = bool(session.get('pending_cart')); add_pending_cart(data, session); destination = '/admin' if session['admin'] else '/cart' if had_pending else '/account'
            else: message = 'Invalid email or password'; destination = '/login'
        if action == 'account_update' and session.get('customer_id'):
            account = current_account(data, session)
            if account:
                account['name'] = form.get('name', [account.get('name', '')])[0].strip() or account.get('name', 'Customer')
                account['phone'] = form.get('phone', [account.get('phone', '')])[0].strip()
                session['customer_name'] = account['name']
                message = 'Your details were saved'
            else:
                message = 'Account not found'
            destination = '/account'
        if action == 'account_password' and session.get('customer_id'):
            account = current_account(data, session)
            current_password = form.get('current_password', [''])[0]
            new_password = form.get('new_password', [''])[0]
            confirm_password = form.get('confirm_password', [''])[0]
            if not account or not check_password(current_password, account.get('password_hash', '')):
                message = 'Current password is incorrect'
            elif len(new_password) < 8:
                message = 'New password must be at least 8 characters'
            elif new_password != confirm_password:
                message = 'New passwords do not match'
            else:
                account['password_hash'] = hash_password(new_password)
                message = 'Your password was changed'
            destination = '/account'
        if action == 'wishlist':
            if not session.get('customer_id'):
                message = 'Please sign in to manage your wishlist'; destination = '/login'
            else:
                product_id = int(form.get('product_id', [0])[0]); key = str(session['customer_id'])
                saved = data.setdefault('wishlists', {}).setdefault(key, [])
                if product_id in saved: saved.remove(product_id); message = 'Removed from wishlist'
                else: saved.append(product_id); message = 'Added to wishlist'
                destination = '/wishlist'
        if action == 'newsletter':
            email = form.get('email', [''])[0].strip().lower()
            if '@' not in email or '.' not in email.rsplit('@', 1)[-1]:
                message = 'Please enter a valid email address'
            elif email in data.setdefault('newsletter_subscribers', []):
                message = 'You are already on the list'
            else:
                data['newsletter_subscribers'].append(email); message = 'You are now on the Luxe list'
            destination = '/'
        if action == 'review' and session.get('customer_id'):
            product_id = int(form.get('product_id', [0])[0])
            purchased = any(order.get('customer_id') == session['customer_id'] and any(item.get('product_id') == product_id for item in order.get('items', [])) for order in data.get('orders', []))
            if not purchased:
                message = 'Reviews are available after purchase'; destination = '/product?id=' + str(product_id)
            else:
                already_reviewed = any(r.get('product_id') == product_id and r.get('customer_id') == session['customer_id'] for r in data.get('reviews', []))
                if not already_reviewed:
                    customer = next((c for c in data.get('customers', []) if c['id'] == session['customer_id']), {})
                    review_id = len(data.get('reviews', [])) + 1; data.setdefault('reviews', []).append({'id': review_id, 'product_id': product_id, 'customer_id': session['customer_id'], 'customer_name': customer.get('name', 'Customer'), 'rating': max(1, min(5, int(form.get('rating', ['5'])[0]))), 'text': form.get('text', [''])[0].strip(), 'approved': False, 'created_at': datetime.now().isoformat(timespec='seconds')}); notify_event(data, 'new_review', 'New review', f'{customer.get("name", "Customer")} submitted a review', 'review', review_id)
                    message = 'Review submitted for admin approval'; destination = '/product?id=' + str(product_id)
        if action == 'review' and not session.get('customer_id'):
            message = 'Sign in before reviewing a product'; destination = '/login'
        if action == 'order_status' and session.get('admin'):
            order = next((o for o in data.get('orders', []) if o.get('order_number') == form.get('order_number', [''])[0]), None)
            if order:
                old_status = order.get('status', 'Pending'); order['status'] = form.get('status', ['Pending'])[0]; audit(data, session, 'order_status', f'{order["order_number"]}: {old_status} to {order["status"]}', 'order', order.get('order_number'), {'status': old_status}, {'status': order['status']}); message = 'Order status updated'; destination = '/admin/orders'
        if action == 'email_invoice' and session.get('admin'):
            order = next((item for item in data.get('orders', []) if item.get('order_number') == form.get('order_number', [''])[0]), None)
            if order:
                try:
                    email_document(data, order, 'Invoice'); audit(data, session, 'invoice_emailed', f'Invoice emailed for {order["order_number"]}', 'order', order['order_number']); message = 'Invoice emailed to customer'
                except (OSError, RuntimeError, smtplib.SMTPException) as error:
                    message = f'Invoice email failed: {error}'
            else: message = 'Order not found'
            destination = '/admin/orders'
        if action in ('payment_status', 'delivery_status') and session.get('admin'):
            order = next((o for o in data.get('orders', []) if o.get('order_number') == form.get('order_number', [''])[0]), None)
            if order:
                key = 'payment_status' if action == 'payment_status' else 'delivery_status'
                previous_status = order.get(key, 'Pending')
                order[key] = form.get(key, ['Pending'])[0]
                if action == 'delivery_status' and order[key] == 'Delivered':
                    order['status'] = 'Delivered'
                    if previous_status != 'Delivered':
                        order['delivered_at'] = datetime.now().isoformat(timespec='seconds')
                        threading.Thread(target=notify_order_delivered, args=(data, dict(order)), daemon=True).start()
                audit(data, session, action, f'{order["order_number"]}: {order[key]}', 'order', order.get('order_number'), {key: previous_status}, {key: order[key]})
                if action == 'payment_status': notify_event(data, 'successful_payment' if order[key] == 'Paid' else 'failed_payment' if order[key] == 'Failed' else 'payment_updated', 'Payment update', f'{order["order_number"]} payment is {order[key]}', 'order', order.get('order_number'))
                message = 'Payment updated' if action == 'payment_status' else 'Delivery updated'
                destination = '/admin/payments' if action == 'payment_status' else '/admin/deliveries'
        if action == 'coupon' and session.get('admin'):
            code = form.get('code', [''])[0].strip().upper()
            if not any(c.get('code') == code for c in data.get('coupons', [])):
                coupon_type = form.get('coupon_type', ['percentage'])[0] if form.get('coupon_type', ['percentage'])[0] in ('percentage', 'fixed') else 'percentage'
                try: value = float(form.get('value', ['0'])[0])
                except ValueError: value = 0
                data.setdefault('coupons', []).append({'code': code, 'type': coupon_type, 'value': max(0, value), 'minimum_order': max(0, float(form.get('minimum_order', ['0'])[0] or 0)), 'maximum_discount': max(0, float(form.get('maximum_discount', ['0'])[0] or 0)), 'expiry_date': form.get('expiry_date', [''])[0], 'usage_limit': max(0, int(form.get('usage_limit', ['100'])[0] or 0)), 'per_customer_limit': max(0, int(form.get('per_customer_limit', ['0'])[0] or 0)), 'categories': [item.strip() for item in form.get('coupon_categories', [''])[0].split(',') if item.strip()], 'product_ids': [int(item.strip()) for item in form.get('coupon_products', [''])[0].split(',') if item.strip().isdigit()], 'used': 0, 'active': True})
                message = 'Coupon created'; destination = '/admin/promotions'
        if action == 'campaign_create' and session.get('admin'):
            campaign_id = f'CAM-{datetime.now():%Y%m%d}-{secrets.token_hex(2).upper()}'
            campaign = {'id': campaign_id, 'name': form.get('campaign_name', [''])[0].strip(), 'type': form.get('campaign_type', ['Promotion'])[0], 'starts_at': form.get('campaign_starts', [''])[0], 'ends_at': form.get('campaign_ends', [''])[0], 'active': True, 'created_at': datetime.now().isoformat(timespec='seconds')}
            data.setdefault('campaigns', []).append(campaign); audit(data, session, 'campaign_created', f'{campaign["name"]} created', 'campaign', campaign_id, None, campaign); message = 'Campaign created'; destination = '/admin/promotions'
        if action in ('coupon_toggle', 'coupon_delete') and session.get('admin'):
            code = form.get('code', [''])[0].strip().upper()
            coupon = next((item for item in data.get('coupons', []) if item.get('code') == code), None)
            if coupon and action == 'coupon_toggle':
                coupon['active'] = not coupon.get('active', True); message = 'Coupon status updated'
            elif coupon:
                data['coupons'].remove(coupon); message = 'Coupon deleted'
            destination = '/admin/promotions'
        if action == 'customer_toggle' and session.get('admin'):
            customer = next((c for c in data.get('customers', []) if c['id'] == int(form.get('customer_id', [0])[0])), None)
            if customer: customer['active'] = not customer.get('active', True); message = 'Customer status updated'; destination = '/admin/customers'
        if action == 'staff_create' and session.get('admin'):
            email = form.get('staff_email', [''])[0].strip().lower()
            requested_role = form.get('staff_role', ['STORE_MANAGER'])[0]
            if requested_role not in ROLE_PERMISSIONS and requested_role != 'ADMIN':
                message = 'Invalid staff role'
            elif any(user.get('email') == email for user in data.get('users', [])):
                message = 'A staff account with that email already exists'
            else:
                staff_id = max([user.get('id', 0) for user in data.get('users', [])] or [0]) + 1
                data.setdefault('users', []).append({'id': staff_id, 'name': form.get('staff_name', [''])[0].strip(), 'email': email, 'password_hash': hash_password(form.get('staff_password', [''])[0]), 'role': requested_role, 'active': True}); audit(data, session, 'staff_create', f'{email} staff account created', 'user', staff_id, None, {'role': requested_role}); message = 'Staff account created'
            destination = '/admin/staff'
        if action == 'staff_toggle' and session.get('admin'):
            staff_id = int(form.get('staff_id', [0])[0])
            user = next((item for item in data.get('users', []) if item.get('id') == staff_id), None)
            if is_superadmin_role(session.get('role')) and user and user.get('id') != session.get('customer_id'):
                user['active'] = not user.get('active', True); audit(data, session, 'staff_toggle', f'{user.get("email", "Staff")} status updated'); message = 'Staff status updated'
            destination = '/admin/staff'
        if action == 'review_moderate' and session.get('admin'):
            review = next((r for r in data.get('reviews', []) if r['id'] == int(form.get('review_id', [0])[0])), None); decision = form.get('decision', ['hide'])[0]
            if review and decision == 'delete': data['reviews'].remove(review)
            elif review: review['approved'] = decision == 'approve'; review['hidden'] = decision == 'hide'
            message = 'Review moderation saved'; destination = '/admin/reviews'
        if action == 'return_request' and session.get('admin'):
            order_number = form.get('order_number', [''])[0].strip()
            order = next((item for item in data.get('orders', []) if item.get('order_number') == order_number), None)
            if not order:
                message = 'Order not found'
            else:
                product_id = int(form.get('product_id', ['0'])[0] or 0)
                product = next((item for item in data.get('products', []) if item.get('id') == product_id), None) if product_id else None
                return_id = f'RET-{datetime.now():%Y%m%d}-{secrets.token_hex(2).upper()}'
                data.setdefault('returns', []).append({'id': return_id, 'order_number': order_number, 'customer_id': order.get('customer_id'), 'customer_name': order.get('customer_name', 'Customer'), 'product_id': product_id or None, 'product_name': product.get('name', 'All items') if product else 'All items', 'reason': form.get('return_reason', [''])[0].strip(), 'status': 'Requested', 'created_at': datetime.now().isoformat(timespec='seconds')})
                audit(data, session, 'return_requested', f'{return_id} created for {order_number}', 'return', return_id, None, {'status': 'Requested', 'order_number': order_number}); message = 'Return request created'
            destination = '/admin/returns'
        if action == 'return_status' and session.get('admin'):
            return_id = form.get('return_id', [''])[0]
            return_item = next((item for item in data.get('returns', []) if item.get('id') == return_id), None)
            if return_item:
                old_status = return_item.get('status', 'Requested'); new_status = form.get('return_status', ['Requested'])[0]
                return_item['status'] = new_status; return_item['updated_at'] = datetime.now().isoformat(timespec='seconds')
                audit(data, session, 'return_status', f'{return_id}: {old_status} to {new_status}', 'return', return_id, {'status': old_status}, {'status': new_status}); message = 'Return status updated'
            destination = '/admin/returns'
        if action == 'refund_create' and session.get('admin'):
            order_number = form.get('order_number', [''])[0].strip(); order = next((item for item in data.get('orders', []) if item.get('order_number') == order_number), None)
            try: amount = round(float(form.get('refund_amount', ['0'])[0]), 2)
            except ValueError: amount = 0
            if not order or amount <= 0 or amount > float(order.get('total', 0)):
                message = 'Refund amount must be greater than zero and no more than the order total'
            else:
                reference = f'REF-{datetime.now():%Y%m%d}-{secrets.token_hex(2).upper()}'
                data.setdefault('refunds', []).append({'reference': reference, 'order_number': order_number, 'customer_id': order.get('customer_id'), 'amount': amount, 'method': order.get('payment_method', 'Unknown'), 'reason': form.get('refund_reason', [''])[0].strip(), 'status': 'Pending', 'created_at': datetime.now().isoformat(timespec='seconds')})
                audit(data, session, 'refund_requested', f'{reference} recorded for {order_number}', 'refund', reference, None, {'status': 'Pending', 'amount': amount}); message = 'Pending refund recorded'
            destination = '/admin/returns'
        if action == 'product_create' and session.get('admin'):
            product_id = max([p['id'] for p in data['products']] or [0]) + 1; image_files = [image.strip() for image in form.get('image_file', []) if image.strip()]; image = image_files[0] if image_files else ''
            variants = []
            for index, line in enumerate(form.get('variants', [''])[0].splitlines(), 1):
                parts = [part.strip() for part in line.split('|')]
                if len(parts) == 3 and parts[0]: variants.append({'id': index, 'name': parts[0], 'price': float(parts[1]), 'stock': max(0, int(float(parts[2]))), 'active': True})
            data['products'].append({'id': product_id, 'name': form.get('name', [''])[0].strip(), 'short_description': form.get('short_description', [''])[0].strip(), 'sku': form.get('sku', [''])[0].strip(), 'barcode': form.get('barcode', [''])[0].strip(), 'brand': form.get('brand', [''])[0].strip(), 'category': form.get('category', [''])[0].strip(), 'subcategory': form.get('subcategory', [''])[0].strip(), 'price': float(form.get('price', ['0'])[0]), 'old_price': float(form.get('discount_price', ['0'])[0]) or None, 'cost_price': max(0, float(form.get('cost_price', ['0'])[0] or 0)), 'stock': int(float(form.get('stock', ['0'])[0])), 'minimum_stock': int(float(form.get('minimum_stock', ['5'])[0])), 'shipping_class': form.get('shipping_class', ['Standard'])[0] if form.get('shipping_class', ['Standard'])[0] in SHIPPING_CLASSES else 'Standard', 'weight_kg': max(0, float(form.get('weight_kg', ['0.5'])[0])), 'dimensions': form.get('dimensions', [''])[0].strip(), 'video_url': form.get('video_url', [''])[0].strip(), 'status': form.get('status', ['Active'])[0] if form.get('status', ['Active'])[0] in ('Active', 'Draft', 'Archived') else 'Active', 'active': form.get('status', ['Active'])[0] == 'Active', 'rating': 0, 'tag': 'New', 'image': image, 'images': image_files, 'variants': variants, 'description': form.get('description', [''])[0].strip(), 'tags': [tag.strip() for tag in form.get('tags', [''])[0].split(',') if tag.strip()], 'created_at': datetime.now().isoformat(timespec='seconds')})
            message = 'Product created'; destination = '/admin/products'
        if action == 'product_update' and session.get('admin'):
            product = next((p for p in data['products'] if str(p['id']) == form.get('product_id', [''])[0]), None)
            if product:
                before = {key: product.get(key) for key in ('name', 'sku', 'price', 'old_price', 'stock', 'minimum_stock', 'shipping_class', 'weight_kg')}
                for key in ('name', 'short_description', 'sku', 'barcode', 'brand', 'category', 'subcategory', 'description', 'dimensions', 'video_url'):
                    product[key] = form.get(key, [product.get(key, '')])[0].strip()
                product['price'] = float(form.get('price', [product['price']])[0]); product['old_price'] = float(form.get('discount_price', [product.get('old_price') or 0])[0]) or None; product['cost_price'] = max(0, float(form.get('cost_price', [product.get('cost_price', 0)])[0] or 0)); product['stock'] = max(0, int(float(form.get('stock', [product['stock']])[0]))); product['minimum_stock'] = max(0, int(float(form.get('minimum_stock', [product.get('minimum_stock', 5)])[0]))); product['shipping_class'] = form.get('shipping_class', [product_shipping_class(product)])[0] if form.get('shipping_class', [product_shipping_class(product)])[0] in SHIPPING_CLASSES else product_shipping_class(product); product['weight_kg'] = max(0, float(form.get('weight_kg', [product.get('weight_kg', 0.5)])[0])); product['status'] = form.get('status', [product.get('status', 'Active')])[0] if form.get('status', [product.get('status', 'Active')])[0] in ('Active', 'Draft', 'Archived') else product.get('status', 'Active'); product['active'] = product['status'] == 'Active'; product['tags'] = [tag.strip() for tag in form.get('tags', [', '.join(product.get('tags', []))])[0].split(',') if tag.strip()]
                image_files = [image.strip() for image in form.get('image_file', []) if image.strip()]
                if image_files: product['image'] = image_files[0]; product['images'] = image_files
                variants = []
                for index, line in enumerate(form.get('variants', [''])[0].splitlines(), 1):
                    parts = [part.strip() for part in line.split('|')]
                    if len(parts) == 3 and parts[0]: variants.append({'id': index, 'name': parts[0], 'price': float(parts[1]), 'stock': max(0, int(float(parts[2]))), 'active': True})
                if 'variants' in form: product['variants'] = variants
                after = {key: product.get(key) for key in before}
                audit(data, session, 'product_update', f'{product["name"]} updated', 'product', product['id'], before, after)
                message = 'Product updated'; destination = '/admin'
        if action == 'product_delete' and is_superadmin_role(session.get('role')):
            product = next((p for p in data['products'] if str(p['id']) == form.get('product_id', [''])[0]), None)
            if product:
                data['products'].remove(product); audit(data, session, 'product_delete', f'{product["name"]} deleted'); message = 'Product deleted'; destination = '/admin'
        checkout_discount = 0
        checkout_coupon = None
        checkout_completed = False
        whatsapp_order_url = ''
        payment_error = False
        mpesa_phone = form.get('mpesa_phone', [''])[0].strip() or form.get('phone', [''])[0].strip()
        mpesa_missing = action == 'checkout' and session.get('customer_id') and session.get('cart') and form.get('payment_method', ['Cash on Delivery'])[0] == 'M-Pesa' and not mpesa_phone
        if action == 'checkout' and (not session.get('customer_id') or not session.get('cart')):
            message = 'Please sign in and add at least one item before checking out'; destination = '/login' if not session.get('customer_id') else '/cart'
        if mpesa_missing:
            message = 'Enter the M-Pesa phone number to continue'; destination = '/checkout'
        if action == 'checkout' and session.get('customer_id') and session.get('cart') and not mpesa_missing:
            checkout_coupon = next((coupon for coupon in data.get('coupons', []) if coupon.get('code') == form.get('coupon_code', [''])[0].strip().upper() and coupon.get('active', True)), None)
            if checkout_coupon:
                subtotal_for_coupon = sum(cart_item_price(data, item) * item['quantity'] for item in session['cart'])
                checkout_discount = coupon_discount(data, checkout_coupon, subtotal_for_coupon, session['cart'], session.get('customer_id'))
            order_prefix = str(data.get('shop', {}).get('order_prefix', 'ORD')).strip().upper() or 'ORD'
            order_number = order_prefix + '-' + datetime.now().strftime('%Y%m%d') + '-' + secrets.token_hex(2).upper()
            invoice_prefix = str(data.get('shop', {}).get('invoice_prefix', 'INV')).strip().upper() or 'INV'
            receipt_prefix = str(data.get('shop', {}).get('receipt_prefix', 'RCT')).strip().upper() or 'RCT'
            subtotal = sum(cart_item_price(data, item) * item['quantity'] for item in session['cart'])
            discount = min(checkout_discount, subtotal)
            eligible_subtotal = subtotal - discount
            delivery_location = form.get('location', [''])[0].strip()
            delivery_result = delivery_quote(data, delivery_location, eligible_subtotal, session['cart'])
            delivery_fee = float(delivery_result['fee'])
            order_payment_method = form.get('payment_method', ['M-Pesa'])[0]
            document_suffix = datetime.now().strftime('%Y%m%d') + '-' + secrets.token_hex(2).upper()
            order = {'order_number': order_number, 'invoice_number': f'{invoice_prefix}-{document_suffix}', 'receipt_number': f'{receipt_prefix}-{document_suffix}', 'customer_id': session.get('customer_id'), 'status': 'Pending', 'payment_method': order_payment_method, 'payment_status': 'Pending', 'mpesa_phone': mpesa_phone if order_payment_method == 'M-Pesa' else '', 'subtotal': subtotal, 'coupon_code': checkout_coupon.get('code') if checkout_coupon and checkout_discount else '', 'discount': discount, 'eligible_subtotal': eligible_subtotal, 'delivery_fee': delivery_fee, 'delivery_zone': (delivery_result['zone'] or {}).get('name', 'Other Kenya'), 'delivery_calculation': delivery_result['reason'], 'total': eligible_subtotal + delivery_fee, 'customer_name': form.get('full_name', [''])[0], 'phone': form.get('phone', [''])[0], 'email': form.get('email', [''])[0], 'address': form.get('address', [''])[0], 'location': delivery_location, 'created_at': datetime.now().isoformat(timespec='seconds'), 'items': [{'product_id': item['id'], 'variant_id': item.get('variant_id'), 'quantity': item['quantity'], 'unit_price': cart_item_price(data, item)} for item in session['cart']]}
            data.setdefault('orders', []).append(order)
            notify_event(data, 'new_order', 'New order', f'{order_number} was placed by {order.get("customer_name", "Customer")}', 'order', order_number)
            if order_payment_method == 'M-Pesa':
                try:
                    stk_response = initiate_mpesa(data, mpesa_phone, order['total'], order_number)
                    order['mpesa_checkout_request_id'] = stk_response['CheckoutRequestID']
                    order['mpesa_customer_message'] = stk_response.get('CustomerMessage', 'Check your phone to complete payment')
                except Exception as error:
                    order['payment_status'] = 'Failed'
                    order['mpesa_result_description'] = str(error)
                    payment_error = True
                    message = f'M-Pesa payment request failed: {error}'
                    destination = '/checkout'
            if order_payment_method == 'Order on WhatsApp':
                item_details = []
                for item in session['cart']:
                    product = next((product for product in data['products'] if product.get('id') == item['id']), {})
                    variant = cart_item_variant(data, item)
                    option = f'Option: {variant.get("name", "")} | ' if variant else ''
                    item_details.append(f'{product.get("name", "Product")} | Brand: {product.get("brand", "")} | SKU: {product.get("sku", "")} | {option}Qty: {item["quantity"]} | Unit price: {money(cart_item_price(data, item))}')
                item_summary = '; '.join(item_details)
                whatsapp_text = f'New order {order_number}. Customer name: {form.get("full_name", [""])[0]}. Customer phone: {form.get("phone", [""])[0]}. Email: {form.get("email", [""])[0]}. Delivery address: {form.get("address", [""])[0]}. County/Town: {form.get("location", [""])[0]}. Payment method: {order_payment_method}. Items: {item_summary}. Subtotal: {money(subtotal)}. Delivery fee: {money(order["delivery_fee"])}. Total: {money(order["total"])}.'
                whatsapp_order_url = whatsapp_url(data['shop'].get('whatsapp', ''), whatsapp_text)
            for item in session['cart'] if not payment_error else []:
                product = next((p for p in data['products'] if p['id'] == item['id']), None)
                variant = cart_item_variant(data, item)
                if variant: variant['stock'] = max(0, variant.get('stock', 0) - item['quantity'])
                elif product: product['stock'] = max(0, product['stock'] - item['quantity'])
            if not payment_error:
                session['cart'] = []; message = f'Order {order_number} confirmed'
                checkout_completed = True
            if checkout_coupon and data.get('orders'):
                checkout_coupon['used'] = checkout_coupon.get('used', 0) + 1
        write_db(data)
        if 'destination' not in locals():
            destination = '/cart' if action in ('cart', 'cart_remove', 'cart_update') else '/admin'
        if action == 'cart' and not session.get('customer_id'):
            destination = '/login'
        if action == 'cart' and 'buy_now' in form and session.get('customer_id'):
            destination = '/checkout'
        if action == 'checkout' and checkout_completed:
            if whatsapp_order_url:
                session['whatsapp_order_url'] = whatsapp_order_url
            destination = '/order-confirmation?order=' + urlencode({'order': order_number}).split('=', 1)[-1]
            threading.Thread(target=notify_order_by_email, args=(data, dict(order)), daemon=True).start()
        if message and not whatsapp_order_url: destination += '?' + urlencode({'message': message})
        secure = '; Secure' if self.secure_cookie() else ''
        self.send_response(303); self.send_header('Location', destination); self.send_header('Set-Cookie', f'luxe_session={self.session_token}; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_SECONDS}; Path=/{secure}'); self.send_header('Cache-Control', 'no-store'); self.end_headers()

if __name__ == "__main__":
    # ---------------------------------------------------------
    # ONE-TIME BACKUP MODE
    # ---------------------------------------------------------
    # Run:
    #     python server.py --backup-once
    #
    # This performs one backup and exits.
    if "--backup-once" in sys.argv:
        try:
            run_backup_once()
            print("Backup completed successfully.")
        except Exception as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            raise SystemExit(1)

        raise SystemExit(0)

    # ---------------------------------------------------------
    # ENSURE REQUIRED DIRECTORIES EXIST
    # ---------------------------------------------------------
    uploads_dir = ROOT / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------
    # RENDER PORT CONFIGURATION
    # ---------------------------------------------------------
    #
    # Render provides the PORT environment variable.
    #
    # IMPORTANT:
    # - Bind to 0.0.0.0, NOT 127.0.0.1
    # - Use Render's PORT in production
    # - Use 8000 locally if PORT is not defined
    #
    # Render:
    #     HOST=0.0.0.0
    #     PORT=<Render supplied port>
    #
    # Local:
    #     HOST=0.0.0.0
    #     PORT=8000
    #
    host = os.environ.get("HOST", "0.0.0.0").strip()

    # Never allow localhost binding in production.
    if os.environ.get("APP_ENV", "").lower() == "production":
        host = "0.0.0.0"

    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        print(
            "ERROR: PORT environment variable must be a valid integer.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Validate the port.
    if not (1 <= port <= 65535):
        print(
            f"ERROR: Invalid PORT value: {port}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # ---------------------------------------------------------
    # WINDOWS BACKUP SCHEDULE
    # ---------------------------------------------------------
    #
    # Render runs Linux, so this only applies locally on Windows.
    #
    if os.name == "nt":
        try:
            data = read_db()
            apply_windows_backup_schedule(
                data.get("shop", {})
            )
        except Exception as exc:
            print(
                f"Could not apply Windows backup schedule: {exc}",
                file=sys.stderr,
            )

    # ---------------------------------------------------------
    # STARTUP INFORMATION
    # ---------------------------------------------------------
    print("=" * 60)
    print("LUXE BEAUTY HUB")
    print("=" * 60)
    print(f"Environment : {os.environ.get('APP_ENV', 'development')}")
    print(f"Host        : {host}")
    print(f"Port        : {port}")
    print(f"Uploads     : {uploads_dir}")
    print(f"Server URL  : http://{host}:{port}")
    print("=" * 60)

    # ---------------------------------------------------------
    # SCHEDULED BACKUP THREAD
    # ---------------------------------------------------------
    #
    # Runs in the background without blocking the web server.
    #
    threading.Thread(
        target=scheduled_backup_loop,
        daemon=True,
        name="scheduled-backups",
    ).start()

    # ---------------------------------------------------------
    # START HTTP SERVER
    # ---------------------------------------------------------
    #
    # BoundedThreadingHTTPServer should be your existing server
    # class, and Store should be your existing request handler.
    #
    # IMPORTANT FOR RENDER:
    #     ("0.0.0.0", port)
    #
    # Do NOT use:
    #     ("127.0.0.1", 8000)
    #
    server = BoundedThreadingHTTPServer(
        ("0.0.0.0", port),
        Store,
    )

    print(
        f"Luxe Beauty Hub server listening on "
        f"0.0.0.0:{port}"
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nShutting down Luxe Beauty Hub...")

    except Exception as exc:
        print(
            f"Server stopped because of an error: {exc}",
            file=sys.stderr,
        )
        raise

    finally:
        server.server_close()
        print("Luxe Beauty Hub server closed.")