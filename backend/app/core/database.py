"""
Async database session management.
Supports SQLite (dev) and PostgreSQL (prod) transparently.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from .config import settings
from .logger import logger


def _ensure_db_dir() -> None:
    """V510 FIX: create the parent directory of the SQLite file before
    opening the engine. SQLite cannot open 'sqlite+aiosqlite:///./data/foo.db'
    if ./data doesn't exist — it raises 'unable to open database file' which
    is exactly the error we saw in production.
    For Postgres / MySQL URLs (no .db file) this is a no-op.
    """
    url = settings.database_url
    if not url.startswith("sqlite"):
        return
    # sqlite+aiosqlite:///./data/memecoin_bot.db  →  ./data/memecoin_bot.db
    # sqlite+aiosqlite:////app/data/memecoin_bot.db  →  /app/data/memecoin_bot.db
    if ":///" in url:
        path = url.split(":///", 1)[1]
    elif "://" in url:
        path = url.split("://", 1)[1].lstrip("/")
    else:
        return
    if not path:
        return
    db_dir = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"DB directory ready: {db_dir}")
    except Exception as exc:
        # In some read-only filesystems (rare) we can't mkdir — log and let
        # the engine fail with a clearer message later.
        logger.warning(f"Could not create DB directory {db_dir}: {exc}")


# V510: ensure the directory exists BEFORE creating the engine.
_ensure_db_dir()

# Engine creation
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

# V510: try to create the engine; if it fails (e.g. read-only filesystem,
# invalid DATABASE_URL), fall back to an in-memory SQLite so the app at least
# starts and serves /health. Without this the entire container would crash
# on startup when the Volume path is wrong.
_real_db_url = settings.database_url
try:
    engine = create_async_engine(
        _real_db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        connect_args=connect_args,
    )
except Exception as exc:
    logger.error(
        f"❌ Failed to create DB engine for {_real_db_url}: {exc}. "
        f"Falling back to in-memory SQLite (data will NOT persist)."
    )
    _real_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(_real_db_url, echo=False, connect_args={"check_same_thread": False})

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Context manager that yields a DB session and rolls back on error."""
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncSession:
    """FastAPI dependency."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables. Used for dev / first-run. Use Alembic for migrations in prod."""
    from .models import Base  # noqa
    # V510: also ensure the directory exists at startup time (in case the
    # Volume mount changed after import).
    _ensure_db_dir()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()
