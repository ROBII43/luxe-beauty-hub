from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from backend.app.database.session import Base


class AdminSetting(Base):
    __tablename__ = "admin_settings"
    setting_key = Column(String(80), primary_key=True)
    encrypted_value = Column(Text, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(DateTime, nullable=False)


class AdminMfaChallenge(Base):
    __tablename__ = "admin_mfa_challenges"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(128), nullable=False)
    token_hash = Column(String(128), unique=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    verified_at = Column(DateTime)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False)