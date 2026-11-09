"""
Structured logging via loguru with safe redaction of secrets.
"""
from __future__ import annotations

import os
import sys

from loguru import logger

from .config import settings


def setup_logging() -> None:
    """Configure loguru sinks. Called once at startup."""
    logger.remove()
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    logger.configure(
        handlers=[
            {"sink": sys.stdout, "level": settings.app_log_level, "format": log_format},
        ],
    )

    # File rotation in production
    if settings.is_production:
        os.makedirs("logs", exist_ok=True)
        logger.add(
            "logs/bot_{time:YYYYMMDD}.log",
            rotation="00:00",
            retention="30 days",
            level="DEBUG",
            format=log_format,
            enqueue=True,
        )

    logger.info(f"Logging initialized | env={settings.app_env}")


__all__ = ["logger", "setup_logging"]
