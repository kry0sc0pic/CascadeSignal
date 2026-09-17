"""Unit tests for `ReserveConfigHistory`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cascadesignal.state.reserve_config_history import ReserveConfigHistory

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"


def _config_dir(tmp_path: Path, rows: list[dict]) -> Path:
 out = tmp_path / "aave_v2_reserve_config" / "chain=1"
 out.mkdir(parents=True)
 pd.DataFrame(rows).to_parquet(out / "cfg.parquet")
 return tmp_path


def _row(asset: str, block: int, lt: float) -> dict:
 return {
 "chain_id": 1,
 "block_number": block,
 "block_timestamp": pd.Timestamp("2021-01-01", tz="UTC")
 + pd.Timedelta(seconds=block),
 "asset": asset,
 "ltv": lt - 0.05,
 "liquidation_threshold": lt,
 "liquidation_bonus": 0.05,
 }


@pytest.fixture
def history(tmp_path: Path) -> ReserveConfigHistory:
 data_dir = _config_dir(
 tmp_path,
 [
 _row(WETH, 100, 0.80),
 _row(WETH, 200, 0.85),
 _row(WETH, 300, 0.86),
 _row(WBTC, 150, 0.70),
 ],
 )
 return ReserveConfigHistory(data_dir=data_dir)


def test_nearest_prior_threshold(history):
 assert history.liquidation_threshold_at(WETH, 250) == pytest.approx(0.85)
 assert history.liquidation_threshold_at(WETH, 300) == pytest.approx(0.86)
 assert history.liquidation_threshold_at(WETH, 999) == pytest.approx(0.86)


def test_before_first_config_returns_none(history):
 # A block earlier than the reserve's first listing config -> None (caller
 # falls back to the frozen reserve_table value).
 assert history.liquidation_threshold_at(WETH, 50) is None


def test_unknown_reserve_returns_none(history):
 assert history.liquidation_threshold_at("0xdeadbeef", 500) is None


def test_thresholds_at_only_includes_covered_reserves(history):
 # At block 175: WETH has a config (block 100), WBTC has one (block 150).
 at_175 = history.thresholds_at(175)
 assert at_175[WETH] == pytest.approx(0.80)
 assert at_175[WBTC] == pytest.approx(0.70)

 # At block 120: WBTC's first config (150) hasn't happened yet -> excluded,
 # so compute_health_factor falls back to WBTC's frozen threshold.
 at_120 = history.thresholds_at(120)
 assert WETH in at_120
 assert WBTC not in at_120


def test_coverage_lists_reserves_with_history(history):
 assert history.coverage == {WETH, WBTC}


def test_missing_config_dir_raises(tmp_path):
 with pytest.raises(FileNotFoundError):
 ReserveConfigHistory(data_dir=tmp_path)
