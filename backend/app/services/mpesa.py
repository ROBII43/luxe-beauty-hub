import base64
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.admin_settings import stored_settings


def normalize_phone(phone_number: str) -> str:
    digits = "".join(character for character in phone_number if character.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    return digits


def initiate_stk_push(phone_number: str, amount: int, order_number: str, db: Session | None = None) -> dict:
    settings = get_settings()
    stored = stored_settings(db) if db else {}
    shortcode = stored.get("mpesa_shortcode", settings.mpesa_shortcode)
    consumer_key = stored.get("mpesa_consumer_key", settings.mpesa_consumer_key)
    consumer_secret = stored.get("mpesa_consumer_secret", settings.mpesa_consumer_secret)
    passkey = stored.get("mpesa_passkey", settings.mpesa_passkey)
    callback_url = stored.get("mpesa_callback_url", settings.mpesa_callback_url)
    environment = stored.get("mpesa_environment", settings.mpesa_environment)
    required = (shortcode, consumer_key, consumer_secret, passkey, callback_url)
    if not all(required):
        raise RuntimeError("M-Pesa is not configured")
    host = "https://api.safaricom.co.ke" if environment == "production" else "https://sandbox.safaricom.co.ke"
    with httpx.Client(timeout=15.0) as client:
        auth = client.get(f"{host}/oauth/v1/generate", params={"grant_type": "client_credentials"}, auth=(consumer_key, consumer_secret))
        auth.raise_for_status()
        access_token = auth.json()["access_token"]
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        response = client.post(
            f"{host}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "BusinessShortCode": int(shortcode),
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount,
                "PartyA": normalize_phone(phone_number),
                "PartyB": int(shortcode),
                "PhoneNumber": normalize_phone(phone_number),
                "CallBackURL": callback_url,
                "AccountReference": order_number,
                "TransactionDesc": f"Luxe order {order_number}",
            },
        )
        response.raise_for_status()
        return response.json()