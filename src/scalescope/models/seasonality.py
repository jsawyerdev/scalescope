"""Autocorrelation-based period detection for univariate histories."""

from __future__ import annotations

import math

import numpy as np

_MIN_HISTORY = 100
_FLAT_VARIANCE_EPS = 1e-12
_MIN_PEAK_CORRELATION = 0.25
_ACF_STD_MULTIPLIER = 0.5
_WHITE_NOISE_STD_MULTIPLIER = 4.0
_STRONG_PEAK_FRACTION = 0.9


def detect_period(
    history: np.ndarray, min_period: int = 10, max_period: int | None = None
) -> int:
    """Return the dominant period in ticks, or 1 when none is confident."""
    if min_period < 1:
        raise ValueError("min_period must be >= 1")

    values = np.asarray(history, dtype=float).ravel()
    n = len(values)
    if n < _MIN_HISTORY or not np.all(np.isfinite(values)):
        return 1

    max_candidate = n // 2
    if max_period is not None:
        max_candidate = min(max_candidate, max_period)
    if max_candidate < min_period:
        return 1

    centered = values - float(np.mean(values))
    variance = float(np.mean(centered * centered))
    if not math.isfinite(variance) or variance < _FLAT_VARIANCE_EPS:
        return 1

    acf = _unbiased_autocorrelation(centered, variance, max_candidate)
    threshold = _confidence_threshold(acf, min_period, n)
    peaks = _acf_peaks(acf, min_period, threshold)
    if not peaks:
        return 1

    strongest = max(value for _, value in peaks)
    for period, value in peaks:
        if value >= strongest * _STRONG_PEAK_FRACTION:
            return period
    return 1


def _unbiased_autocorrelation(
    centered: np.ndarray, variance: float, max_lag: int
) -> np.ndarray:
    n = len(centered)
    corr = np.correlate(centered, centered, mode="full")[n - 1 : n + max_lag]
    lags = np.arange(max_lag + 1)
    acf: np.ndarray = corr / ((n - lags) * variance)
    return acf


def _confidence_threshold(acf: np.ndarray, min_period: int, n: int) -> float:
    exclusion_end = min(max(3, min_period // 2), len(acf) - 1)
    acf_std = float(np.std(acf[exclusion_end:]))
    white_noise_floor = _WHITE_NOISE_STD_MULTIPLIER / math.sqrt(n)
    return max(
        _MIN_PEAK_CORRELATION,
        _ACF_STD_MULTIPLIER * acf_std,
        white_noise_floor,
    )


def _acf_peaks(
    acf: np.ndarray, min_period: int, threshold: float
) -> list[tuple[int, float]]:
    max_period = len(acf) - 1
    peaks: list[tuple[int, float]] = []
    for lag in range(min_period, max_period + 1):
        if acf[lag] < threshold:
            continue
        right = acf[lag + 1] if lag < max_period else -math.inf
        if acf[lag] > acf[lag - 1] and acf[lag] >= right:
            peaks.append((lag, float(acf[lag])))
    return peaks
