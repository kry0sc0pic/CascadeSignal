"""Append-only JSONL persistence for the live monitor's per-bar, per-
liquidation, and per-cascade history.

`state.py` persists only the small, overwritten model checkpoint (enough to
resume scoring); the in-memory deques in `monitor.py` (score history,
confirmed liquidations, alert log) are capped and lost on restart. These
files are the durable record behind them, so the cascade detail/replay view
in the UI still works after a restart -- plain append-only JSONL, no new
dependency, one file per protocol per record kind under
`data/live_state/<protocol>/`.
"""

from __future__ import annotations

import json
from pathlib import Path

HISTORY_DIR = Path("data/live_state")


def protocol_history_dir(protocol: str, base_dir: Path = HISTORY_DIR) -> Path:
 return base_dir / protocol


def bars_path(protocol: str, base_dir: Path = HISTORY_DIR) -> Path:
 return protocol_history_dir(protocol, base_dir) / "bars.jsonl"


def liquidations_path(protocol: str, base_dir: Path = HISTORY_DIR) -> Path:
 return protocol_history_dir(protocol, base_dir) / "liquidations.jsonl"


def alerts_path(protocol: str, base_dir: Path = HISTORY_DIR) -> Path:
 return protocol_history_dir(protocol, base_dir) / "alerts.jsonl"


def cascades_path(protocol: str, base_dir: Path = HISTORY_DIR) -> Path:
 return protocol_history_dir(protocol, base_dir) / "cascades.jsonl"


def append_jsonl(path: Path, record: dict) -> None:
 append_jsonl_many(path, (record,))


def append_jsonl_many(path: Path, records) -> None:
 """Append many records under a single open/close.

 At a 1-block bar width a catch-up can close hundreds of thousands of bars
 in one pass; opening the file per row makes that syscall-bound for no
 reason.
 """
 records = list(records)
 if not records:
 return
 path.parent.mkdir(parents=True, exist_ok=True)
 with path.open("a") as f:
 f.writelines(json.dumps(r, default=str) + "\n" for r in records)


def read_jsonl(path: Path) -> list[dict]:
 if not path.exists:
 return []
 with path.open as f:
 return [json.loads(line) for line in f if line.strip]


def read_jsonl_range(
 path: Path, start_block: int, end_block: int, block_key: str = "end_block"
) -> list[dict]:
 """Read only rows whose `block_key` falls within `[start_block,
 end_block]`.

 Rows are appended in ascending block order, so this streams and stops at
 the first row past `end_block` rather than parsing the whole file -- at a
 1-block bar width `bars.jsonl` grows by ~7200 rows/day, and the detail
 view only ever needs a small window out of it.
 """
 if not path.exists:
 return []
 out: list[dict] = []
 with path.open as f:
 for line in f:
 if not line.strip:
 continue
 row = json.loads(line)
 block = row[block_key]
 if block > end_block:
 break
 if block >= start_block:
 out.append(row)
 return out


__all__ = [
 "HISTORY_DIR",
 "protocol_history_dir",
 "bars_path",
 "liquidations_path",
 "alerts_path",
 "cascades_path",
 "append_jsonl",
 "append_jsonl_many",
 "read_jsonl",
 "read_jsonl_range",
]
