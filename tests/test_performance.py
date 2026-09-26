import numpy as np
import pytest

from scalescope.performance import LatencyModel, fit_latency_model, latency_target_ms


def _curve(
    lo: float, hi: float, noise: float, n: int = 300
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(11)
    x = rng.uniform(lo, hi, n)
    y = (20.0 + 3000.0 / (220.0 - x)) * (1 + rng.normal(0, noise, n))
    return x, y


def test_fit_recovers_the_queueing_parameters() -> None:
    model = fit_latency_model(*_curve(40, 200, 0.05))

    assert model is not None
    assert model.saturation_rps == pytest.approx(220.0, rel=0.03)
    assert model.base_ms == pytest.approx(20.0, rel=0.15)
    assert model.r_squared > 0.9


def test_fit_ignores_an_incident_off_the_healthy_curve() -> None:
    x, y = _curve(40, 200, 0.05)
    # 10% of samples from a degraded episode: low load, doubled latency.
    rng = np.random.default_rng(5)
    incident_x = rng.uniform(40, 80, 30)
    incident_y = 2 * (20.0 + 3000.0 / (110.0 - incident_x))

    model = fit_latency_model(
        np.concatenate([x, incident_x]), np.concatenate([y, incident_y])
    )

    assert model is not None
    assert model.saturation_rps == pytest.approx(220.0, rel=0.05)


def test_fit_is_deterministic() -> None:
    x, y = _curve(40, 200, 0.05)
    assert fit_latency_model(x, y) == fit_latency_model(x, y)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        _curve(20, 60, 0.05),  # load never approaches saturation
        (np.full(100, 50.0), np.full(100, 30.0)),  # no spread at all
        (np.array([50.0, 60.0]), np.array([30.0, 40.0])),  # too few samples
    ],
)
def test_fit_declines_when_the_curve_is_not_identifiable(
    x: np.ndarray, y: np.ndarray
) -> None:
    assert fit_latency_model(x, y) is None


def test_load_for_latency_inverts_the_curve() -> None:
    model = LatencyModel(
        base_ms=20.0, queueing_coefficient=3000.0, saturation_rps=220.0, r_squared=1.0
    )
    load = model.load_for_latency(100.0)
    assert load is not None
    assert model.latency_ms(load) == pytest.approx(100.0)
    assert model.load_for_latency(model.no_load_latency_ms) is None


def test_default_target_is_twice_the_no_load_latency() -> None:
    model = LatencyModel(
        base_ms=20.0, queueing_coefficient=3000.0, saturation_rps=220.0, r_squared=1.0
    )
    assert latency_target_ms(model, None) == pytest.approx(2 * (20.0 + 3000.0 / 220.0))
    assert latency_target_ms(model, 150.0) == 150.0
