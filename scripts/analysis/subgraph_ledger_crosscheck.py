"""Aave v2 Messari subgraph ledger cross-check for the T2 residual (CAS-28 H8).

Every prior cross-check targeted *price*: H7 diffed this engine's HF against
Aave's own `getUserAccountData`; Lever 12 diffed per-reserve prices against
Aave's own `AaveOracle.getAssetPrice`. Neither touches the *balance* side of
the reconstruction (the aToken/debt-token scaled-balance ledger replay). H8
is the balance-side analog: Messari's Aave v2 Ethereum subgraph
(`C2zniPn45RnLDGzVeGZCx2Sw3GXrbc9gL4ZfL8B8Em2j` -- confirmed live via
`lendingProtocols.id == 0xb53c1a33016b2dc2ff3653530bff1848a515c8c5`, matching
`LendingPoolAddressesProvider`'s address already independently verified in
`fetch_aave_v2_price_oracle_sources.py`; `network: MAINNET`, actively
indexed) maintains its own, independently-coded per-user `Position` /
`PositionSnapshot` ledger from the same on-chain events this project
replays -- a genuinely different indexing pipeline to diff against.

`positionSnapshots` are explicit, event-driven entities (one per
balance-changing event, with `blockNumber`/`logIndex`/`balance` recorded),
not a live/pruned state -- confirmed live: the decentralized gateway's
indexers reject historical `block: {number: ...}` time-travel queries this
old ("missing block", pruned), but `positionSnapshots` (plain entity
queries, no time-travel) work at any historical block, since they're
regular stored rows like any other query result.

For a sample of the *current* `unexplained` residual (after Lever 12's
live-oracle correction, i.e. the population any further ledger-level lever
would actually target), fetches each position's nearest-prior-block
snapshot balance and compares it, per reserve and separately for the
collateral vs. debt leg, against this project's own reconstructed
`collateral_units`/`debt_units`.

Cost: The Graph's free tier is 100k queries/mo (see
`data/raw/.checkpoints/thegraph_query_budget.json`, ~95k remaining as of
Lever 12); this script costs roughly 2-10 queries per sampled mismatch
(1 to list positions + 1 per relevant open position for its nearest
snapshot) -- a rounding error at this budget, no chunked/checkpointed pull
needed for a bounded sample.

Usage:
    python scripts/analysis/subgraph_ledger_crosscheck.py [--n 40] [--seed 42]
Writes experiments/T2/output/h8_subgraph_crosscheck.csv.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cascadesignal.state.engine import PositionStateEngine, load_events
from cascadesignal.state.prices import LiveAaveOracleFallback, PreferEthNumeraireOracle
from cascadesignal.state.reserve_config_history import ReserveConfigHistory
from cascadesignal.state.reserves import reserve_table
from cascadesignal.state.t2_gate import (
    apply_live_oracle_fallback,
    bucket_mismatch_causes,
    reconstruct_hf_at_trigger,
)

GATEWAY_BASE = "https://gateway.thegraph.com/api"
# Aave v2 Ethereum mainnet, Messari lending schema -- verified live (see
# module docstring), not guessed or taken on faith from search results alone.
SUBGRAPH_ID = "C2zniPn45RnLDGzVeGZCx2Sw3GXrbc9gL4ZfL8B8Em2j"

OUT_PATH = Path("experiments/T2/output/h8_subgraph_crosscheck.csv")

_POSITIONS_QUERY = """
query($user: String!) {
  positions(where: {account: $user}, first: 1000) {
    id
    side
    blockNumberOpened
    blockNumberClosed
    market { inputToken { id decimals } }
  }
}
"""

_SNAPSHOT_QUERY = """
query($pos: String!, $maxBlock: BigInt!) {
  positionSnapshots(
    where: {position: $pos, blockNumber_lte: $maxBlock}
    orderBy: blockNumber
    orderDirection: desc
    first: 1
  ) {
    balance
  }
}
"""


def _request(url: str, api_key: str, body: dict, retries: int = 5) -> dict:
    full_url = f"{url}/{api_key}/subgraphs/id/{SUBGRAPH_ID}"
    backoff = 2.0
    for attempt in range(retries):
        try:
            resp = requests.post(full_url, json=body, timeout=30)
            data = resp.json()
        except (requests.RequestException, ValueError):
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        if "errors" in data:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        return data["data"]
    raise RuntimeError(f"subgraph query failed after {retries} retries: {body}")


def _subgraph_balances(
    api_key: str, user: str, max_block: int
) -> dict[str, tuple[float, str]]:
    """reserve address -> (balance, side) as of `max_block`, from whichever
    position for that (user, reserve, side) was open at that block."""
    data = _request(
        GATEWAY_BASE, api_key, {"query": _POSITIONS_QUERY, "variables": {"user": user}}
    )
    out: dict[str, tuple[float, str]] = {}
    for pos in data["positions"]:
        opened = int(pos["blockNumberOpened"])
        closed = int(pos["blockNumberClosed"]) if pos["blockNumberClosed"] else None
        if opened > max_block or (closed is not None and closed <= max_block):
            continue
        snap_data = _request(
            GATEWAY_BASE,
            api_key,
            {
                "query": _SNAPSHOT_QUERY,
                "variables": {"pos": pos["id"], "maxBlock": str(max_block)},
            },
        )
        snapshots = snap_data["positionSnapshots"]
        if not snapshots:
            continue
        decimals = int(pos["market"]["inputToken"]["decimals"])
        balance = int(snapshots[0]["balance"]) / (10**decimals)
        reserve = pos["market"]["inputToken"]["id"].lower()
        out[reserve] = (balance, pos["side"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    api_key = os.environ["GRAPH_API_KEY"]

    reserves = reserve_table()
    events = load_events(data_dir="data/raw", protocol="aave_v2")
    liquidations = events[events["event_type"] == "LiquidationCall"]
    engine = PositionStateEngine(events=events, reserve_table=reserves)
    price_oracle = PreferEthNumeraireOracle()
    live_oracle = LiveAaveOracleFallback()
    try:
        config_history: ReserveConfigHistory | None = ReserveConfigHistory()
    except FileNotFoundError:
        config_history = None

    report, position_by_key = reconstruct_hf_at_trigger(
        engine, liquidations, price_oracle, config_history
    )
    causes = bucket_mismatch_causes(
        report, position_by_key, engine, price_oracle, config_history
    )
    # CAS-28 Lever 12: re-baseline against the CURRENT residual, not the
    # pre-Lever-12 unexplained population -- this is what any further
    # ledger-level lever would actually need to explain.
    report = apply_live_oracle_fallback(
        report, causes, position_by_key, engine, live_oracle, config_history
    )
    causes = causes.where(report["mismatch"])

    unexplained = report[causes == "unexplained"]
    print(f"{len(unexplained)} unexplained mismatches (post-Lever-12)")

    rng = random.Random(args.seed)
    sample = unexplained.loc[
        rng.sample(list(unexplained.index), min(args.n, len(unexplained)))
    ]
    print(f"Sampling {len(sample)} (seed={args.seed})\n")

    rows = []
    for i, (_idx, row) in enumerate(sample.iterrows(), start=1):
        key = (row["user"], int(row["block_number"]), int(row["log_index"]))
        position = position_by_key[key]
        max_block = int(row["block_number"]) - 1

        try:
            subgraph_balances = _subgraph_balances(api_key, row["user"], max_block)
        except RuntimeError as exc:
            print(f"  [{i}/{len(sample)}] {row['user']}: FAILED ({exc})")
            continue

        for _, leg in position.iterrows():
            reserve = leg["reserve"]
            our_collateral = float(leg["collateral_units"])
            our_debt = float(leg["debt_units"])
            if reserve not in subgraph_balances and (
                our_collateral > 1e-9 or our_debt > 1e-9
            ):
                rows.append(
                    {
                        "user": row["user"],
                        "block_number": int(row["block_number"]),
                        "reserve": reserve,
                        "leg": "collateral" if our_collateral > our_debt else "debt",
                        "our_units": max(our_collateral, our_debt),
                        "subgraph_units": None,
                        "diff_pct": None,
                        "subgraph_missing": True,
                    }
                )
                continue
            if reserve not in subgraph_balances:
                continue
            sg_balance, sg_side = subgraph_balances[reserve]
            leg_kind = "debt" if sg_side == "BORROWER" else "collateral"
            our_units = our_debt if leg_kind == "debt" else our_collateral
            diff_pct = (
                abs(our_units - sg_balance) / sg_balance if sg_balance > 1e-12 else None
            )
            rows.append(
                {
                    "user": row["user"],
                    "block_number": int(row["block_number"]),
                    "reserve": reserve,
                    "leg": leg_kind,
                    "our_units": our_units,
                    "subgraph_units": sg_balance,
                    "diff_pct": diff_pct,
                    "subgraph_missing": False,
                }
            )
        if i % 10 == 0:
            print(f"  [{i}/{len(sample)}] done", flush=True)

    results = pd.DataFrame(rows)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT_PATH, index=False)

    print(f"\n{len(results)} reserve-legs compared across {len(sample)} positions")
    missing = results[results["subgraph_missing"]]
    print(f"Subgraph has no matching position at all: {len(missing)}")

    comparable = results[~results["subgraph_missing"] & results["diff_pct"].notna()]
    for leg_kind in ("collateral", "debt"):
        subset = comparable[comparable["leg"] == leg_kind]
        if subset.empty:
            continue
        print(f"\n{leg_kind} leg diffs (n={len(subset)}):")
        print(subset["diff_pct"].describe().to_string())

    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
