"""
backend/config.py

Single source of all environment variables.
Every other backend file imports settings from here.
Never call os.getenv() anywhere else in the codebase.

Reads from .env file in development (via python-dotenv).
In production, environment variables are injected by Docker.
"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All environment variables with types and defaults.
    Pydantic validates types on startup — missing required
    vars raise a clear error before any request is served.
    """

    model_config = SettingsConfigDict(
        env_file        = ".env",
        env_file_encoding = "utf-8",
        case_sensitive  = False,
        extra           = "ignore",
    )

    # ── Database ───────────────────────────────────────────────────────────────
    # Tenant connection — subject to RLS (Row-Level Security)
    database_url: str = (
        "postgresql+asyncpg://flowsync:flowsync@localhost:5432/flowsync"
    )
    # Admin connection — bypasses RLS for ML pipeline + migrations
    # NEVER expose this through any HTTP route
    admin_db_url: str = (
        "postgresql+asyncpg://flowsync_admin:flowsync_admin@localhost:5432/flowsync"
    )

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    s3_bucket:      str = "flowsync-dev"
    aws_access_key: str = ""
    aws_secret_key: str = ""
    aws_region:     str = "ap-south-1"   # Mumbai — lowest latency for India

    # ── Auth ───────────────────────────────────────────────────────────────────
    # Generate with: openssl rand -hex 32
    jwt_secret:      str = "change-me-in-production"
    jwt_algorithm:   str = "HS256"
    jwt_expire_mins: int = 15        # access token
    jwt_refresh_days: int = 7        # refresh token

    # ── Notifications ──────────────────────────────────────────────────────────
    whatsapp_token:   str = ""        # Twilio WhatsApp Business API
    whatsapp_from:    str = ""        # pre-registered sender number
    sendgrid_key:     str = ""
    sendgrid_from:    str = "noreply@flowsynchealth.com"
    firebase_key:     str = ""        # Firebase Admin SDK JSON (base64)

    # ── MQTT (IoT) ─────────────────────────────────────────────────────────────
    mqtt_broker_host: str = "localhost"
    mqtt_broker_port: int = 1883

    # ── App ────────────────────────────────────────────────────────────────────
    env:             str = "development"   # development | staging | production
    log_level:       str = "INFO"
    ml_alert_email:  str = "team@flowsynchealth.com"

    # ── Rate limiting ──────────────────────────────────────────────────────────
    rate_limit_public:  int = 100   # requests per minute per IP (public routes)
    rate_limit_authed:  int = 500   # requests per minute per user (auth routes)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_development(self) -> bool:
        return self.env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings instance.
    Call get_settings() everywhere — never instantiate Settings() directly.
    lru_cache means .env is read once at startup, not per request.
    """
    return Settings()


# Convenience alias — most files do: from backend.config import settings
settings = get_settings()