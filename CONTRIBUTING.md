# Contributing to ScaleScope

Thanks for helping. Bug reports, fixes, documentation, and new ideas are all
welcome. By taking part you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Bugs and features:** open an issue first for anything larger than a
  small fix, so the approach can be agreed before you write it.
- **Security issues:** never open a public issue; see [SECURITY.md](SECURITY.md).

## Set up

ScaleScope needs Python 3.14. The lock file pins exactly what CI installs:

```sh
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lock.txt
pip install --no-deps -e .
```

Run the dashboard against the built-in simulator (no cluster needed):

```sh
SCALESCOPE_DB_PATH=./scalescope.duckdb uvicorn scalescope.main:app --reload
```

Then open http://localhost:8000. `docker compose up --build` runs the same
thing in a container.

## Checks

CI runs these on every pull request; run them before you push:

```sh
black --check src tests sample-workload/app scripts
ruff check .
mypy
pytest -q
shellcheck scripts/*.sh scripts/tune/*.sh sample-workload/scripts/*.sh
node --check src/scalescope/static/app.js
```

`black src tests sample-workload/app scripts` fixes formatting. CodeQL also
scans every pull request.

## Making a change

- **Tests:** a behaviour change needs a test that fails without it.
  `tests/` mirrors `src/scalescope/`; DEMO mode and the simulator
  (`src/scalescope/simulator.py`) make most behaviour testable without a
  cluster.
- **Scope:** keep a pull request to one change. Refactoring alongside a fix
  makes both harder to review.
- **Docs:** update `README.md` or `examples/README.md` when behaviour,
  configuration, or deployment changes. Every `SCALESCOPE_*` variable is
  listed in the README's "Run it" table.
- **Changelog:** add a line under `## [Unreleased]` at the top of
  `CHANGELOG.md` (create the heading if it is not there).
- **Measurements:** claims about forecast or scaling quality in the docs
  come from measurements someone can re-run. Say how you measured.

## Project layout

| Path | What it is |
|---|---|
| `src/scalescope/` | The app: collector, short-term and long-memory forecast models, learning service, node-pool forecasts, performance model, capacity policy, diagnosis, API, dashboard (`static/`) |
| `tests/` | pytest suite |
| `k8s/` | Kustomize install (`k8s/scalescope`), optional actuation RBAC, local-Docker RBAC |
| `sample-workload/` | A small app with its own varying load, for trying ScaleScope end to end |
| `scripts/` | Rebuild, kubeconfig, release notes, LightGBM tuning, and the accuracy evaluations (`eval_long_memory.py`, `eval_node_forecast.py`) |
| `examples/` | The deployment guide and namespace-scoped RBAC examples |

## Releasing (maintainers)

A release is a version bump merged to `main`:

1. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z]`.
2. Set `X.Y.Z` in `pyproject.toml`, the image tags in
   `k8s/scalescope/deployment.yaml` and `sample-workload/k8s/deployment.yaml`,
   and the `?ref=vX.Y.Z` install commands in the READMEs.
   `tests/test_release.py` fails if any of these disagree.
3. Merge. `.github/workflows/release.yml` tags `vX.Y.Z`, creates the GitHub
   Release from that changelog section, and publishes the `X.Y.Z` and
   `latest` images to GHCR.

## License

ScaleScope is licensed under the [Apache License 2.0](LICENSE). By
submitting a contribution you agree that it is licensed under the same
terms (section 5 of the license); no separate CLA is required.
