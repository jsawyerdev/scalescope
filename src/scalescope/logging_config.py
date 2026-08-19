"""Logging setup for ScaleScope."""

from __future__ import annotations

import logging

from scalescope.config import settings


def configure_logging() -> None:
    """Configure root logging once, at process start."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
