"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

import cascadesignal.state.engine as engine_module


@pytest.fixture(autouse=True)
def _isolate_token_ledger_by_default(request, monkeypatch):
 """'s exact-token-ledger path is triggered by global file
 *presence* (`data/raw/corrections/aave_v2_token_ledger/`), not by
 whatever `events`/`reserve_table` a test passes in -- unlike the other,
 additive (keyed by user identity, naturally inert
 against synthetic fake-address fixtures across the suite), H3 *replaces*
 ledger construction wholesale. Once the real pull exists on disk it
 would otherwise silently substitute real on-chain WETH/USDC/... activity
 for every synthetic test's fake users (e.g. "0xu1"), unless disabled
 here -- confirmed against the real pull: 47 previously-passing synthetic
 tests across 7 files broke this way the moment the pull landed.

 The real T2 gate test (`@pytest.mark.t2_gate`) is the one test in the
 suite that needs the genuine on-disk data, so it's exempted. Any test
 that explicitly wants to exercise H3 itself
 (`tests/test_state_reconstruction.py`'s `_write_token_ledger` helper)
 re-points these paths at its own tmp_path fixtures *after* this fixture
 runs, which correctly overrides this default for just that test."""
 if request.node.get_closest_marker("t2_gate") is not None:
 return
 monkeypatch.setattr(engine_module, "_ATOKEN_EVENTS_PATH", Path("/nonexistent"))
