from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from backend.app.database.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(120), nullable=False, index=True)
    entity = Column(String(80), nullable=False)
    entity_id = Column(String(80))
    description = Column(Text, nullable=False)
    ip_address = Column(String(45))
    created_at = Column(DateTime, nullable=False, index=True)