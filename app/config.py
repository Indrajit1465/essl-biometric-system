# app/config.py
"""
Central configuration loaded from environment variables.
Copy .env.example to .env and fill in your values.
"""
from __future__ import annotations

import json
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Server ──────────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ── Database ─────────────────────────────────────────────────────────────
    # MySQL (production): "mysql+aiomysql://user:pass@localhost:3306/attendance_db"
    # SQLite (dev only):  "sqlite+aiosqlite:///./attendance.db"
    DATABASE_URL: str = "mysql+aiomysql://att_user:root@localhost:3306/attendance_db"

    # ── Device Trust ─────────────────────────────────────────────────────────
    # Leave empty to accept ANY device serial number (development only).
    # In production, list every device SN that is allowed to push data.
    ALLOWED_DEVICE_SERIALS: List[str] = []

    log_device_requests: bool = False

    # ── ADMS Protocol ────────────────────────────────────────────────────────
    # How often (seconds) the device should push accumulated logs
    ADMS_TRANS_INTERVAL: int = 1
    # Timezone offset for device clock in hours (IST = 5.5)
    DEVICE_TIMEZONE: float = 5.5
    # Maximum acceptable clock drift between device and server (seconds)
    MAX_CLOCK_DRIFT_SECONDS: int = 300

    # ── Pull Mode (pyzk) ─────────────────────────────────────────────────────
    PULL_DEVICE_PORT: int = 4370
    PULL_DEVICE_TIMEOUT: int = 10
    # Clear device memory after pulling? Set True only after DB is confirmed stable.
    PULL_CLEAR_AFTER_FETCH: bool = False

    # ── Security ─────────────────────────────────────────────────────────────
    API_SECRET_KEY: str = "change-me-in-production"

    # ── CORS ─────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["http://localhost:8000"]

    # ── Rate Limiting ────────────────────────────────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = 120

    # ── Pydantic v2 config ───────────────────────────────────────────────────
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # ── Validators ───────────────────────────────────────────────────────────

    @field_validator("ALLOWED_DEVICE_SERIALS", mode="before")
    @classmethod
    def parse_serials(cls, v):
        """Accept JSON list string, comma-separated string, or native list."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    pass
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        """Accept comma-separated string or native list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


settings = Settings()
