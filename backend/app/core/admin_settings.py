from datetime import datetime, timezone
import hashlib
import secrets
import smtplib
from email.message import EmailMessage

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from backend.app.core.config import get_settings
from backend.app.models.admin import AdminSetting
from sqlalchemy import select
from sqlalchemy.orm import Session


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cipher() -> Fernet:
    key = get_settings().settings_encryption_key
    if not key:
        raise HTTPException(status_code=503, detail="Admin settings encryption is not configured")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Admin settings encryption key is invalid") from error


def encrypt(value: str) -> str:
    return cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str:
    try:
        return cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as error:
        raise HTTPException(status_code=503, detail="Stored admin setting cannot be decrypted") from error


def send_mfa_code(recipient: str, code: str, db: Session | None = None) -> None:
    settings = get_settings()
    stored = stored_settings(db) if db else {}
    smtp_host = stored.get("smtp_host", settings.smtp_host)
    smtp_port = int(stored.get("smtp_port", settings.smtp_port or 587))
    smtp_user = stored.get("smtp_user", settings.smtp_user)
    smtp_password = stored.get("smtp_password", settings.smtp_password)
    smtp_from = stored.get("smtp_from", settings.smtp_from)
    if not all((smtp_host, smtp_user, smtp_password, smtp_from)):
        raise HTTPException(status_code=503, detail="Admin MFA email is not configured")
    message = EmailMessage()
    message["Subject"] = "Luxe Beauty Hub admin verification code"
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content(f"Your Luxe Beauty Hub settings verification code is {code}. It expires in 10 minutes.")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def stored_settings(db: Session) -> dict[str, str]:
    return {item.setting_key: decrypt(item.encrypted_value) for item in db.scalars(select(AdminSetting)).all()}