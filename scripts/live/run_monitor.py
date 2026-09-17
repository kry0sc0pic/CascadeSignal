#!/usr/bin/env python3
"""Entrypoint: run the live cascade monitor (Hawkes n(t) + web UI).

Reads ARCHIVE_RPC_URL, ETHERSCAN_API_KEY, and LIVE_* config from the
environment (see .env.example) -- nothing hardcoded, read-only against the
chain (no signing, no private keys).

Usage:
 set -a && source .env && set +a
 python scripts/live/run_monitor.py
 python scripts/live/run_monitor.py --port 8080
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve.parents[2] / "src"))

import uvicorn # noqa: E402


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--host", default="127.0.0.1")
 parser.add_argument("--port", type=int, default=8000)
 args = parser.parse_args
 uvicorn.run("cascadesignal.live.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
 main
