"""Deterministic minute-level demand with daily and weekly structure, for tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

# Naive UTC, like the minute timestamps the store returns.
MONDAY = datetime(2026, 1, 5, tzinfo=UTC).replace(tzinfo=None)
MINUTES_PER_DAY = 1440


def weekly_traffic(days: int, seed: int = 0, base: float = 1000.0) -> np.ndarray:
    """Demand per minute from Monday 00:00 UTC: busy days, quiet nights and weekends."""
    rng = np.random.default_rng(seed)
    t = np.arange(days * MINUTES_PER_DAY)
    hour = (t % MINUTES_PER_DAY) / 60
    weekend = (t // MINUTES_PER_DAY) % 7 >= 5
    daily = (
        0.2
        + np.exp(-0.5 * ((hour - 10) / 1.8) ** 2)
        + 0.8 * np.exp(-0.5 * ((hour - 15) / 2.2) ** 2)
    )
    shape = np.where(weekend, 0.3 + 0.5 * daily, daily)
    noise = np.exp(rng.normal(0, 0.05, len(t)))
    values: np.ndarray = base * shape * noise
    return values
