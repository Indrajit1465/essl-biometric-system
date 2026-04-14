# app/database.py
"""
Async SQLAlchemy engine + session factory.
Supports MySQL (production) and SQLite (development) via DATABASE_URL.
"""
from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
_engine_kwargs: dict = dict(
    echo=False,           # Set True to log all SQL (verbose, dev only)
)

if settings.is_sqlite:
    # SQLite: connect_args needed to allow cross-thread usage in async context
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # MySQL / PostgreSQL: configure connection pooling (M10)
    _engine_kwargs.update(
        pool_size=20,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,      # Recycle connections every 30 min
        pool_pre_ping=True,     # Verify connection is alive before using
    )

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

# M5: Enable SQLite foreign key enforcement (off by default in SQLite)
if settings.is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Keep objects usable after commit
)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def create_tables() -> None:
    """Create all tables if they don't exist (dev / first-run helper)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified/created successfully")


async def get_db() -> AsyncSession:
    """
    FastAPI dependency: yields a per-request DB session.

    H3 FIX: No auto-commit. Handlers must explicitly commit.
    This prevents partial-write masking from C3's rollback issue.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_db_health() -> bool:
    """L12: Verify database connectivity for health endpoint."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
        return False
