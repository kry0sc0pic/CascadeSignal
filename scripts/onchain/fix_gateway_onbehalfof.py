"""Fix Aave v2 `Deposit`/`Borrow` position misattribution via `onBehalfOf` (CAS-28).

Investigated as a candidate root cause for the T2 gate's dominant
`unexplained` mismatch bucket (2,814 of the post-Chainlink-backfill
mismatches, per `t2_mismatch_report.py`). Aave v2's actual event ABI is

    event Deposit(address indexed reserve, address user,
                   address indexed onBehalfOf, uint256 amount, uint16 indexed referral);
    event Borrow(address indexed reserve, address user,
                  address indexed onBehalfOf, uint256 amount, uint256 borrowRateMode,
                  uint256 borrowRate, uint16 indexed referral);

`user` (non-indexed, in `data`) is `msg.sender` -- whoever *called* the
LendingPool. `onBehalfOf` (indexed) is the actual position holder who
receives the aTokens/debtTokens. For a direct self-serve call the two are
identical, but Aave v2's own periphery contracts route both fields apart:
`WETHGateway`/`WrappedTokenGatewayV2` (ETH deposit/borrow), and the
ParaSwap/Uniswap `LiquiditySwapAdapter`/`RepayAdapter` (collateral-swap
flashloan flows) all call `deposit`/`borrow` with `onBehalfOf` = the real
end user but `msg.sender` = the periphery contract itself.

`scripts/dune/aave_v2_core_events.sql` selected `"user"` for these two event
types -- so every gateway-routed event in `data/raw/aave_v2/` is attributed
to the gateway contract's own aggregate ledger bucket instead of the real
account. Verified directly against on-chain receipts (see PR description):
e.g. tx 0xcaf54b53...ef56a's Deposit log has `data`-decoded `user` =
`0xcc9a0b7c...` (`WETHGateway`, matches our stored value) but
topic2-decoded `onBehalfOf` = `0x56618ca4...`, which is also the tx's own
`from` address -- the real depositor. Empirically, 24.3% of all
Deposit+Borrow volume (190k of 769k events, well beyond the 8
contracts checked by name -- see `_decode`'s universal approach below)
resolves to a different `onBehalfOf`.

Rather than hardcode a gateway-contract allowlist (fragile -- misses any
gateway/adapter/vault not checked by name), this script decodes
`onBehalfOf` for *every* Deposit/Borrow log on the LendingPool contract and
writes it as a universal (tx_hash, log_index) -> onbehalfof correction
table, covering both event types. **`cascadesignal.state.engine.load_events`
only applies the `Borrow` half of this table**, not `Deposit` -- see that
module's docstring for the full reasoning, but in short:

  - `Borrow`'s counterpart, `Repay`, already correctly names `onBehalfOf`
    on-chain regardless of gateway routing, so correcting `Borrow` alone is
    safe (can only add previously-invisible debt, never remove any) and
    empirically improves the T2 mismatch rate slightly (28.5% -> 28.1%).
    But only ~11% of the baseline `unexplained` population is even touched
    by a corrected `Borrow` -- **this rules out gateway misattribution as
    the dominant `unexplained` cause**, the same conclusion the interest-
    accrual and price-staleness investigations already reached.
  - `Deposit`'s counterpart, `Withdraw`, has no onBehalfOf-equivalent field
    to correct from at all (a gateway withdrawal still resolves `user` to
    the gateway -- would need the preceding aToken `Transfer` event, not
    pulled here) -- applying `Deposit` without a symmetric `Withdraw` fix
    creates phantom collateral and was confirmed to regress the mismatch
    rate to 73% when tried. Left in this table (for whoever builds the
    `Withdraw` fix next) but not applied.

Pull mechanics: same Etherscan v2 `getLogs` + recursive 10k-window
bisection as `fetch_svr_feed_events.py` (this script imports
`get_logs_paginated` directly, now parametrized by `topic0`). Chunked into
250k-block windows under `data/raw/.checkpoints/` for resumable progress --
this is a much larger pull (~770k raw events) than the Chainlink backfills,
so losing partial progress would be expensive.

Usage:
    python scripts/onchain/fix_gateway_onbehalfof.py
Writes `data/raw/corrections/aave_v2_onbehalfof/chain=1/onbehalfof.parquet`
(both event types); `cascadesignal.state.engine.load_events` applies the
`Borrow` rows automatically to `aave_v2` events on every future load.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_svr_feed_events import get_logs_paginated  # noqa: E402

LENDING_POOL = "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9"

# keccak256("Deposit(address,address,address,uint256,uint16)")
DEPOSIT_TOPIC0 = "0xde6857219544bb5b7746f48ed30be6386fefc61b2f864cacf559893bf50fd951"
# keccak256("Borrow(address,address,address,uint256,uint256,uint256,uint16)")
BORROW_TOPIC0 = "0xc6a898309e823ee50bac64e45ca8adba6690e99e7841c45d754e2a38e9019d9b"
# Both verified directly against a real receipt's logs -- see module docstring.

_TOPICS_BY_EVENT = {"Deposit": DEPOSIT_TOPIC0, "Borrow": BORROW_TOPIC0}

# Local data spans block 11,565,036 - 23,528,875; pad generously to the
# nearest round numbers so a future re-run over freshly-ingested blocks
# doesn't need this constant touched.
_MIN_BLOCK = 11_500_000
_MAX_BLOCK = 24_500_000
_CHUNK_SIZE = 250_000

_CHECKPOINT_DIR = Path("data/raw/.checkpoints")
_OUT_PARQUET = Path(
    "data/raw/corrections/aave_v2_onbehalfof/chain=1/onbehalfof.parquet"
)


def _chunks() -> list[tuple[int, int]]:
    bounds = list(range(_MIN_BLOCK, _MAX_BLOCK, _CHUNK_SIZE)) + [_MAX_BLOCK]
    return [(lo, hi - 1) for lo, hi in zip(bounds[:-1], bounds[1:])]


def _decode(event_type: str, logs: list[dict]) -> list[dict]:
    rows = []
    for log in logs:
        onbehalfof = "0x" + log["topics"][2][-40:]
        rows.append(
            {
                "tx_hash": log["transactionHash"].lower(),
                "log_index": int(log["logIndex"], 16),
                "event_type": event_type,
                "onbehalfof": onbehalfof.lower(),
            }
        )
    return rows


def _pull_chunk(event_type: str, lo: int, hi: int, api_key: str) -> list[dict]:
    checkpoint = _CHECKPOINT_DIR / f"onbehalfof_{event_type}_{lo}_{hi}.json"
    if checkpoint.exists():
        rows: list[dict] = json.loads(checkpoint.read_text())
        print(f"  {event_type} [{lo},{hi}]: {len(rows)} rows (checkpoint)", flush=True)
        return rows

    logs = get_logs_paginated(
        LENDING_POOL,
        api_key,
        from_block=lo,
        to_block=hi,
        topic0=_TOPICS_BY_EVENT[event_type],
    )
    rows = _decode(event_type, logs)
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps(rows))
    print(f"  {event_type} [{lo},{hi}]: {len(rows)} rows (checkpointed)", flush=True)
    return rows


def _pull_chunk_with_retry(
    event_type: str, lo: int, hi: int, api_key: str, attempts: int = 4
) -> list[dict]:
    for attempt in range(attempts):
        try:
            return _pull_chunk(event_type, lo, hi, api_key)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(
                f"  retry {event_type} [{lo},{hi}] in {wait}s after: {exc}", flush=True
            )
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def main() -> None:
    api_key = os.environ["ETHERSCAN_API_KEY"]
    chunks = _chunks()
    print(
        f"Pulling Deposit + Borrow onBehalfOf over {len(chunks)} block chunks "
        f"[{_MIN_BLOCK}, {_MAX_BLOCK}]...",
        flush=True,
    )

    all_rows: list[dict] = []
    for event_type in ("Deposit", "Borrow"):
        for lo, hi in chunks:
            all_rows.extend(_pull_chunk_with_retry(event_type, lo, hi, api_key))

    if not all_rows:
        raise RuntimeError("No Deposit/Borrow logs pulled -- nothing to write")

    df = pd.DataFrame(all_rows).drop_duplicates(subset=["tx_hash", "log_index"])
    df["log_index"] = df["log_index"].astype("int32")
    _OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_OUT_PARQUET, index=False)
    print(f"\nWrote {len(df)} correction rows to {_OUT_PARQUET}")


if __name__ == "__main__":
    main()
