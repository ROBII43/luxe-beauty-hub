import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.core.security import decode_access_token
from backend.app.database.session import get_db
from backend.app.models.admin import AdminMfaChallenge
from sqlalchemy import select
from sqlalchemy.orm import Session

bearer = HTTPBearer(auto_error=False)


def current_claims(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        return decode_access_token(credentials.credentials)
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from error


def require_admin(claims: dict = Depends(current_claims)) -> dict:
    if claims.get("role") not in {"SUPERADMIN", "ADMIN", "MANAGER"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator permission required")
    return claims


ROLE_PERMISSIONS = {
    "SUPERADMIN": {"*"},
    "ADMIN": {"products:write", "orders:read", "orders:write", "inventory:write", "customers:read", "reports:read"},
    "STAFF": {"orders:read", "orders:write", "inventory:write"},
    "CUSTOMER": {"shop:read", "orders:own", "account:own"},
}


def require_permission(permission: str):
    def dependency(claims: dict = Depends(current_claims)) -> dict:
        role = claims.get("role")
        if role not in ROLE_PERMISSIONS or ("*" not in ROLE_PERMISSIONS[role] and permission not in ROLE_PERMISSIONS[role]):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
        return claims
    return dependency


def require_admin_mfa(
    claims: dict = Depends(current_claims),
    mfa_token: str | None = Header(default=None, alias="X-Admin-MFA-Token"),
    db: Session = Depends(get_db),
) -> dict:
    if claims.get("role") not in {"SUPERADMIN", "ADMIN"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators can change payment settings")
    if not mfa_token:
        raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="Email MFA verification required")
    digest = hashlib.sha256(mfa_token.encode("utf-8")).hexdigest()
    challenge = db.scalar(select(AdminMfaChallenge).where(AdminMfaChallenge.token_hash == digest))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not challenge or challenge.user_id != int(claims.get("sub", 0)) or not challenge.verified_at or challenge.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired admin MFA token")
    return claims
