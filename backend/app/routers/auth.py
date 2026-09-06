from datetime import datetime, timedelta
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import create_access_token, hash_password, verify_password
from backend.app.database.session import get_db
from backend.app.models.users import Role, User, UserSession
from backend.app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tokens(user: User, db: Session, request: Request) -> TokenResponse:
    role = user.role.name if user.role else "CUSTOMER"
    refresh_token = secrets.token_urlsafe(48)
    db.add(UserSession(
        user_id=user.id,
        token_hash=token_hash(refresh_token),
        expires_at=datetime.utcnow() + timedelta(days=30),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        created_at=datetime.utcnow(),
    ))
    return TokenResponse(access_token=create_access_token(str(user.id), role), refresh_token=refresh_token)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    customer_role = db.scalar(select(Role).where(Role.name == "CUSTOMER"))
    if not customer_role:
        customer_role = Role(name="CUSTOMER")
        db.add(customer_role)
        db.flush()
    user = User(name=payload.name, email=payload.email.lower(), phone=payload.phone, password_hash=hash_password(payload.password), role=customer_role)
    db.add(user)
    db.commit()
    db.refresh(user)
    response = issue_tokens(user, db, request)
    db.commit()
    return response


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower(), User.active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    response = issue_tokens(user, db, request)
    db.commit()
    return response


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(payload.refresh_token)))
    if not session or session.revoked_at or session.expires_at <= datetime.utcnow() or not session.user.active:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    session.revoked_at = datetime.utcnow()
    response = issue_tokens(session.user, db, request)
    db.commit()
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> None:
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(payload.refresh_token)))
    if session and not session.revoked_at:
        session.revoked_at = datetime.utcnow()
        db.commit()
