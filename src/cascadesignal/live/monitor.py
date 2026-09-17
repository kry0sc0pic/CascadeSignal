"""Per-protocol live monitor: bootstrap fit -> catch-up backfill -> forever
loop (finalized-bar scoring + a live "pending" liquidation feed).

Two update paths, deliberately kept separate:
 * Authoritative scoring only ever reads Etherscan `getLogs` over
 `[scored_through_block+1, finalized_head]` -- never the live websocket
 buffer -- so a dropped/reconnected `eth_subscribe` stream (handled
 transparently by `rpc.ws_subscribe_logs`'s own backoff) can never cause
 a missed or double-counted liquidation in the score history. This also
 sidesteps reasoning about buffer completeness across reconnects.
 * The websocket stream feeds a separate `pending` ring buffer purely for
 UI display (recent liquidations, sub-finality latency) -- explicitly
 marked unconfirmed until their block is <= the finalized head, never
 fed into the model.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import numpy as np

from cascadesignal.labels.cascade_labeler import load_liquidations
from cascadesignal.live.bars import bin_liquidations
from cascadesignal.live.cascades import ClosedCluster, ClusterState, update_cluster
from cascadesignal.live.config import LiveConfig, ProtocolConfig
from cascadesignal.live.decode import LIQUIDATION_CALL_TOPIC0, decode_liquidation_log
from cascadesignal.live.history import (
 alerts_path,
 append_jsonl,
 append_jsonl_many,
 bars_path,
 cascades_path,
 liquidations_path,
 read_jsonl_range,
)
from cascadesignal.live.rpc import (
 etherscan_get_logs_paginated,
 get_finalized_block,
 ws_subscribe_logs,
)
from cascadesignal.live.scoring import score_and_advance
from cascadesignal.live.state import LiveModelState, load_state, save_state
from cascadesignal.models.hawkes import make_operating_model
from cascadesignal.models.labels import build_liquidation_bars

log = logging.getLogger("cascadesignal.live")

_RECENT_HISTORY_MAXLEN = 5000
_PENDING_MAXLEN = 200
_CONFIRMED_MAXLEN = 500
_ALERT_LOG_MAXLEN = 200

# How far before a cascade's start an alert still counts as a warning. Mirrors
# `scripts/live/calibrate_thresholds.py`'s HEADLINE_HORIZON (itself
# run_hawkes_eval.py's DEFAULT_HORIZONS[0]) so the live UI measures the same
# question the offline evaluation calibrates against.
EARLY_WARNING_HORIZON = 50
SECONDS_PER_BLOCK = 12.5 # Ethereum mainnet post-Merge slot time


@dataclass
class ScoredBar:
 end_block: int
 n_liquidations: int
 n_t: float
 scored_at: str
 start_block: int | None = None
 r: float | None = None
 lam: float | None = None
 mu: float | None = None
 alpha: float | None = None
 beta: float | None = None


@dataclass
class AlertEvent:
 block: int
 n_t: float
 threshold: float
 fired_at: str


@dataclass
class MonitorStatus:
 protocol: str
 connected: bool
 finalized_block: int | None
 scored_through_block: int
 latest_n_t: float
 threshold: float | None
 alert_active: bool
 last_error: str | None
 score_history: list[ScoredBar] = field(default_factory=list)
 pending_liquidations: list[dict] = field(default_factory=list)
 confirmed_liquidations: list[dict] = field(default_factory=list)
 alert_log: list[AlertEvent] = field(default_factory=list)


class ProtocolMonitor:
 """Owns one protocol's live Hawkes state, ingestion, and status."""

 def __init__(
 self, cfg: ProtocolConfig, live_cfg: LiveConfig, threshold: float | None
 ):
 self.cfg = cfg
 self.live_cfg = live_cfg
 self.threshold = threshold
 self.state: LiveModelState | None = None
 self.connected = False
 self.last_error: str | None = None
 self._alert_active = False
 # Tolerant debounce (ADR-008): the last `debounce_window` above/below-
 # threshold flags; fire once >= debounce_k of them are above. In-memory
 # like _alert_active -- a run spanning a restart just restarts the
 # window, which at most delays one alert.
 self._recent_above: deque[bool] = deque(maxlen=max(1, cfg.debounce_window))
 self.score_history: deque[ScoredBar] = deque(maxlen=_RECENT_HISTORY_MAXLEN)
 self.pending: deque[dict] = deque(maxlen=_PENDING_MAXLEN)
 self.confirmed: deque[dict] = deque(maxlen=_CONFIRMED_MAXLEN)
 self.alert_log: deque[AlertEvent] = deque(maxlen=_ALERT_LOG_MAXLEN)
 self._finalized_block: int | None = None
 self.cluster_state = ClusterState
 self._bars_path = bars_path(cfg.protocol)
 self._liquidations_path = liquidations_path(cfg.protocol)
 self._alerts_path = alerts_path(cfg.protocol)
 self._cascades_path = cascades_path(cfg.protocol)

 # -- bootstrap -----------------------------------------------------

 def bootstrap(self) -> None:
 """Fit on the full historical liquidation lake (reusing the
 offline loader/binner exactly as `experiments/E3/run_hawkes_eval.py`
 does), or resume from persisted state if this protocol already has
 one (skips the historical refit on daemon restart)."""
 existing = load_state(self.cfg.protocol)
 if existing is not None and existing.bar_blocks != self.cfg.bar_blocks:
 # mu/alpha/beta are per-bar units, so a fit made at a different bar
 # width is not merely stale, it is wrong. Refit rather than resume.
 log.warning(
 "protocol=%s persisted fit is at bar_blocks=%d but config says "
 "%d -- discarding it and refitting (per-bar params don't carry "
 "across widths)",
 self.cfg.protocol,
 existing.bar_blocks,
 self.cfg.bar_blocks,
 )
 existing = None
 if existing is not None:
 log.info(
 "protocol=%s resuming from persisted state (scored_through=%d)",
 self.cfg.protocol,
 existing.scored_through_block,
 )
 self.state = existing
 self.cluster_state = ClusterState(
 start_block=existing.cluster_start_block,
 last_block=existing.cluster_last_block,
 count=existing.cluster_count,
 )
 return

 log.info("protocol=%s bootstrap: fitting on historical liquidations", self.cfg.protocol)
 liq = load_liquidations(protocols=[self.cfg.protocol])
 bars = build_liquidation_bars(liq, bar_blocks=self.cfg.bar_blocks)
 model = make_operating_model
 model.fit(bars)

 origin_block = int(bars["end_block"].iloc[-1])
 n_t_last = float(model.score(bars.tail(1))[0])
 self.state = LiveModelState.from_fit(
 protocol=self.cfg.protocol,
 bar_blocks=self.cfg.bar_blocks,
 origin_block=origin_block,
 scored_through_block=origin_block,
 model=model,
 n_t_last=n_t_last,
 )
 save_state(self.state)

 # Seed the UI chart with the tail of history so it isn't empty on
 # first load.
 tail = bars.tail(500)
 tail_scores = model.score(tail)
 now = datetime.now(UTC).isoformat
 for (_, bar), s in zip(tail.iterrows, tail_scores, strict=True):
 self.score_history.append(
 ScoredBar(
 end_block=int(bar["end_block"]),
 n_liquidations=int(bar["n_liquidations"]),
 n_t=float(s),
 scored_at=now,
 )
 )
 log.info(
 "protocol=%s bootstrap complete: origin_block=%d mu=%.3e alpha=%.3f beta=%.3f",
 self.cfg.protocol,
 origin_block,
 *model.params,
 )

 # -- catch-up / live advance ----------------------------------------

 def _decode_gap(self, from_block: int, to_block: int) -> list[dict]:
 logs = etherscan_get_logs_paginated(
 self.cfg.address,
 LIQUIDATION_CALL_TOPIC0,
 from_block,
 to_block,
 self.live_cfg.etherscan_api_key,
 )
 return [decode_liquidation_log(raw, self.cfg.protocol) for raw in logs]

 def _advance_to(self, finalized: int) -> None:
 assert self.state is not None
 if finalized <= self.state.scored_through_block:
 return
 from_block = self.state.scored_through_block + 1
 rows = self._decode_gap(from_block, finalized)
 for row in rows:
 self.confirmed.append(row)
 append_jsonl_many(self._liquidations_path, rows)
 blocks = np.array([r["block_number"] for r in rows], dtype=np.int64)

 # Anchor edge generation at scored_through_block, not origin_block:
 # scored_through_block is always origin_block + k*bar_blocks, so this
 # produces identical bar boundaries without regenerating the whole
 # edge history back to origin on every call -- matters once the
 # monitor has been running long enough that origin_block is millions
 # of blocks behind.
 new_bars = bin_liquidations(
 blocks, self.state.scored_through_block, self.state.bar_blocks, finalized
 )
 if new_bars.empty:
 # No bar closed yet, but liquidations may still have arrived
 # cluster them so the boundary isn't lost, then stop.
 self._update_clusters(blocks)
 return

 model = self.state.to_model
 result = score_and_advance(model, new_bars)
 now = datetime.now(UTC).isoformat
 mu, alpha, beta = model.params
 bar_rows: list[dict] = []
 alert_rows: list[dict] = []
 for (_, bar), n_t, r, lam in zip(
 new_bars.iterrows, result.n_t, result.r, result.lam, strict=True
 ):
 scored_bar = ScoredBar(
 end_block=int(bar["end_block"]),
 start_block=int(bar["start_block"]),
 n_liquidations=int(bar["n_liquidations"]),
 n_t=float(n_t),
 r=float(r),
 lam=float(lam),
 mu=mu,
 alpha=alpha,
 beta=beta,
 scored_at=now,
 )
 self.score_history.append(scored_bar)
 bar_rows.append(asdict(scored_bar))

 # Threshold is tested on EVERY bar, not just the batch's last one.
 # Scoring a batch is mathematically identical to scoring bar by
 # bar, but alerting is not: checking only n_t[-1] silently drops
 # any crossing that isn't the final bar of the batch, which is
 # most of them during a catch-up backfill.
 if self.threshold is not None:
 # Tolerant persistence filter (ADR-008): fire ONE alert the
 # moment >= k of the last `debounce_window` bars are above the
 # threshold (rising edge). Tolerates dips in a flickering near-
 # critical run-up -- fires earlier and spawns fewer distinct
 # false alarms than strict consecutiveness. k=1 == rising edge.
 k = max(1, self.cfg.debounce_k)
 self._recent_above.append(scored_bar.n_t >= self.threshold)
 cond = sum(self._recent_above) >= k
 if cond and not self._alert_active:
 alert = AlertEvent(
 block=scored_bar.end_block,
 n_t=scored_bar.n_t,
 threshold=self.threshold,
 fired_at=now,
 )
 self.alert_log.appendleft(alert)
 alert_rows.append(asdict(alert))
 log.warning(
 "protocol=%s ALERT n_t=%.4f >= threshold=%.4f at block=%d "
 "(debounce k=%d)",
 self.cfg.protocol,
 scored_bar.n_t,
 self.threshold,
 scored_bar.end_block,
 k,
 )
 self._alert_active = cond

 append_jsonl_many(self._alerts_path, alert_rows)
 append_jsonl_many(self._bars_path, bar_rows)

 self.state.scored_through_block = int(new_bars["end_block"].iloc[-1])
 self.state.r_end = model._r_end # noqa: SLF001
 self.state.n_t_last = float(result.n_t[-1])

 # Clustering runs LAST, after alerts are on disk: _persist_cascade reads
 # alerts.jsonl to compute each cascade's lead time, and during a
 # catch-up the alerts that warn about a cluster are produced in the very
 # same call that closes it.
 self._update_clusters(blocks)
 save_state(self.state)

 # Drop now-confirmed rows from the "pending/unconfirmed" UI buffer.
 self.pending = deque(
 (r for r in self.pending if r["block_number"] > finalized),
 maxlen=_PENDING_MAXLEN,
 )

 # -- cascade persistence ------------------------------------------------

 def _update_clusters(self, blocks: np.ndarray) -> None:
 """Fold newly-confirmed liquidation blocks into the open cluster and
 persist any that closed. Runs off raw block numbers, independent of bar
 boundaries -- see live/cascades.py for why this is a separate, UI-only
 heuristic from the Hawkes scoring."""
 assert self.state is not None
 if not len(blocks):
 return
 self.cluster_state, closed_clusters = update_cluster(
 self.cluster_state,
 blocks,
 gap_blocks=self.live_cfg.cluster_gap_blocks,
 min_cluster_size=self.live_cfg.cluster_min_liquidations,
 )
 for cluster in closed_clusters:
 self._persist_cascade(cluster)
 self.state.cluster_start_block = self.cluster_state.start_block
 self.state.cluster_last_block = self.cluster_state.last_block
 self.state.cluster_count = self.cluster_state.count

 def _cascade_alert_tags(self, start_block: int, end_block: int) -> dict:
 """Alerts for a cascade, split by whether they arrived in time to be a
 warning.

 The question that matters is not "did the alarm fire at some point
 during this cascade" (by then it has already happened) but "did it fire
 BEFORE onset, and how much lead did that give". So alerts are read from
 `start_block - EARLY_WARNING_HORIZON` and the ones landing strictly
 before `start_block` are what `lead_blocks` is measured from -- the
 same horizon `calibrate_thresholds.py` calibrates against.
 """
 alerts = read_jsonl_range(
 self._alerts_path,
 start_block - EARLY_WARNING_HORIZON,
 end_block,
 block_key="block",
 )
 pre = [a for a in alerts if a["block"] < start_block]
 first = pre[0] if pre else None
 lead_blocks = start_block - first["block"] if first else None
 return {
 "alert_fired": len(alerts) > 0,
 "early_warned": first is not None,
 "lead_blocks": lead_blocks,
 "lead_seconds": (
 round(lead_blocks * SECONDS_PER_BLOCK, 1)
 if lead_blocks is not None
 else None
 ),
 "early_warning_horizon_blocks": EARLY_WARNING_HORIZON,
 "first_alert_block": alerts[0]["block"] if alerts else None,
 "first_alert_time": alerts[0]["fired_at"] if alerts else None,
 "alert_events": alerts,
 }

 def _persist_cascade(self, cluster: ClosedCluster) -> None:
 """A cluster just closed (gap > cluster_gap_blocks seen after it)
 write its full record to cascades.jsonl so the detail view survives
 a restart. See live/cascades.py's docstring: this is a UI-side
 liquidation-clustering heuristic, not an ADR-001 D-A episode."""
 liqs = read_jsonl_range(
 self._liquidations_path,
 cluster.start_block,
 cluster.end_block,
 block_key="block_number",
 )
 timestamps = [r["block_timestamp"] for r in liqs if r.get("block_timestamp")]
 record = {
 "cascade_id": f"{self.cfg.protocol}_{cluster.start_block}",
 "protocol": self.cfg.protocol,
 "status": "closed",
 "start_block": cluster.start_block,
 "end_block": cluster.end_block,
 "start_time": min(timestamps) if timestamps else None,
 "end_time": max(timestamps) if timestamps else None,
 "n_liquidations": cluster.n_liquidations,
 "cluster_gap_blocks": self.live_cfg.cluster_gap_blocks,
 "min_cluster_size": self.live_cfg.cluster_min_liquidations,
 **self._cascade_alert_tags(cluster.start_block, cluster.end_block),
 }
 append_jsonl(self._cascades_path, record)
 log.info(
 "protocol=%s cascade cluster closed: %s (%d liquidations, "
 "blocks %d-%d, alert_fired=%s)",
 self.cfg.protocol,
 record["cascade_id"],
 cluster.n_liquidations,
 cluster.start_block,
 cluster.end_block,
 record["alert_fired"],
 )

 def ongoing_cascade(self) -> dict | None:
 """The in-progress cluster, if any -- not yet persisted to
 cascades.jsonl (it isn't closed), so this is the only place it's
 visible. `end_block`/`end_time` are `None`: it hasn't closed, so
 there is no end to report."""
 cs = self.cluster_state
 if not cs.is_open or cs.count < self.live_cfg.cluster_min_liquidations:
 return None
 liqs = read_jsonl_range(
 self._liquidations_path, cs.start_block, cs.last_block, block_key="block_number"
 )
 timestamps = [r["block_timestamp"] for r in liqs if r.get("block_timestamp")]
 return {
 "cascade_id": f"{self.cfg.protocol}_{cs.start_block}",
 "protocol": self.cfg.protocol,
 "status": "ongoing",
 "start_block": cs.start_block,
 "end_block": None,
 "start_time": min(timestamps) if timestamps else None,
 "end_time": None,
 "n_liquidations": cs.count,
 "cluster_gap_blocks": self.live_cfg.cluster_gap_blocks,
 "min_cluster_size": self.live_cfg.cluster_min_liquidations,
 **self._cascade_alert_tags(cs.start_block, cs.last_block),
 }

 async def catch_up(self) -> None:
 """One-time startup catch-up: historical data -> current finalized
 head. Can span a large block range (months) -- this is the same
 Etherscan-paginated path the steady-state poll loop uses, just with
 a bigger gap the first time."""
 finalized = await asyncio.to_thread(
 get_finalized_block, self.live_cfg.rpc_http_url
 )
 self._finalized_block = finalized
 assert self.state is not None
 gap = finalized - self.state.scored_through_block
 log.info(
 "protocol=%s catch-up: %d blocks behind finalized head (%d)",
 self.cfg.protocol,
 gap,
 finalized,
 )

 cap = self.live_cfg.max_catchup_blocks
 if cap and gap > cap:
 # Skip forward instead of scoring the whole gap. Bar edges must stay
 # on the origin's clock (see live/bars.py), so snap the new start
 # down to a bar boundary. r_end resets to 0: the skipped blocks were
 # never scored, so carrying decayed history across the gap would be
 # claiming knowledge we don't have.
 bb = self.state.bar_blocks
 target = finalized - cap
 start = self.state.origin_block + (
 (target - self.state.origin_block) // bb
 ) * bb
 log.warning(
 "protocol=%s gap of %d blocks exceeds LIVE_MAX_CATCHUP_BLOCKS=%d "
 "-- skipping to block %d instead of replaying it (set the env "
 "var to 0 to replay the whole gap)",
 self.cfg.protocol,
 gap,
 cap,
 start,
 )
 self.state.scored_through_block = start
 self.state.r_end = 0.0
 self.state.n_t_last = 0.0
 await asyncio.to_thread(self._advance_to, finalized)
 self.connected = True
 log.info(
 "protocol=%s caught up: scored_through_block=%d n_t=%.4f",
 self.cfg.protocol,
 self.state.scored_through_block,
 self.state.n_t_last,
 )

 # -- forever loop -----------------------------------------------------

 async def run(self) -> None:
 await self.catch_up
 await asyncio.gather(self._tail_pending, self._poll_finalized)

 async def _tail_pending(self) -> None:
 """Live (sub-finality) liquidation feed for the UI only."""
 try:
 async for raw_log in ws_subscribe_logs(
 self.live_cfg.rpc_ws_url, self.cfg.address, LIQUIDATION_CALL_TOPIC0
 ):
 row = decode_liquidation_log(raw_log, self.cfg.protocol)
 self.pending.appendleft(row)
 self.connected = True
 except Exception as exc: # pragma: no cover - defensive top-level guard
 self.last_error = f"live feed error: {exc}"
 self.connected = False
 log.exception("protocol=%s live feed failed", self.cfg.protocol)

 async def _poll_finalized(self) -> None:
 while True:
 await asyncio.sleep(self.live_cfg.poll_interval_seconds)
 try:
 finalized = await asyncio.to_thread(
 get_finalized_block, self.live_cfg.rpc_http_url
 )
 self._finalized_block = finalized
 await asyncio.to_thread(self._advance_to, finalized)
 self.last_error = None
 except Exception as exc: # pragma: no cover - defensive top-level guard
 self.last_error = f"poll error: {exc}"
 log.exception("protocol=%s finalized-poll failed", self.cfg.protocol)

 # -- status -----------------------------------------------------------

 def status(self) -> MonitorStatus:
 assert self.state is not None
 return MonitorStatus(
 protocol=self.cfg.protocol,
 connected=self.connected,
 finalized_block=self._finalized_block,
 scored_through_block=self.state.scored_through_block,
 latest_n_t=self.state.n_t_last,
 threshold=self.threshold,
 alert_active=self._alert_active,
 last_error=self.last_error,
 score_history=list(self.score_history)[-500:],
 pending_liquidations=list(self.pending),
 confirmed_liquidations=list(self.confirmed)[-100:][::-1],
 alert_log=list(self.alert_log),
 )


__all__ = ["ProtocolMonitor", "MonitorStatus", "ScoredBar", "AlertEvent"]
