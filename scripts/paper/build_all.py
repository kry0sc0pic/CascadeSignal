#!/usr/bin/env python3
"""Regenerate every CascadeSignal paper figure into paper/figures/.

Each figure is a standalone script; this runs them in order in fresh processes
(so a 13 M-bar scoring pass frees its memory between figures). Source the env
first so the LIVE_* operating-point overrides apply exactly as the live monitor
uses them:

 set -a && source .env && set +a
 uv run python scripts/paper/build_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve.parent
FIGURES = [
 "fig1_full_history.py",
 "fig2_baselines.py",
 "fig3_operating_characteristics.py",
 "fig4_oos_far.py",
 "fig5_protectable_usd.py",
 "fig6_debounce_ablation.py",
 "fig7_hawkes_gof.py",
 "fig8_cross_protocol.py",
]


def main -> None:
 for name in FIGURES:
 print(f"\n=== {name} ===")
 r = subprocess.run([sys.executable, str(HERE / name)])
 if r.returncode != 0:
 print(f"!! {name} failed (exit {r.returncode})")
 sys.exit(r.returncode)
 print("\nAll figures written to paper/figures/.")


if __name__ == "__main__":
 main
