"""Queueing model of one pod: how latency grows as its load nears saturation.

A pod serving `x` requests/s with saturation throughput `mu` behaves like a
queue: the M/M/1 response time grows as 1 / (mu - x), and every percentile
of it (p95 included) scales the same way. Adding the load-independent part
(network, fixed work) gives, for p95 latency in ms:

    p95(x) = B + c / (mu - x)

B, c, and mu are fitted per workload from its own (load per pod, p95
latency) history. Pods are then sized so the busy-case load keeps p95 under
a latency target: the operator's SLO, or by default twice the no-load
latency p95(0). Unlike "CPU scales linearly with work", this follows the
constraint that actually limits the service, whatever it is (CPU, a lock, a
connection pool, a downstream call), because it is read from latency.

The fit is only trusted when the data can identify it; otherwise callers
fall back to CPU-based capacity and say so.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_MIN_SAMPLES = 30
# The load range must span enough of the curve to see it bend.
_MIN_LOAD_SPREAD = 1.5
_MIN_R_SQUARED = 0.6
# mu is searched from just above the highest observed load up to this many
# times it; a best fit at the top edge means saturation was never
# approached, so mu is not identifiable from the data.
_MU_SEARCH_MAX_FACTOR = 10.0
_MU_GRID_POINTS = 400
# Least trimmed squares: rounds of refitting without the worst-fitting share.
_TRIM_FRACTION = 0.15
_TRIM_ROUNDS = 2
DEFAULT_TARGET_FACTOR = 2.0


@dataclass(frozen=True)
class LatencyModel:
    """Fitted p95 latency curve of one pod: p95(x) = base_ms + c / (mu - x)."""

    base_ms: float
    queueing_coefficient: float
    saturation_rps: float
    r_squared: float

    def latency_ms(self, load_rps: float) -> float:
        """Predicted p95 at `load_rps`; defined only below `saturation_rps`."""
        headroom = self.saturation_rps - load_rps
        return self.base_ms + self.queueing_coefficient / headroom

    @property
    def no_load_latency_ms(self) -> float:
        return self.latency_ms(0.0)

    def load_for_latency(self, target_ms: float) -> float | None:
        """Highest load per pod whose predicted p95 stays at `target_ms`."""
        if target_ms <= self.no_load_latency_ms:
            return None
        return self.saturation_rps - self.queueing_coefficient / (
            target_ms - self.base_ms
        )


def fit_latency_model(
    load_per_pod: np.ndarray, latency_p95_ms: np.ndarray
) -> LatencyModel | None:
    """Robust least-squares fit of the queueing curve, or None if not identifiable.

    Deterministic. For each candidate `mu` on a geometric grid the model is
    linear in (B, c), solved exactly by least squares on relative error
    (latency noise scales with latency); the `mu` with the smallest error
    wins. Incidents (a throttled or degraded pod, a slow dependency) put
    points far off the healthy curve, so the fit is refined by least trimmed
    squares: refit on the samples closest to the previous fit, discarding
    the worst `_TRIM_FRACTION`.
    """
    x = np.asarray(load_per_pod, dtype=float)
    y = np.asarray(latency_p95_ms, dtype=float)
    usable = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    x, y = x[usable], y[usable]
    if len(x) < _MIN_SAMPLES:
        return None

    fit = _fit_curve(x, y)
    for _ in range(_TRIM_ROUNDS):
        if fit is None:
            return None
        _, base, coefficient, mu = fit
        # Points at or past the fitted saturation are overload, not the curve.
        fitted = base + coefficient / np.maximum(mu - x, 1e-9)
        residual = np.where(x < mu, np.abs(y - fitted) / fitted, np.inf)
        keep = np.argsort(residual, kind="stable")[: int(len(x) * (1 - _TRIM_FRACTION))]
        x, y = x[keep], y[keep]
        fit = _fit_curve(x, y)

    if fit is None or x.max() < _MIN_LOAD_SPREAD * x.min():
        return None
    r_squared, base, coefficient, mu = fit
    if r_squared < _MIN_R_SQUARED:
        return None
    return LatencyModel(
        base_ms=base,
        queueing_coefficient=coefficient,
        saturation_rps=mu,
        r_squared=r_squared,
    )


def _fit_curve(
    x: np.ndarray, y: np.ndarray
) -> tuple[float, float, float, float] | None:
    """(R², B, c, mu) of the best grid fit, or None if mu is not identifiable."""
    total_variance = float(np.sum((y - y.mean()) ** 2))
    if total_variance == 0:
        return None

    grid = x.max() * np.geomspace(1.001, _MU_SEARCH_MAX_FACTOR, _MU_GRID_POINTS)
    best: tuple[float, float, float, float] | None = None
    for mu in grid:
        feature = 1.0 / (mu - x)
        design = np.column_stack([np.ones_like(feature), feature])
        # Latency noise is multiplicative, so minimise relative error.
        (base, coefficient), *_ = np.linalg.lstsq(
            design / y[:, None], np.ones_like(y), rcond=None
        )
        if base < 0 or coefficient <= 0:
            continue
        relative_error = float(np.sum((1 - (base + coefficient * feature) / y) ** 2))
        if best is None or relative_error < best[0]:
            best = (relative_error, float(base), float(coefficient), float(mu))

    # A best fit at the top of the grid means saturation was never approached.
    if best is None or best[3] >= grid[-2]:
        return None
    _, base, coefficient, mu = best
    sse = float(np.sum((y - base - coefficient / (mu - x)) ** 2))
    return 1 - sse / total_variance, base, coefficient, mu


def latency_target_ms(model: LatencyModel, slo_ms: float | None) -> float:
    """The operator's SLO, or twice the model's no-load latency."""
    return (
        slo_ms
        if slo_ms is not None
        else DEFAULT_TARGET_FACTOR * model.no_load_latency_ms
    )
