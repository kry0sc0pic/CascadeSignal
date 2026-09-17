"""FastAPI service: status/health endpoints + the static UI.

Strictly read-only -- every route here reads in-memory `ProtocolMonitor`
state; nothing signs or writes on-chain. Monitor `run` loops are started as
background asyncio tasks at startup so the server can answer `/health`
immediately while each protocol's catch-up backfill (which can take a while
on first run) proceeds in the background -- `connected` stays `false` in the
status payload until a protocol's catch-up completes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cascadesignal.live.config import LiveConfig, load_config
from cascadesignal.live.history import (
 bars_path,
 cascades_path,
 liquidations_path,
 read_jsonl,
 read_jsonl_range,
)
from cascadesignal.live.monitor import ProtocolMonitor

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
)
log = logging.getLogger("cascadesignal.live.app")

STATIC_DIR = Path(__file__).parent / "static"
THRESHOLDS_PATH = Path("data/live_state/thresholds.json")
# Precomputed major-cascade timelines (n(t) replay + liquidations) served by
# the "Historical" tab. Built offline by scripts/live/build_historical_cascades.py
# -- read-only here, no scoring happens in a request.
HISTORICAL_DIR = Path("data/live_state/historical")

# How far past a cascade's own block range the detail view fetches bars/
# liquidations for, so the UI shows build-up and decay around it, not just
# the exact clustering boundary. A multiple of the clustering gap itself
# (not a separately-invented constant) -- adjust alongside cluster_gap_blocks.
DETAIL_PAD_GAP_MULTIPLES = 2


def _load_thresholds -> dict[str, float]:
 if not THRESHOLDS_PATH.exists:
 log.warning(
 "no %s -- run scripts/live/calibrate_thresholds.py first; "
 "alerting will stay disabled (n(t) still scores/serves fine)",
 THRESHOLDS_PATH,
 )
 return {}
 data = json.loads(THRESHOLDS_PATH.read_text)
 return {p: v["threshold"] for p, v in data.items}


monitors: dict[str, ProtocolMonitor] = {}
_background_tasks: list[asyncio.Task] = []
live_cfg: LiveConfig | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
 global live_cfg
 cfg = load_config
 live_cfg = cfg
 thresholds = _load_thresholds
 for pc in cfg.protocols:
 threshold = (
 pc.threshold_override
 if pc.threshold_override is not None
 else thresholds.get(pc.protocol)
 )
 if pc.threshold_override is not None:
 log.info(
 "protocol=%s threshold overridden to %.6f via LIVE_%s_THRESHOLD "
 "(thresholds.json value ignored)",
 pc.protocol,
 pc.threshold_override,
 pc.protocol.upper,
 )
 monitor = ProtocolMonitor(pc, cfg, threshold)
 await asyncio.to_thread(monitor.bootstrap)
 monitors[pc.protocol] = monitor
 _background_tasks.append(asyncio.create_task(monitor.run))
 log.info("live monitor started for protocols: %s", list(monitors))
 yield
 for task in _background_tasks:
 task.cancel


app = FastAPI(title="CascadeSignal live monitor", lifespan=lifespan)


@app.get("/health")
def health -> JSONResponse:
 ready = len(monitors) > 0
 return JSONResponse(
 {
 "status": "ok" if ready else "starting",
 "protocols": {p: m.connected for p, m in monitors.items},
 }
 )


@app.get("/api/status")
def status_all -> dict:
 return {p: asdict(m.status) for p, m in monitors.items}


@app.get("/api/status/{protocol}")
def status_one(protocol: str) -> dict:
 if protocol not in monitors:
 raise HTTPException(404, f"unknown or not-yet-started protocol {protocol!r}")
 return asdict(monitors[protocol].status)


def _list_cascades(protocol: str) -> list[dict]:
 """Closed clusters (persisted, survive a restart) + the current
 in-progress one (if any), most-recent-first. See live/cascades.py
 these are a UI-side liquidation-clustering heuristic, not ADR-001
 episodes; no accuracy/hit-rate is computed anywhere in this list."""
 closed = read_jsonl(cascades_path(protocol))
 ongoing = monitors[protocol].ongoing_cascade if protocol in monitors else None
 all_cascades = closed + ([ongoing] if ongoing else [])
 return sorted(all_cascades, key=lambda c: c["start_block"], reverse=True)


@app.get("/api/cascades/config")
def cascades_config -> dict:
 if live_cfg is None:
 raise HTTPException(503, "monitor not started yet")
 return {
 "cluster_gap_blocks": live_cfg.cluster_gap_blocks,
 "min_cluster_size": live_cfg.cluster_min_liquidations,
 "detail_pad_blocks": live_cfg.cluster_gap_blocks * DETAIL_PAD_GAP_MULTIPLES,
 "note": (
 "UI-side liquidation-clustering heuristic (gap-based, on raw "
 "confirmed liquidation blocks only) -- not the ADR-001 D-A "
 "cascade definition, which needs amount_usd/position breadth "
 "not available live. No accuracy or hit-rate is computed."
 ),
 }


@app.get("/api/cascades/{protocol}")
def cascades_list(protocol: str) -> list[dict]:
 if protocol not in monitors:
 raise HTTPException(404, f"unknown or not-yet-started protocol {protocol!r}")
 return _list_cascades(protocol)


@app.get("/api/cascades/{protocol}/{cascade_id}")
def cascade_detail(protocol: str, cascade_id: str) -> dict:
 if protocol not in monitors:
 raise HTTPException(404, f"unknown or not-yet-started protocol {protocol!r}")
 cascade = next(
 (c for c in _list_cascades(protocol) if c["cascade_id"] == cascade_id), None
 )
 if cascade is None:
 raise HTTPException(404, f"no cascade {cascade_id!r} for protocol {protocol!r}")

 assert live_cfg is not None
 pad = live_cfg.cluster_gap_blocks * DETAIL_PAD_GAP_MULTIPLES
 lo = cascade["start_block"] - pad
 hi = (cascade["end_block"] or monitors[protocol].state.scored_through_block) + pad

 bars = read_jsonl_range(bars_path(protocol), lo, hi, block_key="end_block")
 liquidations = read_jsonl_range(
 liquidations_path(protocol), lo, hi, block_key="block_number"
 )
 return {
 "cascade": cascade,
 "window": {"start_block": lo, "end_block": hi, "pad_blocks": pad},
 "threshold": monitors[protocol].threshold,
 "bars": sorted(bars, key=lambda b: b["end_block"]),
 "liquidations": sorted(liquidations, key=lambda row: row["block_number"]),
 }


@app.get("/api/historical")
def historical_list -> list[dict]:
 """The precomputed major cascades (index only -- no bar/liq payload), most
 severe first. Empty list if the precompute hasn't been run yet."""
 index_path = HISTORICAL_DIR / "index.json"
 if not index_path.exists:
 return []
 return json.loads(index_path.read_text)


