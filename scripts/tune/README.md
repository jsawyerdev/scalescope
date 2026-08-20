# LightGBM SMAC Tuning

This directory is intentionally separate from the main ScaleScope runtime.
SMAC3 2.4.0 is incompatible with the app venv's scikit-learn 1.9.0 because
SMAC's RandomForest surrogate imports `DTYPE` from `sklearn.tree._tree`, a
private scikit-learn internal removed in 1.9.0. The tuner therefore uses an
operator-created venv with `scikit-learn<1.9`; `smac` is not a project
dependency and is not installed in the FastAPI/Docker runtime.

From the repo root:

```bash
python3.14 -m venv scripts/tune/.venv-tune
source scripts/tune/.venv-tune/bin/activate
pip install -r scripts/tune/requirements.txt
pip install -e ".[dev]"
```

Run against a DuckDB file that already contains observations for the workload:

```bash
python scripts/tune/tune_lightgbm.py \
  --db-path /path/to/scalescope.duckdb \
  --workload payments-api \
  --trials 40 \
  --horizon 30 \
  --out /tmp/scalescope-lightgbm.json
```

The script prints the replay-lab MAE for the current hardcoded defaults and
the best SMAC incumbent, then writes a flat JSON config:

```json
{
  "learning_rate": 0.08,
  "min_child_samples": 8,
  "n_estimators": 180,
  "num_leaves": 31
}
```

Use the result by explicitly pointing the app at that file:

```bash
SCALESCOPE_LIGHTGBM_CONFIG_PATH=/tmp/scalescope-lightgbm.json \
  uvicorn scalescope.main:app --reload
```

If `SCALESCOPE_LIGHTGBM_CONFIG_PATH` is set but missing, malformed, or contains
unknown hyperparameters, ScaleScope fails during startup instead of silently
falling back to defaults.
