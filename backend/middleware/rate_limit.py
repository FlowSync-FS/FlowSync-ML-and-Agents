"""
backend/middleware/rate_limit.py

Redis-backed rate limiter.
Sliding window algorithm — counts requests per minute.

Two limits (from compliance_config / settings):
    Public routes:        100 req/min per IP
    Authenticated routes: 500 req/min per authenticated user

Returns 429 Too Many Requests with Retry-After header.
Limit keys expire automatically after 60 seconds.
"""

import logging
import time

import redis.asyncio as aioredis
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend.config import settings

logger = logging.getLogger("flowsync.middleware.rate_limit")

# Paths that bypass rate limiting entirely
EXEMPT_PATHS = {"/health", "/", "/docs", "/redoc", "/openapi.json"}


class RateLimitMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        self._redis: aioredis.Redis = None

    async def _get_redis(self) -> aioredis.Redis:
        """Lazy Redis connection — created on first request."""
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding        = "utf-8",
                decode_responses = True,
            )
        return self._redis

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path in EXEMPT_PATHS:
            return await call_next(request)

        # Determine limit and identifier
        user_id = getattr(request.state, "user_id", None)

        if user_id:
            # Authenticated — higher limit, keyed by user_id
            limit     = settings.rate_limit_authed
            redis_key = f"rate:user:{user_id}"
        else:
            # Public — lower limit, keyed by IP
            limit     = settings.rate_limit_public
            ip        = _get_ip(request)
            redis_key = f"rate:ip:{ip}"

        # Check and increment counter
        try:
            redis  = await self._get_redis()
            count  = await _increment(redis, redis_key, window_secs=60)
        except Exception as e:
            # Redis down — fail open (allow request, log warning)
            logger.warning(f"Rate limiter Redis error: {e} — allowing request")
            return await call_next(request)

        if count > limit:
            logger.warning(
                f"Rate limit exceeded: key={redis_key} "
                f"count={count} limit={limit}"
            )
            return JSONResponse(
                status_code = 429,
                content     = {
                    "error":  "rate_limit_exceeded",
                    "detail": f"Too many requests. Limit: {limit}/min.",
                },
                headers = {
                    "Retry-After": "60",
                    "X-RateLimit-Limit":     str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Add rate limit headers to successful responses
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"]     = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(
            max(limit - count, 0)
        )
        return response


async def _increment(
    redis: aioredis.Redis,
    key: str,
    window_secs: int,
) -> int:
    """
    Sliding window counter using Redis INCR + EXPIRE.
    Returns current count for this window.
    """
    pipe  = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_secs)
    results = await pipe.execute()
    return int(results[0])


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"