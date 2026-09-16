"""Aave v2 interest-index ingestion via Dune (CAS-47 follow-up).

Pulls `ReserveDataUpdated` (liquidityIndex / variableBorrowIndex per reserve
per block) -- the interest-accrual data `cascadesignal.state` never had,
previously blocked on exhausted Dune credits. See
`scripts/dune/aave_v2_reserve_data_updated.sql`.

Self-contained (plain `requests`, no `dune-client` package) rather than
reusing `cascadesignal.ingest.dune.DuneIngester`: that module hard-imports
`dune_client` at module level for an attribute (`self._client`) that is never
actually called anywhere in the class -- every real API interaction already
goes through raw HTTP. Importing it here would require installing a package
this module doesn't need. Mirrors `defillama_prices.py`'s existing pattern of
a standalone `requests`-based ingester rather than a shared base class.

Output layout:
  data/raw/aave_v2_reserve_index/chain={chain_id}/reserve_data_updated_blocks_{start:09d}_{end:09d}_batch{n:04d}.parquet
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

log = logging.getLogger(__name__)

_SQL_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scripts"
    / "dune"
    / "aave_v2_reserve_data_updated.sql"
)
_DUNE_BASE = "https://api.dune.com/api/v1"
_PAGE_SIZE = 10_000

RESERVE_INDEX_SCHEMA = pa.schema(
    [
        pa.field("chain_id", pa.int32()),
        pa.field("block_number", pa.int64()),
        pa.field("block_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("reserve", pa.string()),
        pa.field("liquidity_index_raw", pa.string()),
        pa.field("variable_borrow_index_raw", pa.string()),
        pa.field("liquidity_rate_raw", pa.string()),
        pa.field("variable_borrow_rate_raw", pa.string()),
        pa.field("stable_borrow_rate_raw", pa.string()),
    ]
)


class ReserveIndexIngester:
    """Pulls Aave v2's ReserveDataUpdated history into partitioned parquet."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        data_dir: Path = Path("data/raw"),
    ):
        self.api_key = api_key or os.environ.get("DUNE_API_KEY")
        if not self.api_key:
            raise ValueError("DUNE_API_KEY env var or api_key argument is required")
        self._headers = {
            "X-DUNE-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        self.data_dir = Path(data_dir)
        self._checkpoint_dir = self.data_dir / ".checkpoints"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._sql_template = _SQL_PATH.read_text()

    def ingest_chunked(
        self,
        protocol: str = "aave_v2_reserve_index",
        start_block: int = 11_565_019,
        end_block: int = 24_560_000,
        chunk_blocks: int = 500_000,
        chain_id: int = 1,
    ) -> int:
        total = 0
        cursor = start_block
        while cursor <= end_block:
            chunk_end = min(cursor + chunk_blocks - 1, end_block)
            total += self._ingest_range(protocol, chain_id, cursor, chunk_end)
            cursor = chunk_end + 1
        return total

    def check_credits(self) -> dict:
        resp = requests.post(f"{_DUNE_BASE}/usage", headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------

    def _ingest_range(
        self, protocol: str, chain_id: int, start_block: int, end_block: int
    ) -> int:
        range_key = f"blocks_{start_block:09d}_{end_block:09d}"
        cp = self._load_checkpoint(range_key)
        if cp.get("status") == "completed":
            log.info("%s already complete (%d rows)", range_key, cp.get("rows", 0))
            return cp.get("rows", 0)

        sql = self._sql_template.replace("{{start_block}}", str(start_block)).replace(
            "{{end_block}}", str(end_block)
        )
        query_id = cp.get("query_id") or self._create_query(range_key, sql)
        exec_id = cp.get("execution_id") or self._execute_query(query_id)
        self._save_checkpoint(
            range_key,
            {"query_id": query_id, "execution_id": exec_id, "status": "executing"},
        )

        log.info("Waiting for execution %s (%s)...", exec_id, range_key)
        self._wait_for_completion(exec_id)

        out_dir = self.data_dir / protocol / f"chain={chain_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        total_rows = self._paginate_to_parquet(exec_id, range_key, out_dir)

        self._save_checkpoint(
            range_key,
            {
                "query_id": query_id,
                "execution_id": exec_id,
                "status": "completed",
                "rows": total_rows,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        log.info("Finished %s: %d rows", range_key, total_rows)
        return total_rows

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        timeout: int = 30,
        max_retries: int = 6,
    ) -> requests.Response:
        backoff = 2.0
        resp = None
        for attempt in range(max_retries + 1):
            resp = requests.request(
                method,
                url,
                headers=self._headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == max_retries:
                    break
                wait = float(resp.headers.get("Retry-After", backoff))
                log.warning(
                    "Dune %s -> %d; backing off %.1fs (retry %d/%d)",
                    method,
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            return resp
        assert resp is not None
        return resp

    def _create_query(self, range_key: str, sql: str) -> int:
        resp = self._request(
            "POST",
            f"{_DUNE_BASE}/query",
            json_body={
                "name": f"cascadesignal_reserve_index_{range_key}",
                "query_sql": sql,
                "is_private": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["query_id"]

    def _execute_query(self, query_id: int) -> str:
        resp = self._request(
            "POST", f"{_DUNE_BASE}/query/{query_id}/execute", json_body={}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["execution_id"]

    def _wait_for_completion(self, exec_id: str, poll_interval: float = 5.0) -> None:
        while True:
            resp = self._request(
                "GET", f"{_DUNE_BASE}/execution/{exec_id}/status", timeout=30
            )
            resp.raise_for_status()
            state = resp.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                return
            if state in (
                "QUERY_STATE_FAILED",
                "QUERY_STATE_CANCELLED",
                "QUERY_STATE_EXPIRED",
            ):
                raise RuntimeError(f"Dune execution {exec_id} {state}")
            time.sleep(poll_interval)

    def _paginate_to_parquet(self, exec_id: str, range_key: str, out_dir: Path) -> int:
        total_rows = 0
        offset = 0
        batch_num = 0
        for stale in out_dir.glob(f"reserve_data_updated_{range_key}_batch*.parquet"):
            stale.unlink()

        while True:
            resp = self._request(
                "GET",
                f"{_DUNE_BASE}/execution/{exec_id}/results",
                params={"limit": _PAGE_SIZE, "offset": offset},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("result", {}).get("rows", [])
            if not rows:
                break

            df = pd.DataFrame(rows)
            df["chain_id"] = df["chain_id"].astype("int32")
            df["block_number"] = df["block_number"].astype("int64")
            df["block_timestamp"] = pd.to_datetime(df["block_timestamp"], utc=True)
            df = df[[f.name for f in RESERVE_INDEX_SCHEMA]]

            out_path = (
                out_dir
                / f"reserve_data_updated_{range_key}_batch{batch_num:04d}.parquet"
            )
            pq.write_table(
                pa.Table.from_pandas(df, schema=RESERVE_INDEX_SCHEMA, safe=False),
                out_path,
                compression="zstd",
            )
            total_rows += len(rows)
            offset += len(rows)
            batch_num += 1
            if not data.get("next_uri") and len(rows) < _PAGE_SIZE:
                break

        return total_rows

    def _checkpoint_path(self, range_key: str) -> Path:
        return self._checkpoint_dir / f"aave_v2_reserve_index_{range_key}.json"

    def _load_checkpoint(self, range_key: str) -> dict:
        path = self._checkpoint_path(range_key)
        return json.loads(path.read_text()) if path.exists() else {}

    def _save_checkpoint(self, range_key: str, data: dict) -> None:
        self._checkpoint_path(range_key).write_text(json.dumps(data, indent=2))
