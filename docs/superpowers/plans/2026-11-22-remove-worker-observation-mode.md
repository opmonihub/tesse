# Remove Worker Observation Mode Implementation Plan

> **For agentic workers:** Execute this plan inline with superpowers:executing-plans. Use the checkboxes to track work.

**Goal:** Remove the legacy observation-only worker mode so it cannot be started by accident.

**Architecture:** Remove the mode rather than hiding or aliasing the flag. A stale invocation must fail during argument parsing instead of silently switching to an order-capable worker. The normal worker keeps its TEST checks; `--shadow` stays a simulation with SDK order sending disabled. Historical events and the separate drill audit report are unchanged.

**Tech Stack:** Python 3.14, argparse, pytest.

## Constraints

Do not restart the deployed worker, touch its operational database, or place orders as part of verification. Preserve concurrent changes in the checkout.

## Tasks

- [x] Add `tests/test_worker_modes.py` covering rejection at all CLI entry points, retention of `--shadow`, and TEST plus SDK gating in the live readiness report. Run the new tests and confirm the legacy CLI still accepts the removed option.
- [x] Update `tesse/cli.py`, `tesse/worker.py`, `tesse/execution/readiness.py`, and the Tesse operation guides. Remove the option and branches; keep the normal worker and shadow paths distinct.
- [x] Run the full pytest suite and inspect the CLI help and diff. Verify the deployed container ID and startup time did not change.
