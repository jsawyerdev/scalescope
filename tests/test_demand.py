import polars as pl

from scalescope.demand import demand_history, demand_signal


def _frame(request_rate: list[float], cpu: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"request_rate": request_rate, "cpu_usage_millicores": cpu})


def test_request_rate_is_preferred_when_reported() -> None:
    df = _frame([0.0, 120.0], [300.0, 400.0])
    assert demand_signal(df) == "request_rate"
    assert demand_history(df, "request_rate").tolist() == [0.0, 120.0]


def test_total_cpu_is_the_fallback_demand_signal() -> None:
    df = _frame([0.0, 0.0], [300.0, 400.0])
    assert demand_signal(df) == "cpu_millicores"
    assert demand_history(df, "cpu_millicores").tolist() == [300.0, 400.0]


def test_no_signal_defaults_to_request_rate() -> None:
    assert demand_signal(_frame([0.0], [0.0])) == "request_rate"
