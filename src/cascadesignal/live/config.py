"""Live-monitor configuration -- .env only, nothing hardcoded.

Every value here has a `LIVE_*`/`ETHERSCAN_API_KEY`/`ARCHIVE_RPC_URL`
environment-variable source; defaults (contract addresses, bar width) mirror
the values already pinned elsewhere in this repo (`configs/ingest.yaml`,
`live/decode.py`) so the monitor behaves identically to the offline pipeline
unless a variable is explicitly overridden.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cascadesignal.live.cascades import DEFAULT_CLUSTER_GAP_BLOCKS, DEFAULT_MIN_CLUSTER_SIZE
from cascadesignal.live.decode import PROTOCOL_ADDRESSES

PROTOCOLS = ("aave_v2", "aave_v3")
# One block (~12.5s) per bar, not 5. n(t) only updates when a bar closes, so a
# wider bar discards lead time that is already present in the data: at a
# matched alarm budget, narrowing 5 -> 1 left detection counts unchanged but
# roughly doubled the median warning lead. Note mu/alpha/beta are PER-BAR
# units, so changing this invalidates a persisted fit -- monitor.bootstrap()
# detects the mismatch and refits.
DEFAULT_BAR_BLOCKS = 1
# Consecutive above-threshold bars required to raise one alert. k=1 is the
# original rising-edge behaviour (fire on the first bar that crosses). k>1 is a
# persistence/debounce filter: it suppresses isolated single-bar n(t) spikes
# (most false alarms) while sustained cascade excitation still fires, at the
# cost of k-1 bars of lead. See scripts/live/tune_threshold.py for the FAR/lead
# tradeoff this was tuned against.
DEFAULT_DEBOUNCE_K = 1
# Tolerant-debounce window (ADR-008): fire once >= k above-threshold bars occur
# within the last W bars (W >= k), instead of requiring k STRICTLY CONSECUTIVE.
# A near-critical run-up flickers around the threshold; strict consecutiveness
# resets on every dip, firing late AND spawning extra distinct false alarms on
# each dip-recovery. The window absorbs the dips: lower FAR *and* earlier fire.
# W == k reproduces the old strict-consecutive behaviour.
DEFAULT_DEBOUNCE_WINDOW = 100
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_REFIT_INTERVAL_SECONDS = 7 * 24 * 3600  # weekly
# The historical lake ends months behind the chain head, so a fresh bootstrap
# would otherwise replay that entire gap -- at one bar per block that is ~1.1M
# bars and hundreds of MB of scored-bar log per protocol, nearly all of it
# quiet. Cap how far back a cold start scores from; ~200k blocks is about a
# month, enough for the UI to have real cascades to show. Set 0 to disable
# the cap and replay the whole gap.
DEFAULT_MAX_CATCHUP_BLOCKS = 200_000


@dataclass(frozen=True)
class ProtocolConfig:
    protocol: str
    address: str
    bar_blocks: int
    # Overrides thresholds.json when set, so the operating point can be moved
    # and watched live without re-running the (multi-minute) calibration.
    threshold_override: float | None = None
    # Persistence filter: raise one alert once this many above-threshold bars
    # occur within the last `debounce_window` bars (1 = fire on first crossing).
    # See DEFAULT_DEBOUNCE_K / DEFAULT_DEBOUNCE_WINDOW (ADR-008).
    debounce_k: int = 1
    debounce_window: int = DEFAULT_DEBOUNCE_WINDOW


@dataclass(frozen=True)
class LiveConfig:
    rpc_http_url: str
    rpc_ws_url: str
    etherscan_api_key: str
    poll_interval_seconds: float
    refit_interval_seconds: float
    cluster_gap_blocks: int
    cluster_min_liquidations: int
    max_catchup_blocks: int
    protocols: tuple[ProtocolConfig, ...]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_opt_float(name: str) -> float | None:
    raw = os.environ.get(name)
    return float(raw) if raw else None


def load_config() -> LiveConfig:
    rpc_http_url = os.environ["ARCHIVE_RPC_URL"]
    rpc_ws_url = os.environ.get("ARCHIVE_RPC_WS_URL") or rpc_http_url.replace(
        "https://", "wss://"
    )
    etherscan_api_key = os.environ["ETHERSCAN_API_KEY"]

    protocols = tuple(
        ProtocolConfig(
            protocol=p,
            address=(
                os.environ.get(f"LIVE_{p.upper()}_ADDRESS") or PROTOCOL_ADDRESSES[p]
            ).lower(),
            bar_blocks=_env_int(f"LIVE_{p.upper()}_BAR_BLOCKS", DEFAULT_BAR_BLOCKS),
            threshold_override=_env_opt_float(f"LIVE_{p.upper()}_THRESHOLD"),
            debounce_k=_env_int(f"LIVE_{p.upper()}_DEBOUNCE_K", DEFAULT_DEBOUNCE_K),
            debounce_window=_env_int(
                f"LIVE_{p.upper()}_DEBOUNCE_W", DEFAULT_DEBOUNCE_WINDOW
            ),
        )
        for p in PROTOCOLS
    )

    return LiveConfig(
        rpc_http_url=rpc_http_url,
        rpc_ws_url=rpc_ws_url,
        etherscan_api_key=etherscan_api_key,
        poll_interval_seconds=_env_float(
            "LIVE_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        refit_interval_seconds=_env_float(
            "LIVE_REFIT_INTERVAL_SECONDS", DEFAULT_REFIT_INTERVAL_SECONDS
        ),
        cluster_gap_blocks=_env_int(
            "LIVE_CLUSTER_GAP_BLOCKS", DEFAULT_CLUSTER_GAP_BLOCKS
        ),
        cluster_min_liquidations=_env_int(
            "LIVE_CLUSTER_MIN_LIQUIDATIONS", DEFAULT_MIN_CLUSTER_SIZE
        ),
        max_catchup_blocks=_env_int(
            "LIVE_MAX_CATCHUP_BLOCKS", DEFAULT_MAX_CATCHUP_BLOCKS
        ),
        protocols=protocols,
    )


__all__ = ["ProtocolConfig", "LiveConfig", "load_config", "PROTOCOLS"]
