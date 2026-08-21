#!/usr/bin/env python3
"""Tune ScaleScope's LightGBM forecaster against replay-lab MAE."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from ConfigSpace import Configuration, ConfigurationSpace
from ConfigSpace.hyperparameters import (
    UniformFloatHyperparameter,
    UniformIntegerHyperparameter,
)
from smac import HyperparameterOptimizationFacade, Scenario

from scalescope.models.lightgbm_model import (
    LightGbmHyperparameters,
    LightGbmQuantileModel,
)
from scalescope.replay import replay_score
from scalescope.storage import Store

_MAX_REPLAY_OBSERVATIONS = 5000
_REPLAY_MIN_HISTORY = 8
_DEFAULT_WORKLOAD = "payments-api"
_LIGHTGBM_INT_CONFIG_KEYS = frozenset(
    {"n_estimators", "num_leaves", "min_child_samples"}
)
_LIGHTGBM_CONFIG_KEYS = _LIGHTGBM_INT_CONFIG_KEYS | {"learning_rate"}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _configuration_space() -> ConfigurationSpace:
    configspace = ConfigurationSpace(seed=0)
    configspace.add_hyperparameters(
        [
            UniformIntegerHyperparameter(
                "n_estimators", lower=50, upper=300, default_value=100
            ),
            UniformIntegerHyperparameter(
                "num_leaves", lower=7, upper=63, default_value=15
            ),
            UniformIntegerHyperparameter(
                "min_child_samples", lower=2, upper=20, default_value=5
            ),
            UniformFloatHyperparameter(
                "learning_rate",
                lower=0.01,
                upper=0.3,
                default_value=0.1,
                log=True,
            ),
        ]
    )
    return configspace


def _params_from_mapping(
    values: Mapping[str, object],
) -> LightGbmHyperparameters:
    return {
        "n_estimators": int(values["n_estimators"]),
        "num_leaves": int(values["num_leaves"]),
        "min_child_samples": int(values["min_child_samples"]),
        "learning_rate": float(values["learning_rate"]),
    }


def _params_from_configuration(config: Configuration) -> LightGbmHyperparameters:
    return _params_from_mapping(config.get_dictionary())


def _positive_int_config(raw_config: Mapping[str, object], key: str) -> int:
    value = raw_config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key!r} must be a positive integer")
    return value


def _load_hyperparameter_config(path: Path) -> LightGbmHyperparameters:
    with path.open(encoding="utf-8") as f:
        raw_config = json.load(f)

    if not isinstance(raw_config, dict):
        raise TypeError("baseline config must contain a JSON object")

    unknown_keys = sorted(set(raw_config) - _LIGHTGBM_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"baseline config contains unsupported LightGBM key(s): {unknown_keys}"
        )

    config: LightGbmHyperparameters = {}
    if "n_estimators" in raw_config:
        config["n_estimators"] = _positive_int_config(raw_config, "n_estimators")
    if "num_leaves" in raw_config:
        config["num_leaves"] = _positive_int_config(raw_config, "num_leaves")
    if "min_child_samples" in raw_config:
        config["min_child_samples"] = _positive_int_config(
            raw_config, "min_child_samples"
        )
    if "learning_rate" in raw_config:
        value = raw_config["learning_rate"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError("'learning_rate' must be a positive finite number")
        config["learning_rate"] = float(value)
    return config


def _load_history(db_path: Path, workload: str) -> np.ndarray:
    store = Store(str(db_path))
    try:
        df = store.recent_observations(workload, _MAX_REPLAY_OBSERVATIONS)
        if df.is_empty():
            known = ", ".join(store.workloads()) or "<none>"
            raise ValueError(
                f"no observations found for workload {workload!r}; "
                f"known workloads: {known}"
            )
        return df["request_rate"].to_numpy()
    finally:
        store.close()


def _mae(
    history: np.ndarray,
    horizon: int,
    params: LightGbmHyperparameters | None = None,
) -> float:
    model = (
        LightGbmQuantileModel(**params)
        if params is not None
        else LightGbmQuantileModel()
    )
    scores = replay_score(
        history,
        {LightGbmQuantileModel.name: model},
        min_history=_REPLAY_MIN_HISTORY,
        horizon=horizon,
    )
    if not scores:
        minimum = _REPLAY_MIN_HISTORY + horizon
        raise ValueError(
            "not enough history for replay scoring: "
            f"need at least {minimum} observations, got {len(history)}"
        )
    return scores[0].mean_absolute_error


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Tune LightGBM quantile hyperparameters with SMAC, using "
            "ScaleScope replay MAE as the minimized objective."
        )
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--workload", default=_DEFAULT_WORKLOAD)
    parser.add_argument("--trials", type=_positive_int, default=40)
    parser.add_argument("--horizon", type=_positive_int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--baseline-config",
        type=Path,
        help="Existing LightGBM config to replay-score against the current data.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    history = _load_history(args.db_path, args.workload)

    def objective(config: Configuration, seed: int = 0) -> float:
        del seed
        return _mae(history, args.horizon, _params_from_configuration(config))

    with tempfile.TemporaryDirectory(prefix="scalescope-smac-") as output_directory:
        scenario = Scenario(
            _configuration_space(),
            deterministic=True,
            n_trials=args.trials,
            n_workers=1,
            output_directory=Path(output_directory),
        )
        smac = HyperparameterOptimizationFacade(scenario, objective)
        incumbent = smac.optimize()

    best_params = _params_from_configuration(incumbent)
    default_mae = _mae(history, args.horizon)
    baseline_mae = (
        _mae(
            history,
            args.horizon,
            _load_hyperparameter_config(args.baseline_config),
        )
        if args.baseline_config is not None
        else None
    )
    best_mae = _mae(history, args.horizon, best_params)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(best_params, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"workload={args.workload}")
    print(f"observations={len(history)}")
    print(f"horizon={args.horizon}")
    print(f"trials={args.trials}")
    print(f"default_mae={default_mae:.2f}")
    if baseline_mae is not None:
        print(f"baseline_mae={baseline_mae:.2f}")
    print(f"best_mae={best_mae:.2f}")
    print(f"best_params={json.dumps(best_params, sort_keys=True)}")
    print(f"wrote={args.out}")


if __name__ == "__main__":
    main()
