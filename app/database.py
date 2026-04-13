# app/database.py
"""
Async SQLAlchemy engine + session factory.
Supports SQLite (development) and PostgreSQL (production) via DATABASE_URL.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings
from app.models import Base

# ── Engine ────────────────────────────────────────────────────────────────────
# SQLite: connect_args needed to allow cross-thread usage in async context
_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,            # Set True to log all SQL (verbose, dev only)
    connect_args=_connect_args,
)

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


async def get_db() -> AsyncSession:
    """FastAPI dependency: yields a per-request DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
