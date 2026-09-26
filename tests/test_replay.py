from __future__ import annotations

import numpy as np
import pytest

from scalescope.models.baselines import NaiveModel
from scalescope.replay import _anchors, _pinball_loss, replay_score


def test_anchors_empty_when_insufficient_history() -> None:
    assert _anchors(history_len=20, min_history=30, horizon=5, num_anchors=5) == []


def test_anchors_single_point_at_exact_minimum() -> None:
    assert _anchors(history_len=35, min_history=30, horizon=5, num_anchors=5) == [30]


def test_anchors_are_within_valid_range_and_capped_at_num_anchors() -> None:
    anchors = _anchors(history_len=200, min_history=30, horizon=10, num_anchors=5)
    assert len(anchors) <= 5
    assert all(30 <= a <= 190 for a in anchors)
    assert anchors == sorted(anchors)


def test_replay_score_perfect_on_constant_series_for_naive_model() -> None:
    # naive repeats the last value; a flat series is a trivial perfect forecast.
    history = np.full(100, 500.0)
    scores = replay_score(history, {"naive": NaiveModel()}, min_history=8, horizon=5)
    assert len(scores) == 1
    assert scores[0].model_name == "naive"
    assert scores[0].mean_absolute_error == 0.0
    assert scores[0].mean_absolute_pct_error == 0.0


def test_replay_score_omits_model_with_no_valid_anchors() -> None:
    history = np.full(20, 100.0)
    scores = replay_score(history, {"naive": NaiveModel()}, min_history=30, horizon=5)
    assert scores == []


def test_replay_scores_p90_pinball_and_coverage() -> None:
    history = np.full(100, 500.0)
    scores = replay_score(history, {"naive": NaiveModel()}, min_history=8, horizon=5)
    # Flat series: p90 sits above every actual value, so coverage is total and
    # the loss is (1 - 0.9) times the band above the actuals.
    assert scores[0].p90_coverage == 1.0
    assert scores[0].p90_pinball_loss > 0.0


def test_pinball_loss_penalizes_under_forecasts_more() -> None:
    actual = np.array([100.0])
    assert _pinball_loss(actual, np.array([90.0]), 0.9) == pytest.approx(9.0)
    assert _pinball_loss(actual, np.array([110.0]), 0.9) == pytest.approx(1.0)
