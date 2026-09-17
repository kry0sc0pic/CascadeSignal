"""Archive-node ground-truth spot-check for the T2 residual.

Every prior lever (H1-H5, H3/Lever 9) improved this engine's *reconstruction*
of Aave v2's on-chain state, then compared the result against itself (a
mismatch is "our HF >= 1 at trigger"). After the cause-bucketing fix (see
`CAS28_mismatch_next_steps.md`), 84.6% of the remaining mismatches (3,142 of
3,715) are `unexplained` -- HF >= 1 under our best reconstruction, no known
cause identified. This script checks those against Aave v2's own live
contract logic directly: `LendingPool.getUserAccountData(user)`, called via
`eth_call` at `block_number - 1` (this project's own pre-liquidation-state
convention, Lever 1) on a real archive RPC. If the on-chain call *also*
returns HF >= 1 (or no debt), our reconstruction is consistent with the
contract Aave itself ran -- the "mismatch" isn't a bug in this engine, it's
evidence the T2 gate's own definition needs revisiting (H9). If the on-chain
call disagrees (HF < 1), that is a genuine, attributable discrepancy: this
script also decodes `totalCollateralETH`/`totalDebtETH` and, for positions
where `PreferEthNumeraireOracle` used its native ETH-numeraire path
(comparable units, no USD/ETH conversion needed), diffs them against this
engine's own reconstructed collateral/debt to localize the error to price
vs. balance vs. threshold.

`getUserAccountData` selector (`0xbf92857c`) derived via `Crypto.Hash.keccak`
then verified live (real HTTP 200 response, decoded to a sane HF < 1 for a
real liquidated user at its trigger block) before use, same discipline as
every other. Needs `ARCHIVE_RPC_URL` in `.env`
(a real archive-capable endpoint, e.g. Alchemy/Infura free tier -- the
public `ethereum-rpc.publicnode.com` used elsewhere in this repo rejects
every non-latest `eth_call` with "Archive requests require a personal
token", confirmed live before reaching for a paid-tier-capable key).

Usage:
 python scripts/analysis/archive_ground_truth_spotcheck.py [--n 300] [--seed 42] [--cause unexplained]
Writes experiments/T2/output/h7_archive_spotcheck.csv.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.prices import EthNumeraire, PreferEthNumeraireOracle
from cascadesignal.state.reserve_config_history import ReserveConfigHistory
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
 bucket_mismatch_causes,
 reconstruct_hf_at_trigger,
)

LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"
# keccak256("getUserAccountData(address)"), derived via Crypto.Hash.keccak
# verified live against a real historical eth_call before use (see module
# docstring).
SELECTOR_GET_USER_ACCOUNT_DATA = "bf92857c"
# `healthFactor` (and totalCollateralETH/totalDebtETH) are WAD-scaled
# (1e18), NOT RAY (1e27) like the indices/rates elsewhere in this project
# confirmed by hand: totalCollateralETH * liquidationThreshold /
# totalDebtETH matched a /1e18 decode (~0.914) exactly, not /1e27 (~9e-10),
# on the first real call this script made.
_WAD = 10**18
_UINT_MAX = 2**256 - 1

OUT_PATH = Path("experiments/T2/output/h7_archive_spotcheck.csv")


def _eth_call(
 rpc_url: str, to: str, data: str, block_hex: str, retries: int = 5
) -> str:
 payload: dict[str, Any] = {
 "jsonrpc": "2.0",
 "method": "eth_call",
 "params": [{"to": to, "data": data}, block_hex],
 "id": 1,
 }
 result: dict = {}
 for attempt in range(retries):
 try:
 resp = requests.post(rpc_url, json=payload, timeout=20)
 result = resp.json
 except (requests.RequestException, ValueError):
 time.sleep(2.0 * (attempt + 1))
 continue
 if "error" in result:
 time.sleep(2.0 * (attempt + 1))
 continue
 return result["result"]
 raise RuntimeError(f"eth_call failed after {retries} retries: {result}")


def get_user_account_data(rpc_url: str, user: str, block_number: int) -> dict:
 calldata = "0x" + SELECTOR_GET_USER_ACCOUNT_DATA + user[2:].rjust(64, "0").lower
 result = _eth_call(rpc_url, LENDING_POOL, calldata, hex(block_number))
 body = result[2:]
 words = [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]
 total_collateral_eth, total_debt_eth, _available, liq_threshold, ltv, hf_raw = words
 return {
 "total_collateral_eth": total_collateral_eth / 1e18,
 "total_debt_eth": total_debt_eth / 1e18,
 "liquidation_threshold": liq_threshold / 10_000,
 "ltv": ltv / 10_000,
 "health_factor": None if hf_raw == _UINT_MAX else hf_raw / _WAD,
 }


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--n", type=int, default=300)
 parser.add_argument("--seed", type=int, default=42)
 parser.add_argument("--cause", default="unexplained")
 args = parser.parse_args

 rpc_url = os.environ["ARCHIVE_RPC_URL"]

 reserves = reserve_table
 events = load_events(data_dir="data/raw", protocol="aave_v2")
 liquidations = events[events["event_type"] == "LiquidationCall"]
 engine = PositionStateEngine(events=events, reserve_table=reserves)
 price_oracle = PreferEthNumeraireOracle
 eth_oracle = EthNumeraire
 try:
 config_history: ReserveConfigHistory | None = ReserveConfigHistory
 except FileNotFoundError:
 config_history = None

 report, position_by_key = reconstruct_hf_at_trigger(
 engine, liquidations, price_oracle, config_history
 )
 causes = bucket_mismatch_causes(
 report, position_by_key, engine, price_oracle, config_history
 )

 target = report[causes == args.cause]
 print(f"{len(target)} mismatches in cause bucket '{args.cause}'")

 rng = random.Random(args.seed)
 sample_idx = rng.sample(list(target.index), min(args.n, len(target)))
 sample = target.loc[sample_idx]
 print(f"Sampling {len(sample)} (seed={args.seed})\n")

 rows = []
 for i, (_idx, row) in enumerate(sample.iterrows, start=1):
 user, block_number, log_index = (
 row["user"],
 int(row["block_number"]),
 int(row["log_index"]),
 )
 try:
 onchain = get_user_account_data(rpc_url, user, block_number - 1)
 except RuntimeError as exc:
 print(f" [{i}/{len(sample)}] {user} @ {block_number}: FAILED ({exc})")
 continue

 position = position_by_key[(row["user"], block_number, log_index)]
 reserves_involved = position["reserve"].tolist
 eth_prices = eth_oracle.prices_at(reserves_involved, row["block_timestamp"])
 used_eth_numeraire = bool(reserves_involved) and all(
 r in eth_prices for r in reserves_involved
 )

 # Raw (unweighted) collateral/debt in ETH terms, directly comparable
 # to Aave's own totalCollateralETH/totalDebtETH -- only meaningful
 # when `used_eth_numeraire` (units align); NaN otherwise.
 our_collateral_eth = our_debt_eth = float("nan")
 if used_eth_numeraire:
 enabled = (
 position["collateral_enabled"]
 if "collateral_enabled" in position.columns
 else pd.Series(True, index=position.index)
 )
 our_collateral_eth = float(
 (
 position["collateral_units"].clip(lower=0)
 * position["reserve"].map(eth_prices)
 * enabled.astype(bool)
 ).sum
 )
 our_debt_eth = float(
 (
 position["debt_units"].clip(lower=0)
 * position["reserve"].map(eth_prices)
 ).sum
 )

 onchain_hf = onchain["health_factor"]
 rows.append(
 {
 "user": user,
 "block_number": block_number,
 "log_index": log_index,
 "our_hf": row["health_factor"],
 "onchain_hf": onchain_hf,
 "onchain_confirms_mismatch": onchain_hf is None or onchain_hf >= 1.0,
 "used_eth_numeraire": used_eth_numeraire,
 "our_collateral_eth": our_collateral_eth,
 "our_debt_eth": our_debt_eth,
 "onchain_collateral_eth": onchain["total_collateral_eth"],
 "onchain_debt_eth": onchain["total_debt_eth"],
 "onchain_liquidation_threshold": onchain["liquidation_threshold"],
 }
 )
 if i % 25 == 0:
 print(f" [{i}/{len(sample)}] done", flush=True)
 time.sleep(0.05)

 results = pd.DataFrame(rows)
 OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
 results.to_csv(OUT_PATH, index=False)

 print(f"\n{len(results)}/{len(sample)} archive calls succeeded")
 if results.empty:
 return
 confirmed = int(results["onchain_confirms_mismatch"].sum)
 print(
 f"On-chain confirms mismatch (HF >= 1 or no debt): {confirmed}/{len(results)} "
 f"({confirmed / len(results):.1%})"
 )
 disagree = results[~results["onchain_confirms_mismatch"]]
 print(
 f"On-chain DISAGREES (shows HF < 1): {len(disagree)}/{len(results)} "
 f"({len(disagree) / len(results):.1%})"
 )
 if not disagree.empty:
 print("\nDisagreement on-chain HF distribution:")
 print(disagree["onchain_hf"].describe.to_string)

 comparable = disagree[disagree["used_eth_numeraire"]]
 if not comparable.empty:
 collateral_diff_pct = (
 comparable["our_collateral_eth"] - comparable["onchain_collateral_eth"]
 ) / comparable["onchain_collateral_eth"]
 debt_diff_pct = (
 comparable["our_debt_eth"] - comparable["onchain_debt_eth"]
 ) / comparable["onchain_debt_eth"]
 print(
 f"\nComponent diffs, ETH-numeraire-matched disagreements (n={len(comparable)}):"
 )
 print(
 f" collateral_eth (ours - onchain) / onchain: "
 f"mean={collateral_diff_pct.mean:+.2%} median={collateral_diff_pct.median:+.2%}"
 )
 print(
 f" debt_eth (ours - onchain) / onchain: "
 f"mean={debt_diff_pct.mean:+.2%} median={debt_diff_pct.median:+.2%}"
 )

 print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
 main
