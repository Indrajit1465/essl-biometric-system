# app/config.py
"""
Central configuration loaded from environment variables.
Copy .env.example to .env and fill in your values.
"""
from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    # ── Server ──────────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ── Database ─────────────────────────────────────────────────────────────
    # SQLite  → "sqlite:///./attendance.db"
    # Postgres → "postgresql+asyncpg://user:pass@localhost/attendance_db"
    DATABASE_URL: str = "sqlite+aiosqlite:///./attendance.db"

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