@app.get("/api/historical/{cascade_id}")
def historical_detail(cascade_id: str) -> dict:
 # Guard against path traversal: only serve a bare id that matches a file
 # sitting directly in HISTORICAL_DIR.
 path = HISTORICAL_DIR / f"{cascade_id}.json"
 if cascade_id in ("index",) or path.parent != HISTORICAL_DIR or not path.exists:
 raise HTTPException(404, f"no historical cascade {cascade_id!r}")
 return json.loads(path.read_text)


# Served at both paths: cascade.html is reachable by its filename, so the
# index must be too -- otherwise a plain `href="index.html"` link 404s.
@app.get("/")
@app.get("/index.html")
def index -> FileResponse:
 return FileResponse(STATIC_DIR / "index.html")


@app.get("/historical.html")
def historical_page -> FileResponse:
 return FileResponse(STATIC_DIR / "historical.html")


@app.get("/cascade.html")
def cascade_page -> FileResponse:
 return FileResponse(STATIC_DIR / "cascade.html")


# The pages load their panels from js/*.js (index.html used to carry all its
# script inline); without this mount those requests 404 and the UI renders
# nothing at all. Mounted last so it can't shadow the /api routes above.
app.mount("/js", StaticFiles(directory=STATIC_DIR / "js"), name="js")
app.mount("/css", StaticFiles(directory=STATIC_DIR / "css"), name="css")


__all__ = ["app"]
