from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.security import hash_password
from backend.app.models.users import Role, User


def ensure_superadmin(db: Session) -> None:
    settings = get_settings()
    email = settings.admin_email.lower()
    if db.scalar(select(User).where(User.email == email)):
        return
    if not settings.admin_password:
        raise RuntimeError("LUXE_ADMIN_PASSWORD must be configured to create the initial superadmin")

    role = db.scalar(select(Role).where(Role.name == "SUPERADMIN"))
    if not role:
        role = Role(name="SUPERADMIN")
        db.add(role)
        db.flush()
    db.add(User(
        name="Store Administrator",
        email=email,
        password_hash=hash_password(settings.admin_password),
        role=role,
        active=True,
        created_at=datetime.utcnow(),
    ))
    db.commit()