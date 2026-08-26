from __future__ import annotations

from pathlib import Path

import pytest

from scalescope.api.routes import _load_lightgbm_config


@pytest.mark.parametrize("path", [None, ""])
def test_load_lightgbm_config_treats_unset_or_empty_as_defaults(
    path: str | None,
) -> None:
    assert _load_lightgbm_config(path) == {}


def test_load_lightgbm_config_accepts_flat_hyperparameter_json(
    tmp_path: Path,
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


def test_load_lightgbm_config_rejects_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "lightgbm.json"
    config_path.write_text('{"n_estimators": 180, "max_depth": 5}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported LightGBM"):
        _load_lightgbm_config(str(config_path))


def test_load_lightgbm_config_rejects_non_object_json(tmp_path: Path) -> None:
    config_path = tmp_path / "lightgbm.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must contain a JSON object"):
        _load_lightgbm_config(str(config_path))


def test_load_lightgbm_config_rejects_missing_real_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing file"):
        _load_lightgbm_config(str(tmp_path / "missing.json"))
