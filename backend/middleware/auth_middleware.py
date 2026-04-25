"""
backend/middleware/auth_middleware.py

JWT authentication middleware.
Runs on every request before it reaches any router.

Decodes JWT, injects into request.state:
    request.state.user_id   — UUID string
    request.state.depot_id  — UUID string (sets RLS context)
    request.state.role      — ADMIN | MANAGER | STAFF | FIELD_REP
    request.state.client_id — UUID string

Public routes (no token required):
    GET  /health
    GET  /
    POST /auth/login
    POST /auth/refresh

All other routes require a valid Bearer token.
Role enforcement is done in individual routers via
the require_role() dependency — not here.
"""

import logging
from typing import Optional

from fastapi import Request, Response
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend.config import settings

logger = logging.getLogger("flowsync.middleware.auth")

# Routes that do not require authentication
PUBLIC_PATHS = {
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/login",
    "/auth/refresh",
}


class AuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always allow public paths
        if path in PUBLIC_PATHS:
            _clear_state(request)
            return await call_next(request)

        # Extract token from Authorization header
        token = _extract_token(request)

        if not token:
            return JSONResponse(
                status_code = 401,
                content     = {
                    "error": "missing_token",
                    "detail": "Authorization header required. "
                              "Format: Bearer <token>",
                },
            )

        # Decode and validate
        payload = _decode_token(token)

        if payload is None:
            return JSONResponse(
                status_code = 401,
                content     = {
                    "error":  "invalid_token",
                    "detail": "Token is invalid or expired.",
                },
            )

        # Inject into request state
        # get_db() reads depot_id from here to set RLS context
        request.state.user_id   = payload.get("user_id")
        request.state.depot_id  = payload.get("depot_id")
        request.state.role      = payload.get("role")
        request.state.client_id = payload.get("client_id")

        return await call_next(request)


def _extract_token(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def _decode_token(token: str) -> Optional[dict]:
    """
    Decode and validate JWT.
    Returns payload dict or None on any error.
    """
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        logger.debug(f"JWT decode failed: {e}")
        return None


def _clear_state(request: Request) -> None:
    """Set all state fields to None for public routes."""
    request.state.user_id   = None
    request.state.depot_id  = None
    request.state.role       = None
    request.state.client_id  = None


# ── Router-level role dependency ───────────────────────────────────────────────
# Import and use in individual routers:
#   from backend.middleware.auth_middleware import require_role
#   @router.post("/", dependencies=[Depends(require_role("MANAGER"))])

from fastapi import Depends, HTTPException


def require_role(*allowed_roles: str):
    """
    FastAPI dependency that enforces role-based access.

    Usage:
        @router.post("/approve", dependencies=[Depends(require_role("MANAGER", "ADMIN"))])
        async def approve_action(...):
            ...
    """
    def _check(request: Request):
        role = getattr(request.state, "role", None)
        if role not in allowed_roles:
            raise HTTPException(
                status_code = 403,
                detail      = (
                    f"Role '{role}' not permitted. "
                    f"Required: {list(allowed_roles)}"
                ),
            )
    return Depends(_check)


def get_current_user(request: Request) -> dict:
    """
    Dependency that returns current user info from request state.
    Raises 401 if not authenticated.

    Usage:
        @router.get("/")
        async def handler(user=Depends(get_current_user)):
            print(user["user_id"])
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "user_id":   request.state.user_id,
        "depot_id":  request.state.depot_id,
        "role":      request.state.role,
        "client_id": request.state.client_id,
    }