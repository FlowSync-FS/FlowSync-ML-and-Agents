"""
backend/middleware/audit_middleware.py

Audit trail middleware.
Intercepts POST, PUT, PATCH, DELETE requests.
Writes one INSERT to audit_trail for every mutating request.

The audit_trail table has a DB-level trigger preventing
UPDATE and DELETE — once written, it cannot be modified.
This satisfies IT Act 2000 requirements for electronic records.

What is captured:
    event_type     — HTTP method + path (e.g. POST /invoices/scan)
    entity_table   — derived from path (e.g. invoices)
    entity_id      — from path parameter or response body
    performed_by   — user_id from JWT (request.state)
    ip_address     — client IP from X-Forwarded-For or client host
    new_value      — response body (truncated at 10KB)

What is NOT captured:
    GET requests — read-only, no audit needed
    /health, /docs — system routes
    old_value — captured at service layer when needed (not here)
"""

import json
import logging
import time
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backend.database import get_admin_db

logger = logging.getLogger("flowsync.middleware.audit")

# Methods that trigger audit logging
AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths excluded from audit (system routes)
AUDIT_EXCLUDE_PATHS = {
    "/health",
    "/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/login",
    "/auth/refresh",
}

# Maximum size of captured request/response body in bytes
MAX_BODY_SIZE = 10_240   # 10 KB


class AuditMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        path   = request.url.path
        method = request.method

        # Skip non-mutating and excluded routes
        if (method not in AUDIT_METHODS or
                path in AUDIT_EXCLUDE_PATHS or
                path.startswith("/docs")):
            return await call_next(request)

        # Capture request body (stream must be read and re-injected)
        request_body = await _read_body(request)

        # Process request
        start_time = time.time()
        response   = await call_next(request)
        duration   = time.time() - start_time

        # Capture response body
        response_body = await _read_response_body(response)

        # Write audit entry asynchronously
        # Do not block the response if audit write fails
        try:
            await _write_audit(
                request       = request,
                method        = method,
                path          = path,
                request_body  = request_body,
                response_body = response_body,
                status_code   = response.status_code,
                duration_ms   = round(duration * 1000, 1),
            )
        except Exception as e:
            logger.error(f"Audit write failed: {e}")

        return response


async def _write_audit(
    request:       Request,
    method:        str,
    path:          str,
    request_body:  bytes,
    response_body: bytes,
    status_code:   int,
    duration_ms:   float,
) -> None:
    """
    Write one row to audit_trail.
    Uses admin DB connection so RLS does not restrict the write.
    """
    user_id   = getattr(request.state, "user_id",  None)
    depot_id  = getattr(request.state, "depot_id", None)
    ip        = _get_client_ip(request)
    entity    = _derive_entity(path)

    # Truncate bodies to MAX_BODY_SIZE
    req_body_str = _safe_decode(request_body[:MAX_BODY_SIZE])
    res_body_str = _safe_decode(response_body[:MAX_BODY_SIZE])

    async with get_admin_db() as db:
        await db.execute("""
            INSERT INTO audit_trail
                (event_type, entity_table, performed_by,
                 old_value, new_value, ip_address, created_at)
            VALUES
                (:event, :entity, :user,
                 :old, :new, :ip, NOW())
        """, {
            "event":  f"{method} {path}",
            "entity": entity,
            "user":   user_id,
            "old":    None,
            "new":    json.dumps({
                "request_body":  _try_parse_json(req_body_str),
                "response_body": _try_parse_json(res_body_str),
                "status_code":   status_code,
                "duration_ms":   duration_ms,
                "depot_id":      depot_id,
            }),
            "ip":     ip,
        })


async def _read_body(request: Request) -> bytes:
    """
    Read and buffer request body so it can be used
    both by middleware and the actual route handler.
    """
    try:
        body = await request.body()
        # Re-inject body so route handler can read it
        async def receive():
            return {"type": "http.request", "body": body}
        request._receive = receive
        return body
    except Exception:
        return b""


async def _read_response_body(response: Response) -> bytes:
    """
    Read response body from StreamingResponse.
    Reconstruct it so it can still be sent to client.
    """
    try:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes)
                          else chunk.encode())
        body = b"".join(chunks)

        # Reconstruct response body iterator
        async def body_iterator():
            yield body

        response.body_iterator = body_iterator()
        return body
    except Exception:
        return b""


def _get_client_ip(request: Request) -> str:
    """Extract real client IP from X-Forwarded-For or direct connection."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _derive_entity(path: str) -> str:
    """
    Derive table name from path.
    /invoices/123/confirm → invoices
    /temperature/photo-log → temperature_logs
    """
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "unknown"
    entity_map = {
        "invoices":    "invoices",
        "inventory":   "stock_movements",
        "payments":    "payments",
        "returns":     "returns",
        "temperature": "temperature_logs",
        "recalls":     "recalls",
        "agents":      "agent_actions",
        "compliance":  "audit_trail",
        "retailers":   "retailers",
    }
    return entity_map.get(segments[0], segments[0])


def _safe_decode(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _try_parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except Exception:
        return text