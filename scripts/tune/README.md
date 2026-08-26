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
  --workload sample-app \
  --trials 40 \
  --horizon 30 \
  --out /tmp/scalescope-lightgbm.json
```

The script prints the replay-lab MAE for the current hardcoded defaults and
the best SMAC incumbent, then writes a flat JSON config. Pass
`--baseline-config /path/to/current.json` to also replay-score an existing
tuned config against the same current data:

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

## Periodic re-tuning

`tune_periodic.sh` is the cron driver for periodic re-tuning. It does not create
or modify the isolated tuning venv; if `scripts/tune/.venv-tune` is missing, it
fails with a setup error and leaves the deployed config untouched.

From the repo root:

```bash
./scripts/tune/tune_periodic.sh
./scripts/tune/tune_periodic.sh --workload sample-app --service scalescope --trials 30
./scripts/tune/tune_periodic.sh --workload sample-workload --service scalescope-observe --trials 40
```

Arguments:

- `--workload NAME`: workload to tune; defaults to `sample-app`, matching the
  one-shot tuner's default.
- `--service NAME`: running docker-compose service to copy data from; defaults
  to `scalescope`. Use `scalescope-observe` for an OBSERVE-profile instance.
- `--trials N`: SMAC trial count; defaults to `30`.

The script copies `/data/scalescope.duckdb` from the selected running service,
activates `scripts/tune/.venv-tune`, and runs the tuner against that copied
database. Candidate configs are written to
`scripts/tune/output/<workload>.json.tmp`; the deployed path is
`scripts/tune/output/<workload>.json`.

Promotion is a three-way decision on the current copied data:

- hardcoded LightGBM defaults are scored as `default_mae`;
- the existing deployed config at `scripts/tune/output/<workload>.json`, when
  present, is scored as `baseline_mae`;
- the new SMAC candidate is scored as `best_mae`.

The candidate is promoted only when `best_mae` is lower than the currently
deployed score. If no deployed config exists yet, the hardcoded default score is
the current deployed score. A candidate that beats the hardcoded default but not
the deployed config is discarded without restarting anything. Promotion uses
`mv` from the `.tmp` path into place, so the deployed file is never partially
written.

After promotion, the script restarts running compose services configured to read
`SCALESCOPE_LIGHTGBM_CONFIG_PATH` (`scalescope` and `scalescope-observe`). If no
promotion happens, it exits 0 and does not restart services. Real failures such
as a missing tuning venv, failed `docker compose cp`, or tuner failure exit
non-zero. Every run prints timestamped stdout suitable for cron logs, including
workload, old MAE, new MAE, whether promotion happened, and whether a restart
ran.

Example weekly crontab, adjustable to your checkout path and desired schedule:

```cron
0 3 * * 0 cd /path/to/scalescope && ./scripts/tune/tune_periodic.sh >> scripts/tune/tune_periodic.log 2>&1
```

To opt the compose services into a promoted config, set the container-visible
path in `.env`:

```bash
SCALESCOPE_LIGHTGBM_CONFIG_PATH=/tune-output/sample-app.json
```
