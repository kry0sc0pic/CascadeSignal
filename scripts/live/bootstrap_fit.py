#!/usr/bin/env python3
"""Fit + persist the deployed operating model (ADR-007 USD-marked blend) on the
full historical liquidation lake, one LiveModelState per protocol -- the same
fit monitor.bootstrap would produce, but runnable offline (no RPC). Paper
figures, the Historical tab, and the live daemon all load these state files, so
run this whenever the operating model or the data changes.

Usage: python scripts/live/bootstrap_fit.py [--protocol aave_v2]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))

from cascadesignal.labels.cascade_labeler import load_liquidations # noqa: E402
from cascadesignal.live.config import DEFAULT_BAR_BLOCKS # noqa: E402
from cascadesignal.live.state import LiveModelState, save_state # noqa: E402
from cascadesignal.models.hawkes import make_operating_model # noqa: E402
from cascadesignal.models.labels import build_liquidation_bars # noqa: E402

PROTOCOLS = ("aave_v2", "aave_v3")


def bootstrap(protocol: str, bar_blocks: int = DEFAULT_BAR_BLOCKS) -> None:
 liq = load_liquidations(protocols=[protocol])
 bars = build_liquidation_bars(liq, bar_blocks=bar_blocks)
 model = make_operating_model
 model.fit(bars)
 origin_block = int(bars["end_block"].iloc[-1])
 n_t_last = float(model.score(bars.tail(1))[0])
 state = LiveModelState.from_fit(
 protocol=protocol,
 bar_blocks=bar_blocks,
 origin_block=origin_block,
 scored_through_block=origin_block,
 model=model,
 n_t_last=n_t_last,
 )
 save_state(state)
 mu, alpha, beta = model.params
 print(f" {protocol}: mu={mu:.3e} alpha={alpha:.4f} beta={beta:.4f} "
 f"excite_scale={state.excite_scale:.1f} n_t_last={n_t_last:.4f} -> saved")


def main -> None:
 ap = argparse.ArgumentParser(description=__doc__)
 ap.add_argument("--protocol", choices=PROTOCOLS, default=None)
 args = ap.parse_args
 for p in ([args.protocol] if args.protocol else list(PROTOCOLS)):
 print(f"Bootstrapping {p} (operating model, bar_blocks={DEFAULT_BAR_BLOCKS})...")
 bootstrap(p)


if __name__ == "__main__":
 main
