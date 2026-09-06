from datetime import timedelta
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.admin_settings import decrypt, encrypt, hash_value, new_code, now, send_mfa_code
from backend.app.core.dependencies import current_claims, require_admin_mfa
from backend.app.database.session import get_db
from backend.app.models.admin import AdminMfaChallenge, AdminSetting
from backend.app.models.users import User
from backend.app.schemas.admin_settings import AdminSettingsResponse, AdminSettingsUpdate, MfaRequestResponse, MfaVerifyRequest, MfaVerifyResponse, SmtpSettingsUpdate

router = APIRouter(prefix="/api/admin/settings", tags=["admin settings"])
SETTING_KEYS = ("public_base_url", "app_env", "mpesa_environment", "mpesa_shortcode", "mpesa_consumer_key", "mpesa_consumer_secret", "mpesa_passkey", "mpesa_callback_url", "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from")


def admin_id(claims: dict) -> int:
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="Invalid administrator identity") from error


def setting_map(db: Session) -> dict[str, str]:
    return {item.setting_key: decrypt(item.encrypted_value) for item in db.scalars(select(AdminSetting).where(AdminSetting.setting_key.in_(SETTING_KEYS))).all()}


@router.post("/mfa/request", response_model=MfaRequestResponse)
def request_mfa(claims: dict = Depends(current_claims), db: Session = Depends(get_db)) -> MfaRequestResponse:
    if claims.get("role") not in {"SUPERADMIN", "ADMIN"}:
        raise HTTPException(status_code=403, detail="Only administrators can change payment settings")
    user = db.get(User, admin_id(claims))
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Administrator account is unavailable")
    code = new_code()
    challenge = AdminMfaChallenge(user_id=user.id, code_hash=hash_value(code), expires_at=now() + timedelta(minutes=10), attempts=0, created_at=now())
    db.add(challenge)
    db.flush()
    send_mfa_code(user.email, code, db)
    db.commit()
    return MfaRequestResponse(message="A verification code was sent to the administrator email address", expires_in_seconds=600)


@router.post("/mfa/verify", response_model=MfaVerifyResponse)
def verify_mfa(payload: MfaVerifyRequest, claims: dict = Depends(current_claims), db: Session = Depends(get_db)) -> MfaVerifyResponse:
    if claims.get("role") not in {"SUPERADMIN", "ADMIN"}:
        raise HTTPException(status_code=403, detail="Only administrators can verify settings changes")
    user_id = admin_id(claims)
    challenge = db.scalar(select(AdminMfaChallenge).where(AdminMfaChallenge.user_id == user_id, AdminMfaChallenge.verified_at.is_(None)).order_by(AdminMfaChallenge.id.desc()))
    if not challenge or challenge.expires_at <= now() or challenge.attempts >= 5:
        raise HTTPException(status_code=401, detail="Verification code is invalid or expired")
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.code_hash, hash_value(payload.code)):
        db.commit()
        raise HTTPException(status_code=401, detail="Verification code is invalid or expired")
    token = secrets.token_urlsafe(32)
    challenge.token_hash = hash_value(token)
    challenge.verified_at = now()
    challenge.expires_at = now() + timedelta(minutes=10)
    db.commit()
    return MfaVerifyResponse(mfa_token=token, expires_in_seconds=600)


@router.get("", response_model=AdminSettingsResponse)
def get_admin_settings(claims: dict = Depends(current_claims), db: Session = Depends(get_db)) -> AdminSettingsResponse:
    if claims.get("role") not in {"SUPERADMIN", "ADMIN"}:
        raise HTTPException(status_code=403, detail="Administrator permission required")
    values = setting_map(db)
    return AdminSettingsResponse(
        public_base_url=values.get("public_base_url", "http://localhost:8000"),
        app_env=values.get("app_env", "staging"),
        mpesa_environment=values.get("mpesa_environment", "sandbox"),
        mpesa_shortcode=values.get("mpesa_shortcode"),
        mpesa_consumer_key_configured=bool(values.get("mpesa_consumer_key")),
        mpesa_consumer_secret_configured=bool(values.get("mpesa_consumer_secret")),
        mpesa_passkey_configured=bool(values.get("mpesa_passkey")),
        mpesa_callback_url=values.get("mpesa_callback_url"),
        smtp_host=values.get("smtp_host"),
        smtp_port=int(values.get("smtp_port", "587")),
        smtp_user=values.get("smtp_user"),
        smtp_configured=bool(values.get("smtp_host") and values.get("smtp_user") and values.get("smtp_password") and values.get("smtp_from")),
    )


@router.put("", response_model=AdminSettingsResponse)
def update_admin_settings(payload: AdminSettingsUpdate, claims: dict = Depends(require_admin_mfa), db: Session = Depends(get_db)) -> AdminSettingsResponse:
    if payload.app_env == "production" and (not str(payload.public_base_url).startswith("https://") or not str(payload.mpesa_callback_url).startswith("https://") or payload.mpesa_environment != "production"):
        raise HTTPException(status_code=422, detail="Production settings require HTTPS URLs and the production M-Pesa environment")
    values = payload.model_dump(mode="json")
    user_id = admin_id(claims)
    for key, value in values.items():
        setting = db.get(AdminSetting, key)
        if not setting:
            setting = AdminSetting(setting_key=key, updated_by=user_id, updated_at=now())
            db.add(setting)
        setting.encrypted_value = encrypt(str(value))
        setting.updated_by = user_id
        setting.updated_at = now()
    db.commit()
    return get_admin_settings(claims, db)


@router.put("/smtp", response_model=AdminSettingsResponse)
def update_smtp_settings(payload: SmtpSettingsUpdate, claims: dict = Depends(require_admin_mfa), db: Session = Depends(get_db)) -> AdminSettingsResponse:
    values = payload.model_dump()
    user_id = admin_id(claims)
    for key, value in values.items():
        setting = db.get(AdminSetting, key)
        if not setting:
            setting = AdminSetting(setting_key=key, updated_by=user_id, updated_at=now())
            db.add(setting)
        setting.encrypted_value = encrypt(str(value))
        setting.updated_by = user_id
        setting.updated_at = now()
    db.commit()
    return get_admin_settings(claims, db)