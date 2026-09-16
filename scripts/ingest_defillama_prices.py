#!/usr/bin/env python3
"""Pull daily USD token prices from DefiLlama's free coins API.

Covers the core Aave/Compound/Maker collateral & debt assets over the study
period. No API key required.

Usage:
    python scripts/ingest_defillama_prices.py
    python scripts/ingest_defillama_prices.py --data-dir data/raw
    python scripts/ingest_defillama_prices.py --tokens WETH,WBTC,USDC
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cascadesignal.ingest.defillama_prices import (
    DEFAULT_END,
    DEFAULT_START,
    TOKENS,
    DefiLlamaPriceIngester,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_defillama_prices")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--start-ts", type=int, default=DEFAULT_START)
    parser.add_argument("--end-ts", type=int, default=DEFAULT_END)
    parser.add_argument("--tokens", help="Comma-separated symbol subset (default: all)")
    args = parser.parse_args()

    tokens = TOKENS
    if args.tokens:
        wanted = {t.strip() for t in args.tokens.split(",") if t.strip()}
        tokens = {s: a for s, a in TOKENS.items() if s in wanted}
        missing = wanted - set(tokens)
        if missing:
            log.error("Unknown tokens: %s. Known: %s", sorted(missing), sorted(TOKENS))
            sys.exit(1)

    ingester = DefiLlamaPriceIngester(data_dir=Path(args.data_dir))
    df = ingester.ingest(tokens=tokens, start_ts=args.start_ts, end_ts=args.end_ts)
    log.info(
        "Done. %d total daily price rows across %d tokens.",
        len(df),
        df["symbol"].nunique(),
    )


if __name__ == "__main__":
    main()
