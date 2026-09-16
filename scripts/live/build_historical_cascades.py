#!/usr/bin/env python3
"""Precompute the historical-cascade timelines served by the live UI's
"Historical" tab.

The live monitor only scores ~200k blocks back (a month), so the big
2021--2025 cascades are outside its window. This script materializes, for each
protocol's `is_primary` D-A episodes (the ones ADR-001 flags as the major
cascades), a self-contained timeline JSON the app serves read-only:

  * the Hawkes n(t) alarm curve replayed across a padded window around the
    episode, and
  * the underlying confirmed liquidations (block, USD, assets) in that window.

Historical and live are the SAME system, just over an older block range -- so
this resolves the operating point EXACTLY as the live monitor does, per
protocol and independently: the fitted (mu, alpha, beta) and bar clock from
`data/live_state/<p>.json`, and the threshold + debounce_k from the same
env-override-then-thresholds.json path config.py uses (LIVE_<P>_THRESHOLD /
LIVE_<P>_DEBOUNCE_K). Change the live operating point and re-run this, and the
Historical tab moves with it. aave_v2 and aave_v3 never share parameters.

Run (after `set -a && source .env && set +a`, so the LIVE_* overrides apply):
    python scripts/live/build_historical_cascades.py
Outputs: data/live_state/historical/<episode_id>.json + index.json
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cascadesignal.labels.cascade_labeler import load_liquidations  # noqa: E402
from cascadesignal.live.config import DEFAULT_DEBOUNCE_WINDOW  # noqa: E402
from cascadesignal.live.state import STATE_DIR, load_state  # noqa: E402
from cascadesignal.models.hawkes import make_operating_model  # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars  # noqa: E402

PROTOCOLS = ("aave_v2", "aave_v3")
LABELS_DIR = Path("data/curated/labels")
THRESHOLDS_PATH = STATE_DIR / "thresholds.json"
OUT_DIR = STATE_DIR / "historical"

SECONDS_PER_BLOCK = 12.5  # Ethereum post-merge; only used for lead-time copy.

# Blocks of context to show on each side of the episode's own span, so the
# curve shows n(t) building into the cascade and decaying after it. ~2000
# blocks is ~7h at 12.5s/block.
PAD_BLOCKS = 2000

# Short human labels for the primary episodes, keyed by (protocol, start_block).
EPISODE_LABELS: dict[tuple[str, int], dict[str, str]] = {
    ("aave_v2", 12464836): {
        "name": "May 2021 crash",
        "blurb": "Mid-May 2021 market-wide deleveraging (China mining "
        "crackdown era) -- the largest Aave v2 D-A cascade in the table.",
    },
    ("aave_v2", 14959193): {
        "name": "June 2022 contagion",
        "blurb": "Terra/LUNA aftermath -- Celsius/3AC-era forced "
        "deleveraging on Aave v2.",
    },
    ("aave_v2", 13737970): {
        "name": "December 2021 crash",
        "blurb": "Dec 4 2021 flash crash -- rapid cross-asset liquidation "
        "wave on Aave v2.",
    },
    ("aave_v3", 21762804): {
        "name": "February 2025 crash",
        "blurb": "Feb 3 2025 flash deleveraging -- the largest Aave v3 D-A "
        "cascade in the table.",
    },
}


def _operating_point(protocol: str) -> tuple[float | None, int, int]:
    """(threshold, debounce_k, debounce_window) resolved exactly as the live
    monitor resolves them (config.py): LIVE_<P>_* env override wins, else
    thresholds.json / default. Keeps Historical and Live on one operating
    point, per protocol."""
    p = protocol.upper()
    thresholds = json.loads(THRESHOLDS_PATH.read_text()) if THRESHOLDS_PATH.exists() else {}
    thr_env = os.environ.get(f"LIVE_{p}_THRESHOLD")
    threshold = float(thr_env) if thr_env else thresholds.get(protocol, {}).get("threshold")
    debounce_k = int(os.environ.get(f"LIVE_{p}_DEBOUNCE_K", "1"))
    debounce_window = int(
        os.environ.get(f"LIVE_{p}_DEBOUNCE_W", str(DEFAULT_DEBOUNCE_WINDOW))
    )
    return threshold, debounce_k, debounce_window


def _load_primary_episodes(protocol: str) -> pd.DataFrame:
    df = pd.read_parquet(LABELS_DIR / protocol / "episodes.parquet")
    df = df[df["is_primary"]]
    return df.sort_values("severity_usd", ascending=False).reset_index(drop=True)


def _scored_bars(protocol: str) -> tuple[pd.DataFrame, int]:
    """All-history bars for `protocol` with an `n_t` column, scored by the
    persisted live fit. Returns (bars, bar_blocks)."""
    state = load_state(protocol)
    if state is None:
        raise SystemExit(
            f"no persisted live fit for {protocol!r} at "
            f"{STATE_DIR / (protocol + '.json')}; run the live monitor once "
            "so it bootstraps a fit, then re-run this."
        )
    liq = load_liquidations(protocols=[protocol])
    bars = build_liquidation_bars(liq, bar_blocks=state.bar_blocks)

    model = make_operating_model()  # ADR-007 USD-marked blend
    model._params = (state.mu, state.alpha, state.beta)  # noqa: SLF001
    model._excite_scale = getattr(state, "excite_scale", 1.0)  # noqa: SLF001
    model._r_end = 0.0  # noqa: SLF001 -- score from the start of history
    bars = bars.assign(n_t=model.score(bars))
    return bars, state.bar_blocks


def _first_alarm_block(
    bars: list[dict], threshold: float | None, k: int,
    window: int = DEFAULT_DEBOUNCE_WINDOW,
) -> int | None:
    """end_block of the bar where the tolerant debounce (ADR-008) first fires:
    the first bar where >= k of the last `window` bars are above threshold.
    Mirrors the live monitor's persistence filter (k=1 == first crossing;
    window == k == old strict-consecutive)."""
    if threshold is None:
        return None
    k = max(1, k)
    recent: deque[bool] = deque(maxlen=max(window, k))
    for b in bars:
        recent.append(b["n_t"] >= threshold)
        if sum(recent) >= k:
            return b["end_block"]
    return None


def _cumulative_cost(payload: dict) -> tuple[list[int], list[float], float]:
    """(bar_end_blocks, cumulative_usd_at_each_bar, total_window_usd)."""
    liq = sorted(payload["liquidations"], key=lambda r: r["block_number"])
    total = sum((r["amount_usd"] or 0.0) for r in liq)
    blocks, cum = [], []
    acc, j = 0.0, 0
    for b in payload["bars"]:
        while j < len(liq) and liq[j]["block_number"] <= b["end_block"]:
            acc += liq[j]["amount_usd"] or 0.0
            j += 1
        blocks.append(b["end_block"])
        cum.append(acc)
    return blocks, cum, total


def _lead(payload: dict) -> dict | None:
    """Two early-warning metrics per ADR-006, both reported. `advance_warning_*`
    (t_start - t_alarm) is how far before the cascade *begins* the debounced
    alarm fires -- the honest before-onset number. `lead_*` (t_half - t_alarm,
    the existing/validated metric) is the alarm -> 50%-of-USD figure; retained
    unchanged. None if the alarm never fires."""
    cross = _first_alarm_block(
        payload["bars"], payload["threshold"], payload["debounce_k"],
        payload.get("debounce_window", DEFAULT_DEBOUNCE_WINDOW),
    )
    if cross is None:
        return None
    adv = payload["start_block"] - cross  # >0 == warned before onset
    out = {
        "cross_block": cross,
        "advance_warning_blocks": adv,
        "advance_warning_minutes": round(adv * SECONDS_PER_BLOCK / 60),
    }
    # metric 2: alarm -> block where cumulative USD first reaches 50% of window
    _, _, total = _cumulative_cost(payload)
    liq = sorted(payload["liquidations"], key=lambda r: r["block_number"])
    acc, half_block = 0.0, None
    for r in liq:
        acc += r["amount_usd"] or 0.0
        if total and acc >= 0.5 * total:
            half_block = r["block_number"]
            break
    if half_block is not None:
        out["half_cost_block"] = half_block
        out["lead_blocks"] = half_block - cross
        out["lead_minutes"] = round((half_block - cross) * SECONDS_PER_BLOCK / 60)
    return out


def _preview(payload: dict, n: int = 72) -> dict:
    """Downsampled n(t) + cumulative-cost-fraction overlay for the card
    sparkline (keeps index.json small)."""
    bars = payload["bars"]
    if not bars:
        return {"nt": [], "cost": []}
    _, cum, total = _cumulative_cost(payload)
    total = total or 1.0
    m = len(bars)
    idx = [round(i * (m - 1) / (n - 1)) for i in range(n)] if m > n else list(range(m))
    return {
        "nt": [round(bars[i]["n_t"], 4) for i in idx],
        "cost": [round(cum[i] / total, 4) for i in idx],
    }


def _episode_payload(
    protocol: str, ep: pd.Series, bars: pd.DataFrame, liq: pd.DataFrame,
    threshold: float | None, debounce_k: int,
    debounce_window: int = DEFAULT_DEBOUNCE_WINDOW,
) -> dict:
    start, end = int(ep["start_block"]), int(ep["end_block"])
    lo, hi = start - PAD_BLOCKS, end + PAD_BLOCKS

    win_bars = bars[(bars["end_block"] >= lo) & (bars["end_block"] <= hi)]
    win_liq = liq[(liq["block_number"] >= lo) & (liq["block_number"] <= hi)].sort_values(
        "block_number"
    )

    label = EPISODE_LABELS.get((protocol, start), {})
    return {
        "id": str(ep["episode_id"]),
        "protocol": protocol,
        "name": label.get("name") or str(ep["start_time"])[:10],
        "blurb": label.get("blurb", ""),
        "start_block": start,
        "end_block": end,
        "start_time": str(ep["start_time"]),
        "end_time": str(ep["end_time"]),
        "window": {"start_block": lo, "end_block": hi, "pad_blocks": PAD_BLOCKS},
        "threshold": threshold,
        "debounce_k": debounce_k,
        "debounce_window": debounce_window,
        "stats": {
            "total_liquidated_usd": float(ep["total_liquidated_usd"]),
            "num_positions": int(ep["num_positions"]),
            "num_accounts": int(ep["num_accounts"]),
            "max_generations": int(ep["max_generations"]),
        },
        "bars": [
            {
                "end_block": int(b.end_block),
                "n_liquidations": int(b.n_liquidations),
                "n_t": float(b.n_t),
            }
            for b in win_bars.itertuples()
        ],
        "liquidations": [
            {
                "block_number": int(r.block_number),
                "block_timestamp": str(r.block_timestamp),
                "tx_hash": r.tx_hash,
                "user": r.user,
                "collateral_asset": r.collateral_asset,
                "debt_asset": r.debt_asset,
                "amount_usd": None if pd.isna(r.amount_usd) else float(r.amount_usd),
            }
            for r in win_liq.itertuples()
        ],
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    for protocol in PROTOCOLS:
        episodes = _load_primary_episodes(protocol)
        if episodes.empty:
            print(f"  [skip {protocol}: no is_primary episodes]")
            continue
        threshold, debounce_k, debounce_window = _operating_point(protocol)
        print(
            f"scoring {protocol} full history (threshold={threshold} "
            f"debounce_k={debounce_k} debounce_window={debounce_window})..."
        )
        bars, bar_blocks = _scored_bars(protocol)
        liq = load_liquidations(protocols=[protocol])

        for _, ep in episodes.iterrows():
            payload = _episode_payload(
                protocol, ep, bars, liq, threshold, debounce_k, debounce_window
            )
            (OUT_DIR / f"{payload['id']}.json").write_text(json.dumps(payload))
            peak = max((b["n_t"] for b in payload["bars"]), default=0.0)
            _, _, window_cost = _cumulative_cost(payload)
            fires = _first_alarm_block(
                payload["bars"], threshold, debounce_k, debounce_window
            )
            index.append(
                {
                    "id": payload["id"],
                    "protocol": protocol,
                    "name": payload["name"],
                    "blurb": payload["blurb"],
                    "start_time": payload["start_time"],
                    "start_block": payload["start_block"],
                    "end_block": payload["end_block"],
                    "threshold": threshold,
                    "debounce_k": debounce_k,
                    "debounce_window": debounce_window,
                    "peak_n_t": peak,
                    "detected": fires is not None,
                    "window_cost_usd": window_cost,
                    "lead": _lead(payload),
                    "preview": _preview(payload),
                    "stats": payload["stats"],
                }
            )
            print(
                f"  {payload['name']:<22} {len(payload['bars']):>5} bars, "
                f"{len(payload['liquidations']):>4} liq, peak n(t)={peak:.4f}, "
                f"detected={fires is not None}"
            )

    (OUT_DIR / "index.json").write_text(json.dumps(index))
    print(f"wrote {len(index)} episodes + index.json to {OUT_DIR}")


if __name__ == "__main__":
    main()
