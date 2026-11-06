"""
Async database session management.
Supports SQLite (dev) and PostgreSQL (prod) transparently.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from .config import settings


# Engine creation
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args=connect_args,
)

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # V506: best-effort column migration for existing SQLite DBs. SQLAlchemy's
    # create_all doesn't ALTER existing tables, so for already-deployed bots
    # the new peak_price column would be missing. We try to add it and ignore
    # the "duplicate column" error if it already exists.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE trades ADD COLUMN peak_price FLOAT"
                )
            )
    except Exception:
        pass  # column already exists, or non-SQLite backend — non-fatal


async def close_db() -> None:
    await engine.dispose()
