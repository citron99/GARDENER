from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuthSession, User
from app.schemas import TokenRead


password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user_id: int, session_id: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": str(user_id), "sid": session_id, "jti": str(uuid4()), "exp": expires},
        settings.jwt_secret,
        algorithm="HS256",
    )


def _refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token_pair(user: User, db: Session, session: AuthSession | None = None) -> TokenRead:
    now = datetime.now(timezone.utc)
    refresh_token = secrets.token_urlsafe(48)
    if session is None:
        session = AuthSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=_refresh_hash(refresh_token),
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
        )
        db.add(session)
    else:
        session.previous_refresh_token_hash = session.refresh_token_hash
        session.refresh_token_hash = _refresh_hash(refresh_token)
        session.last_used_at = now
    db.commit()
    return TokenRead(
        access_token=create_access_token(user.id, session.id),
        refresh_token=refresh_token,
    )


def rotate_refresh_token(refresh_token: str, db: Session) -> TokenRead:
    now = datetime.now(timezone.utc)
    token_hash = _refresh_hash(refresh_token)
    session = db.query(AuthSession).filter(
        AuthSession.refresh_token_hash == token_hash,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
    ).with_for_update().one_or_none()
    if not session:
        reused = db.query(AuthSession).filter(
            AuthSession.previous_refresh_token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
        ).with_for_update().one_or_none()
        if reused:
            reused.revoked_at = now
            db.commit()
        raise HTTPException(401, "Refresh-токен недействителен")
    user = db.get(User, session.user_id)
    if not user or user.is_blocked or user.email_verified_at is None:
        raise HTTPException(401, "Сессия пользователя недоступна")
    return create_token_pair(user, db, session)


def revoke_refresh_token(refresh_token: str, db: Session) -> bool:
    session = db.query(AuthSession).filter(
        AuthSession.refresh_token_hash == _refresh_hash(refresh_token),
        AuthSession.revoked_at.is_(None),
    ).one_or_none()
    if not session:
        return False
    session.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    error = HTTPException(401, "Требуется действительный токен доступа", headers={"WWW-Authenticate": "Bearer"})
    if not credentials:
        raise error
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
        session_id = str(payload["sid"])
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        raise error
    session = db.get(AuthSession, session_id)
    now = datetime.now(timezone.utc)
    user = db.get(User, user_id)
    if (not user or user.is_blocked or user.email_verified_at is None or
            not session or session.user_id != user.id or
            session.revoked_at is not None or _aware(session.expires_at) <= now):
        raise error
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Требуются права администратора")
    return user
