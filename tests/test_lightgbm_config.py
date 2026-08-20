from __future__ import annotations

import pytest

from scalescope.api.routes import _load_lightgbm_config


def test_load_lightgbm_config_accepts_flat_hyperparameter_json(
    tmp_path,
) -> None:
    config_path = tmp_path / "lightgbm.json"
    config_path.write_text(
        """
        {
          "n_estimators": 180,
          "num_leaves": 31,
          "min_child_samples": 8,
          "learning_rate": 0.08
        }
        """,
        encoding="utf-8",
    )

    assert _load_lightgbm_config(str(config_path)) == {
        "n_estimators": 180,
        "num_leaves": 31,
        "min_child_samples": 8,
        "learning_rate": 0.08,
    }


def test_load_lightgbm_config_rejects_unknown_keys(tmp_path) -> None:
    config_path = tmp_path / "lightgbm.json"
    config_path.write_text('{"n_estimators": 180, "max_depth": 5}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported LightGBM"):
        _load_lightgbm_config(str(config_path))
