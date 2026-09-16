"""Full-history `AnswerUpdated` pull for the raw Chainlink feeds underlying
GUSD/ENS/LUSD's custom Aave oracle-source adapters (CAS-28, Lever 11b).

Lever 11 (finding 4) confirmed GUSD's 2 sources, xSUSHI's original source,
ENS, and LUSD's original source all revert on `decimals()`/`description()`
and emit no `AnswerUpdated` -- genuinely custom, not raw aggregators. Reading
each one's verified Etherscan source (`getsourcecode`) this session found
three of the four (GUSD both eras, ENS, LUSD's original era) reduce to a
plain Chainlink cross-rate, computed fresh on every call from two ordinary,
pullable feeds -- not an approximation, this is Aave's own real formula:

    GusdPriceProxy / ExtendedGusdPriceProxy (both eras, identical formula):
        latestAnswer() = (1e8 * 1 ether) / ETH_USD.latestAnswer()
        -- GUSD's peg to 1 USD is hard-coded, no separate GUSD/USD feed read.
    EnsUsdToEnsEth:
        latestAnswer() = (ENS_USD.latestAnswer() * 1 ether) / ETH_USD.latestAnswer()
    LSUDUsdToLUSDEth (LUSD's original 2022-2024 source):
        latestAnswer() = (LUSD_USD.latestAnswer() * 1 ether) / ETH_USD.latestAnswer()

All three read the SAME `ETH_USD` proxy (`0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419`
-- Chainlink's canonical mainnet ETH/USD proxy, confirmed live via
`description()`). That proxy has 7 real on-chain phases (via the same
`phaseId()`/`phaseAggregators(n)` walk `validate_aave_oracle_sources_per_era.py`
already uses) -- `chainlink_feeds.ETH_USD_AGGREGATORS` currently has only 1
(phase 5, and only golden-episode-window rows at that: 35,412 rows spanning
just blocks [12,414,796, 17,238,713], not full history). This is the exact
same "missed phases + missed block range" gap H2 (Lever 6) fixed for the
asset/ETH feeds, just never applied to this separate, smaller list. Fixing
it is a prerequisite for GUSD/ENS/LUSD's replication below AND a free
side-benefit for `ChainlinkPriceOracle`'s existing BAL/USDP ETH->USD
conversion (`_convert_via_eth_usd`), which silently lost all coverage for
2023-06 onward (phase 6/7's block range) before this pull.

ENS/USD (`0x5C00128d4d1c2F4f652C267d7bcdD7aC99C16E16`) has 2 phases, both
needed (ENS's Aave wrapper has been live, unchanged, since block 14,338,029).
LUSD/USD (`0x3D7aE7E594f2f2091Ad8798313450130d0Aba3a0`) has 2 phases, but
only phase 1 falls inside LUSD's original-source era (blocks
[15,435,842, 19,723,911) -- phase 2 starts at block 20,188,536, after Aave's
real 2024-04-24 migration to a new (also unmapped, dead-wrapper) source, so
it's out of scope here.

All addresses verified live via `eth_call` (`decimals()`/`description()`/
`phaseId()`/`phaseAggregators()`) before being trusted, same discipline as
every other CAS-28 selector/address -- see `CAS28_mismatch_next_steps.md`'s
Lever 11b section for the specific hits.

Pull mechanics reused wholesale from `backfill_chainlink_eth_feeds_full_history.py`:
Etherscan v2 `getLogs`, recursive 10,000-row-window bisection, per-address
checkpointing.

Usage:
    python scripts/onchain/backfill_custom_adapter_underlying_feeds.py --dry-run
    python scripts/onchain/backfill_custom_adapter_underlying_feeds.py
Writes `data/raw/chainlink/chain=1/custom_adapter_underlying_answer_updated.parquet`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cascadesignal.ingest.schema import normalize, to_arrow  # noqa: E402

RPC_URL = "https://ethereum-rpc.publicnode.com"
_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/chainlink/chain=1/custom_adapter_underlying_answer_updated.parquet"
)

# Aave v2's actual launch block; upper bound resolved live against chain tip.
FULL_HISTORY_START_BLOCK = 11_362_000

# address -> (symbol, decimals) -- all decimals=8, standard Chainlink USD
# feed convention, confirmed live via eth_call this session.
PLAN: dict[str, dict] = {
    # ETH/USD proxy 0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419's 7 real
    # phases (phaseId() == 7, walked via phaseAggregators(1..7)). Phase 5
    # (0x37bc7498...) is already partially in the lake (golden-windows-only,
    # 35,412 rows) -- re-pulled here for full history anyway; downstream
    # dedup on (block_number, log_index) makes the overlap harmless.
    "0xf79d6afbb6da890132f9d7c355e3015f15f3406f": {
        "symbol": "ETH/USD-p1",
        "decimals": 8,
    },
    "0xb103ede8acd6f0c106b7a5772e9d24e34f5ebc2c": {
        "symbol": "ETH/USD-p2",
        "decimals": 8,
    },
    "0x00c7a37b03690fb9f41b5c5af8131735c7275446": {
        "symbol": "ETH/USD-p3",
        "decimals": 8,
    },
    "0xd3fcd40153e56110e6eeae13e12530e26c9cb4fd": {
        "symbol": "ETH/USD-p4",
        "decimals": 8,
    },
    "0x37bc7498f4ff12c19678ee8fe19d713b87f6a9e6": {
        "symbol": "ETH/USD-p5",
        "decimals": 8,
    },
    "0xe62b71cf983019bff55bc83b48601ce8419650cc": {
        "symbol": "ETH/USD-p6",
        "decimals": 8,
    },
    "0x7d4e742018fb52e48b08be73d041c18b21de6fb5": {
        "symbol": "ETH/USD-p7",
        "decimals": 8,
    },
    # ENS/USD proxy 0x5C00128d4d1c2F4f652C267d7bcdD7aC99C16E16's 2 phases.
    "0x780f1bd91a5a22ede36d4b2b2c0eccb9b1726a28": {
        "symbol": "ENS/USD-p1",
        "decimals": 8,
    },
    "0x6cc5173ffd8d674c64f2dc7237730ff021829865": {
        "symbol": "ENS/USD-p2",
        "decimals": 8,
    },
    # LUSD/USD proxy 0x3D7aE7E594f2f2091Ad8798313450130d0Aba3a0's phase 1
    # only (phase 2 postdates LUSD's original-era wrapper, see docstring).
    "0x27b97a63091d185ce056e1747624b9b92baad056": {
        "symbol": "LUSD/USD-p1",
        "decimals": 8,
    },
}


def get_block_number() -> int:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }
    resp = requests.post(RPC_URL, json=payload, timeout=20)
    return int(resp.json()["result"], 16)


def _rows_from_logs(aggregator: str, decimals: int, logs: list[dict]) -> list[dict]:
    rows = []
    for log in logs:
        answer = int(log["topics"][1], 16)
        if answer >= 2**255:
            answer -= 2**256
        rows.append(
            {
                "chain_id": 1,
                "block_number": int(log["blockNumber"], 16),
                "block_timestamp": pd.Timestamp(
                    int(log["timeStamp"], 16), unit="s", tz="UTC"
                ),
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "protocol": "chainlink",
                "event_type": "AnswerUpdated",
                "user": aggregator.lower(),
                "collateral_asset": None,
                "debt_asset": None,
                "amount_raw": str(answer),
                "amount_usd": answer / (10.0**decimals),
                "liquidator": None,
                "collateral_seized_raw": None,
                "collateral_seized_usd": None,
            }
        )
    return rows


def _pull_aggregator(
    addr: str, symbol: str, api_key: str, from_block: int, to_block: int
) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"custom_adapter_underlying_{addr}.json"
    if checkpoint.exists():
        cached: list[dict] = json.loads(checkpoint.read_text())
        print(f"  {symbol:12s} {addr}: {len(cached)} rows (checkpoint)", flush=True)
        return cached

    logs = get_logs_paginated(addr, api_key, from_block=from_block, to_block=to_block)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(logs))
    print(f"  {symbol:12s} {addr}: {len(logs)} rows (checkpointed)", flush=True)
    return logs


def _pull_aggregator_with_retry(
    addr: str,
    symbol: str,
    api_key: str,
    from_block: int,
    to_block: int,
    attempts: int = 4,
) -> list[dict]:
    for attempt in range(attempts):
        try:
            return _pull_aggregator(addr, symbol, api_key, from_block, to_block)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"  retry {addr} in {wait}s after: {exc}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan, pull nothing."
    )
    args = parser.parse_args()

    chain_tip = get_block_number()
    print(
        f"Plan: {len(PLAN)} addresses, blocks "
        f"[{FULL_HISTORY_START_BLOCK}, {chain_tip}] (chain tip)\n"
    )
    for addr, spec in sorted(PLAN.items(), key=lambda kv: kv[1]["symbol"]):
        print(f"  {spec['symbol']:12s} {addr}")
    if args.dry_run:
        return

    api_key = os.environ["ETHERSCAN_API_KEY"]
    all_rows: list[dict] = []
    for i, (addr, spec) in enumerate(sorted(PLAN.items()), start=1):
        print(f"[{i}/{len(PLAN)}]", end=" ", flush=True)
        logs = _pull_aggregator_with_retry(
            addr, spec["symbol"], api_key, FULL_HISTORY_START_BLOCK, chain_tip
        )
        all_rows.extend(_rows_from_logs(addr, spec["decimals"], logs))

    if not all_rows:
        print("No AnswerUpdated rows pulled -- nothing to write.")
        return

    df = normalize(pd.DataFrame(all_rows), protocol="chainlink")
    df = df.drop_duplicates(subset=["user", "block_number", "log_index"])
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    to_arrow(df).to_pandas().to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
