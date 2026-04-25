"""
backend/routers/auth.py

Authentication endpoints.
POST /auth/login   — returns access + refresh tokens
POST /auth/refresh — exchange refresh token for new access token

Passwords hashed with bcrypt via passlib.
JWT issued with jose.
No business logic here — token generation only.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config   import settings
from backend.database import get_db

logger = logging.getLogger("flowsync.routers.auth")

router   = APIRouter()
pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Schemas ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email:    str
    password: str

class LoginResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    role:          str
    depot_id:      Optional[str] = None
    user_id:       str

class RefreshRequest(BaseModel):
    refresh_token: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    db:   AsyncSession = Depends(get_db),
):
    """
    Authenticate user by email + password.
    Returns JWT access token (15 min) and refresh token (7 days).
    """
    row = await db.execute(text("""
        SELECT
            u.id, u.email, u.password_hash,
            u.role, u.depot_id, u.client_id,
            u.is_active
        FROM users u
        WHERE u.email = :email
        LIMIT 1
    """), {"email": body.email.lower().strip()})

    user = row.fetchone()

    if not user or not user.is_active:
        raise HTTPException(
            status_code = 401,
            detail      = "Invalid credentials",
        )

    if not pwd_ctx.verify(body.password, user.password_hash):
        raise HTTPException(
            status_code = 401,
            detail      = "Invalid credentials",
        )

    access_token  = _create_token(user, "access")
    refresh_token = _create_token(user, "refresh")

    logger.info(f"Login: user={user.id} role={user.role}")

    return LoginResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        role          = user.role,
        depot_id      = str(user.depot_id) if user.depot_id else None,
        user_id       = str(user.id),
    )


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    body: RefreshRequest,
    db:   AsyncSession = Depends(get_db),
):
    """
    Exchange a valid refresh token for a new access token.
    """
    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except Exception:
        raise HTTPException(
            status_code = 401,
            detail      = "Invalid or expired refresh token",
        )

    if payload.get("token_type") != "refresh":
        raise HTTPException(
            status_code = 401,
            detail      = "Not a refresh token",
        )

    # Reload user to get current role/status
    row = await db.execute(text("""
        SELECT id, email, role, depot_id, client_id, is_active
        FROM users WHERE id = :uid LIMIT 1
    """), {"uid": payload["user_id"]})
    user = row.fetchone()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    access_token  = _create_token(user, "access")
    refresh_token = _create_token(user, "refresh")

    return LoginResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        role          = user.role,
        depot_id      = str(user.depot_id) if user.depot_id else None,
        user_id       = str(user.id),
    )


# ── Token helpers ──────────────────────────────────────────────────────────────

def _create_token(user, token_type: str) -> str:
    """
    Create JWT for a user.
    access  — expires in jwt_expire_mins (15 min default)
    refresh — expires in jwt_refresh_days (7 days default)
    """
    if token_type == "access":
        expire = datetime.utcnow() + timedelta(
            minutes=settings.jwt_expire_mins
        )
    else:
        expire = datetime.utcnow() + timedelta(
            days=settings.jwt_refresh_days
        )

    payload = {
        "user_id":    str(user.id),
        "depot_id":   str(user.depot_id) if user.depot_id else None,
        "client_id":  str(user.client_id) if user.client_id else None,
        "role":       user.role,
        "token_type": token_type,
        "exp":        expire,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def hash_password(plain: str) -> str:
    """Utility — hash a plain password. Use in seed scripts."""
    return pwd_ctx.hash(plain)