# Repository Guidelines

## Project Structure & Module Organization

Tesse provides a Python CLI for offline strategy research and an Ebinex TEST worker. The
`tesse/` package contains the CLI (`cli.py`) and worker (`worker.py`). `tesse/forge/` owns
offline research and its YAML templates; `tesse/runtime/`, `tesse/execution/`, and
`tesse/registry/` own worker routing, execution, and model registration. Put reusable
calculations in `tesse/indicators/` or `tesse/primitives/` rather than duplicating them in the
worker or Forge. Use `openspec/` for software change proposals and specs. Local runtime files
belong in the ignored `data/` directory.

## Build, Test, and Development Commands

- `python3.14 -m venv .venv && source .venv/bin/activate` — create and activate a Python 3.14 environment.
- `python -m pip install -e '.[dev]'` — install Tesse and its dev tools. If the SDK is unavailable
  from your package index, install a compatible sibling checkout first: `python -m pip install -e ../ebinex`.
- `python -m tesse forge --help` — check the offline CLI without starting the worker.
- `python -m pytest` — run the test suite once tests are present.
- `ruff check .` — check Python lint and import order.
- `bash docker/build-image.sh` — stage a wheel from the committed SDK checkout (`EBINEX_CHECKOUT`,
  default `../ebinex`) and build the worker image. The Dockerfile currently references missing
  build inputs (`setup.py`, `MANIFEST.in`, `catalog/`); resolve those references before building.

## Coding Style & Naming

Use four-space indentation, `snake_case` for functions and modules, and `PascalCase` for classes.
Target Ruff’s configured 100-character line length.

## Testing Guidelines

Add `tests/test_<module>.py` files with `test_<behavior>` functions for changed behavior. Prefer
deterministic fixtures and fake brokers over network calls or TEST orders. Pytest and Hypothesis
are development dependencies; no tests or coverage threshold are checked in yet.

## Commits & Pull Requests

Use short, action-oriented subjects, as in `Add Tesse project`. In PRs, describe the change,
list verification commands and results, note configuration or runtime impact, and link the
relevant issue or OpenSpec change when available.

## Configuration & Safety

Keep worker execution in `TEST`. Copy `.env.example` to `.env` for local credentials; never
commit credentials, session files, or runtime data.
