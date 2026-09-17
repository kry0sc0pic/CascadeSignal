#!/usr/bin/env python3
"""Pull non-Dune external data sources from the CascadeSignal source registry.

This script intentionally separates small public pulls from large/credentialed
sources. It downloads public API payloads, provenance pages, and mempool
manifests/summaries. It does not download multi-GB Flashbots parquet files unless
explicitly requested by a future extension.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests

log = logging.getLogger("ingest_external_sources")

DEFILLAMA_BASE = "https://api.llama.fi"
DEFILLAMA_STABLECOINS_BASE = "https://stablecoins.llama.fi"
FLASHBOTS_BASE = "https://mempool-dumpster.flashbots.net/ethereum/mainnet/"

LENDING_PROTOCOL_SLUGS = [
 "aave-v1",
 "aave-v2",
 "aave-v3",
 "aave-v4",
 "compound-v1",
 "compound-v2",
 "compound-v3",
 "sky-lending",
 "sparklend",
 "morpho-blue",
 "morpho-optimizer-aavev2",
 "morpho-optimizer-aavev3",
 "morpho-optimizer-compoundv2",
 "euler-v1",
 "euler-v2",
]

CHAIN_NAMES = ["Ethereum", "Arbitrum", "Optimism", "Polygon", "Avalanche", "Base"]

MEMPOOL_DATES = [
 date(2025, 10, 10),
 *[date(2026, 1, 31) + timedelta(days=i) for i in range(6)],
]

PROVENANCE_URLS = {
 "aave_historical_liquidations": "https://aave.com/blog/historical-liquidations",
 "zeromev_api": "https://info.zeromev.org/api.html",
 "zeromev_technical": "https://info.zeromev.org/technical.html",
 "flashbots_mempool_dumpster": "https://mempool-dumpster.flashbots.net/index.html",
 "defillama_api_docs": "https://api-docs.defillama.com/",
}


def main -> None:
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--data-dir", default="data/raw", help="Raw data root")
 parser.add_argument("--skip-defillama", action="store_true")
 parser.add_argument("--skip-provenance", action="store_true")
 parser.add_argument("--skip-flashbots", action="store_true")
 args = parser.parse_args

 logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%H:%M:%S",
 )

 out_root = Path(args.data_dir)
 out_root.mkdir(parents=True, exist_ok=True)

 if not args.skip_defillama:
 fetch_defillama(out_root / "defillama")
 if not args.skip_provenance:
 fetch_provenance(out_root / "provenance")
 if not args.skip_flashbots:
 fetch_flashbots_manifests(out_root / "flashbots_mempool")


def fetch_defillama(out_dir: Path) -> None:
 out_dir.mkdir(parents=True, exist_ok=True)
 log.info("Fetching DefiLlama protocol registry")
 protocols = get_json(f"{DEFILLAMA_BASE}/protocols")
 write_json(out_dir / "protocols.json", protocols)

 selected = [p for p in protocols if p.get("slug") in LENDING_PROTOCOL_SLUGS]
 write_json(out_dir / "selected_lending_protocols.json", selected)
 dataframe_for_parquet(selected).to_parquet(
 out_dir / "selected_lending_protocols.parquet", index=False
 )

 protocol_rows = []
 for slug in LENDING_PROTOCOL_SLUGS:
 try:
 payload = get_json(f"{DEFILLAMA_BASE}/protocol/{slug}")
 except requests.HTTPError as exc:
 log.warning("Skipping DefiLlama protocol %s: %s", slug, exc)
 continue
 write_json(out_dir / "protocols" / f"{slug}.json", payload)
 for point in payload.get("tvl", []) or []:
 protocol_rows.append(
 {
 "slug": slug,
 "date": point.get("date"),
 "tvl": point.get("totalLiquidityUSD"),
 }
 )
 if protocol_rows:
 df = pd.DataFrame(protocol_rows)
 df["date"] = pd.to_datetime(df["date"], unit="s", utc=True)
 df.to_parquet(out_dir / "protocol_tvl.parquet", index=False)

 log.info("Fetching DefiLlama chain TVL and market context")
 write_json(out_dir / "chains_current.json", get_json(f"{DEFILLAMA_BASE}/v2/chains"))

 chain_rows = []
 for chain in CHAIN_NAMES:
 try:
 payload = get_json(f"{DEFILLAMA_BASE}/v2/historicalChainTvl/{chain}")
 except requests.HTTPError as exc:
 log.warning("Skipping DefiLlama chain %s: %s", chain, exc)
 continue
 write_json(out_dir / "chains" / f"{slugify(chain)}.json", payload)
 for point in payload:
 chain_rows.append(
 {"chain": chain, "date": point.get("date"), "tvl": point.get("tvl")}
 )
 if chain_rows:
 df = pd.DataFrame(chain_rows)
 df["date"] = pd.to_datetime(df["date"], unit="s", utc=True)
 df.to_parquet(out_dir / "chain_tvl.parquet", index=False)

 for base_url, endpoint, name in [
 (DEFILLAMA_BASE, "/overview/dexs/Ethereum", "dexs_ethereum.json"),
 (DEFILLAMA_BASE, "/overview/fees/Ethereum", "fees_ethereum.json"),
 (DEFILLAMA_STABLECOINS_BASE, "/stablecoins", "stablecoins_current.json"),
 (
 DEFILLAMA_STABLECOINS_BASE,
 "/stablecoincharts/Ethereum",
 "stablecoins_ethereum_history.json",
 ),
 ]:
 try:
 write_json(out_dir / name, get_json(f"{base_url}{endpoint}"))
 except requests.HTTPError as exc:
 log.warning("Skipping DefiLlama endpoint %s: %s", endpoint, exc)


def fetch_provenance(out_dir: Path) -> None:
 out_dir.mkdir(parents=True, exist_ok=True)
 manifest = []
 for name, url in PROVENANCE_URLS.items:
 log.info("Fetching provenance page %s", url)
 resp = get(url)
 html_path = out_dir / f"{name}.html"
 text_path = out_dir / f"{name}.txt"
 html_path.write_text(resp.text)
 text_path.write_text(html_to_text(resp.text))
 manifest.append(
 {
 "name": name,
 "url": url,
 "status_code": resp.status_code,
 "html_path": str(html_path),
 "text_path": str(text_path),
 }
 )
 write_json(out_dir / "manifest.json", manifest)


def fetch_flashbots_manifests(out_dir: Path) -> None:
 out_dir.mkdir(parents=True, exist_ok=True)
 month_keys = sorted({d.strftime("%Y-%m") for d in MEMPOOL_DATES})
 manifest_rows = []

 for month in month_keys:
 url = urljoin(FLASHBOTS_BASE, f"{month}/index.html")
 log.info("Fetching Flashbots mempool index %s", url)
 resp = get(url)
 month_dir = out_dir / month
 month_dir.mkdir(parents=True, exist_ok=True)
 index_path = month_dir / "index.html"
 index_path.write_text(resp.text)
 for item in parse_flashbots_index(resp.text, url):
 manifest_rows.append(item)

 with (out_dir / "file_manifest.csv").open("w", newline="") as f:
 writer = csv.DictWriter(f, fieldnames=["month", "filename", "url", "size_text"])
 writer.writeheader
 writer.writerows(manifest_rows)
 write_json(out_dir / "file_manifest.json", manifest_rows)

 for day in MEMPOOL_DATES:
 month = day.strftime("%Y-%m")
 filename = f"{day.isoformat}_summary.txt"
 url = urljoin(FLASHBOTS_BASE, f"{month}/{filename}")
 try:
 resp = get(url)
 except requests.HTTPError as exc:
 log.warning("Skipping Flashbots summary %s: %s", url, exc)
 continue
 out_path = out_dir / month / filename
 out_path.write_text(resp.text)


def parse_flashbots_index(text: str, base_url: str) -> list[dict[str, str]]:
 rows = []
 month_match = re.search(r"/(\d{4}-\d{2})/index\.html$", base_url)
 month = month_match.group(1) if month_match else ""
 for match in re.finditer(r'<a href="([^"]+)">([^<]+)</a>\s*([^<\n\r]*)', text):
 href, label, size = match.groups
 filename = html.unescape(label.strip)
 if not filename or filename == "../":
 continue
 rows.append(
 {
 "month": month,
 "filename": filename,
 "url": urljoin(base_url, href),
 "size_text": html.unescape(size.strip),
 }
 )
 return rows


def get_json(url: str) -> Any:
 resp = get(url)
 return resp.json


def get(url: str) -> requests.Response:
 resp = requests.get(url, timeout=60, headers={"User-Agent": "CascadeSignal/0.1"})
 resp.raise_for_status
 return resp


def write_json(path: Path, payload: Any) -> None:
 path.parent.mkdir(parents=True, exist_ok=True)
 path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def dataframe_for_parquet(rows: list[dict[str, Any]]) -> pd.DataFrame:
 normalized = []
 for row in rows:
 item = {}
 for key, value in row.items:
 if isinstance(value, (dict, list)):
 item[key] = json.dumps(value, sort_keys=True)
 else:
 item[key] = value
 normalized.append(item)
 return pd.DataFrame(normalized)


def html_to_text(markup: str) -> str:
 text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", markup)
 text = re.sub(r"(?s)<[^>]+>", " ", text)
 text = html.unescape(text)
 text = re.sub(r"[ \t]+", " ", text)
 text = re.sub(r"\n\s*\n\s*", "\n\n", text)
 return text.strip + "\n"


def slugify(value: str) -> str:
 return re.sub(r"[^a-z0-9]+", "_", value.lower).strip("_")


if __name__ == "__main__":
 try:
 main
 except KeyboardInterrupt:
 sys.exit(130)
