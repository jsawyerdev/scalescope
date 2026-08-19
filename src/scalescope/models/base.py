"""Common interface every forecasting model implements.

The capacity engine and diagnosis engine depend only on this interface, never
on a specific model library, so models are interchangeable plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Forecast:
    """Quantile forecast for one workload over a fixed horizon."""

    model_name: str
    p10: np.ndarray
    p50: np.ndarray
    p90: np.ndarray

    def __post_init__(self) -> None:
        if not (len(self.p10) == len(self.p50) == len(self.p90)):
            raise ValueError("p10/p50/p90 must have equal length")


class ForecastModel(Protocol):
    """A model that forecasts a single univariate demand series."""

    name: str

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        """Forecast `horizon` steps ahead from a 1-D history array."""
        ...
