from datetime import datetime

from sqlalchemy.orm import Session

from backend.app.models.audit import AuditLog


def record_audit(db: Session, claims: dict, action: str, entity: str, description: str, entity_id: str | None = None, ip_address: str | None = None) -> None:
    db.add(AuditLog(user_id=int(claims["sub"]) if claims.get("sub") else None, action=action, entity=entity, entity_id=entity_id, description=description, ip_address=ip_address, created_at=datetime.utcnow()))