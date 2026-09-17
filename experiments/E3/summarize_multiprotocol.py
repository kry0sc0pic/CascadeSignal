"""Multi-protocol Hawkes EWS comparison table (E3 extension).

Reads each protocol's `run_hawkes_eval.py` output
(`experiments/E3/output/{protocol}/e3_hawkes_eval.json`) and prints one
comparison row per venue: prevalence, AUPRC lift over prevalence, the n(t)
range, and episode detection at the headline horizon. Aave v2 and Aave v3
use the full D-A definition; Compound v2 and Maker are flagged provisional
(severity-gate degenerate, see `run_hawkes_eval.py`'s module docstring).

Usage:
 python experiments/E3/run_hawkes_eval.py --protocol aave_v2
 python experiments/E3/run_hawkes_eval.py --protocol aave_v3
 python experiments/E3/run_hawkes_eval.py --protocol compound_v2
 python experiments/E3/run_hawkes_eval.py --protocol maker
 python experiments/E3/summarize_multiprotocol.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_hawkes_eval import PROTOCOLS

OUT_DIR = Path("experiments/E3/output")


def _headline_report(payload: dict) -> dict | None:
 reports = payload["reports"]
 return min(reports, key=lambda r: r["horizon_blocks"]) if reports else None


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
 args = parser.parse_args

 rows = []
 for protocol in PROTOCOLS:
 path = args.out_dir / protocol / "e3_hawkes_eval.json"
 if not path.exists:
 print(
 f"{protocol}: no output at {path} -- run run_hawkes_eval.py --protocol {protocol} first"
 )
 continue
 payload = json.loads(path.read_text)
 rep = _headline_report(payload)
 n_detected = sum(1 for e in payload["per_episode"] if e["detected"])
 n_episodes = len(payload["per_episode"])
 n_t_lo, n_t_hi = payload["n_t_range"] if payload["n_t_range"] else (None, None)
 rows.append(
 {
 "protocol": protocol,
 "provisional": payload["degenerate_severity_gate"],
 "prevalence": rep["prevalence"] if rep else None,
 "auprc": rep["auprc"] if rep else None,
 "lift": rep.get("auprc_lift_over_prevalence") if rep else None,
 "n_t_min": n_t_lo,
 "n_t_max": n_t_hi,
 "episodes_detected": f"{n_detected}/{n_episodes}",
 }
 )

 print(
 f"{'protocol':<12} {'provisional':<12} {'prevalence':<12} {'AUPRC':<10} "
 f"{'lift':<10} {'n(t) range':<18} {'episodes'}"
 )
 for r in rows:
 prevalence = f"{r['prevalence']:.2e}" if r["prevalence"] is not None else "n/a"
 auprc = f"{r['auprc']:.4f}" if r["auprc"] is not None else "n/a"
 lift = f"{r['lift']:.1f}x" if r["lift"] is not None else "n/a"
 n_t = (
 f"[{r['n_t_min']:.4f}, {r['n_t_max']:.4f}]"
 if r["n_t_min"] is not None
 else "n/a"
 )
 print(
 f"{r['protocol']:<12} {str(r['provisional']):<12} {prevalence:<12} "
 f"{auprc:<10} {lift:<10} {n_t:<18} {r['episodes_detected']}"
 )

 with open(args.out_dir / "multiprotocol_summary.json", "w") as f:
 json.dump(rows, f, indent=2, default=str)
 print(f"\nWrote {args.out_dir}/multiprotocol_summary.json")


if __name__ == "__main__":
 main
