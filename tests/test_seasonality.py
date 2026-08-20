from __future__ import annotations

import numpy as np
import pytest

from scalescope.models.seasonality import detect_period


def _sine(period: int, cycles: int) -> np.ndarray:
    t = np.arange(period * cycles)
    return 100.0 + 20.0 * np.sin(2 * np.pi * t / period)


@pytest.mark.parametrize(("period", "cycles"), [(300, 2), (50, 6), (20, 8)])
def test_detect_period_finds_clean_sine_period(period: int, cycles: int) -> None:
    assert detect_period(_sine(period, cycles)) == period


def test_detect_period_returns_one_for_white_noise() -> None:
    rng = np.random.default_rng(20260820)
    assert detect_period(rng.normal(size=600)) == 1


def test_detect_period_returns_one_for_short_or_flat_history() -> None:
    assert detect_period(_sine(20, 4)) == 1
    assert detect_period(np.full(600, 42.0)) == 1
