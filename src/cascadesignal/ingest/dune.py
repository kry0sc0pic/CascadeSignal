"""Dune Analytics ingestion layer (CAS-5).

Architecture: one execution per (protocol, event_category) covers the full
study period — pays the scan cost once, then paginates results locally with
no additional credit cost. Much cheaper than per-chunk block-range queries.

Free plan: 2,500 credits/month. One liquidation query costs ~50-200 credits
depending on the date range and result size; all 8 queries fit comfortably.

Workflow:
  1. Create a saved Dune query from a SQL file (cached by query ID).
  2. Execute once to get an execution_id.
  3. Poll for completion via raw HTTP (bypassing dune-client's broken
     performance-tier serialization).
  4. Paginate results 10k rows at a time, writing parquet batches locally.
  5. Record completion in a checkpoint so re-runs skip already-done pulls.

Output layout:
  data/raw/{protocol}/chain={chain_id}/{sql_name}_batch{n:04d}.parquet
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq
import requests

from dune_client.client import DuneClient

from cascadesignal.ingest.schema import CANONICAL_SCHEMA, normalize

log = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent.parent.parent.parent / "scripts" / "dune"

_DUNE_BASE = "https://api.dune.com/api/v1"
_PAGE_SIZE = 10_000
_DEFAULT_START_BLOCK = 11_565_019
_DEFAULT_END_BLOCK = 24_560_000


def _load_sql(name: str) -> str:
    path = _SQL_DIR / f"{name}.sql"
    if not path.exists():
        raise FileNotFoundError(f"SQL template not found: {path}")
    return path.read_text()


class DuneIngester:
    """Downloads on-chain events from Dune Analytics into partitioned parquet files."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        data_dir: Path = Path("data/raw"),
        performance: str = "free",
    ):
        self.api_key = api_key or os.environ.get("DUNE_API_KEY")
        if not self.api_key:
            raise ValueError("DUNE_API_KEY env var or api_key argument is required")
        self._headers = {
            "X-DUNE-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        self.data_dir = Path(data_dir)
        self.performance = performance
        # dune-client used only for query creation (create_query works fine)
        self._client = DuneClient(self.api_key)
        self._checkpoint_dir = self.data_dir / ".checkpoints"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(
        self,
        protocol: str,
        sql_name: str,
        chain_id: int = 1,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
    ) -> int:
        """Execute a full-range query and paginate all results to parquet.

        Skips if a completed checkpoint exists for this (protocol, sql_name).
        Returns total rows written.
        """
        range_key = self._range_key(start_block, end_block)
        cp = self._load_checkpoint(protocol, sql_name, range_key)
        if cp.get("status") == "completed":
            log.info(
                "%s/%s already complete (%d rows)",
                protocol,
                sql_name,
                cp.get("rows", 0),
            )
            return cp.get("rows", 0)

        sql = self._render_sql(_load_sql(sql_name), start_block, end_block)
        query_id = self._get_or_create_query_id(sql_name, sql, range_key)

        # Resume from an existing execution if one was recorded
        exec_id = cp.get("execution_id") or self._execute_query(query_id)
        self._save_checkpoint(
            protocol,
            sql_name,
            {"execution_id": exec_id, "status": "executing"},
            range_key,
        )

        log.info("Waiting for execution %s (%s/%s)…", exec_id, protocol, sql_name)
        self._wait_for_completion(exec_id)

        out_dir = self.data_dir / protocol / f"chain={chain_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        output_stem = sql_name if range_key == "full" else f"{sql_name}_{range_key}"
        total_rows = self._paginate_to_parquet(exec_id, protocol, output_stem, out_dir)
        self._save_checkpoint(
            protocol,
            sql_name,
            {
                "execution_id": exec_id,
                "status": "completed",
                "rows": total_rows,
                "start_block": start_block,
                "end_block": end_block,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            range_key,
        )
        log.info("Finished %s/%s: %d rows", protocol, sql_name, total_rows)
        return total_rows

    def ingest_chunked(
        self,
        protocol: str,
        sql_name: str,
        start_block: int = _DEFAULT_START_BLOCK,
        end_block: int = _DEFAULT_END_BLOCK,
        chunk_blocks: int = 500_000,
        chain_id: int = 1,
    ) -> int:
        """Execute a parameterized SQL file over block chunks.

        This avoids Dune result-size caps for high-volume event streams such as
        Aave core events while preserving resumable checkpoints per chunk.
        """
        total = 0
        cursor = start_block
        while cursor <= end_block:
            chunk_end = min(cursor + chunk_blocks - 1, end_block)
            total += self.ingest(
                protocol=protocol,
                sql_name=sql_name,
                chain_id=chain_id,
                start_block=cursor,
                end_block=chunk_end,
            )
            cursor = chunk_end + 1
        return total

    def check_credits(self) -> dict:
        # Dune's /usage endpoint now requires POST (was GET) -- confirmed
        # 2026-07-14: GET returns 405 Method Not Allowed, POST 200s.
        resp = requests.post(f"{_DUNE_BASE}/usage", headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Query management
    # ------------------------------------------------------------------

    def _get_or_create_query_id(
        self, sql_name: str, sql: str, range_key: str = "full"
    ) -> int:
        suffix = sql_name if range_key == "full" else f"{sql_name}_{range_key}"
        cache_path = self._checkpoint_dir / f"query_id_{suffix}.json"
        sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            if cached.get("sql_sha256") == sql_hash:
                return cached["query_id"]

        query_name = f"cascadesignal_{suffix}"
        log.info("Creating Dune query for %s", query_name)
        # Raw HTTP (via _request) so query creation gets the same 429/5xx backoff
        # as execute/status/results — dune-client's built-in retry gives up under
        # the free tier's rate limits when many chunks each create a query.
        resp = self._request(
            "POST",
            f"{_DUNE_BASE}/query",
            json_body={"name": query_name, "query_sql": sql, "is_private": False},
            timeout=30,
        )
        resp.raise_for_status()
        query_id = resp.json()["query_id"]
        cache_path.write_text(
            json.dumps(
                {
                    "sql_name": sql_name,
                    "range_key": range_key,
                    "query_id": query_id,
                    "sql_sha256": sql_hash,
                }
            )
        )
        log.info("Created Dune query %d for %s", query_id, sql_name)
        return query_id

    @staticmethod
    def _render_sql(
        sql: str,
        start_block: Optional[int],
        end_block: Optional[int],
    ) -> str:
        """Render DuneSQL block placeholders with explicit or study defaults."""
        start = _DEFAULT_START_BLOCK if start_block is None else start_block
        end = _DEFAULT_END_BLOCK if end_block is None else end_block
        return sql.replace("{{start_block}}", str(start)).replace(
            "{{end_block}}", str(end)
        )

    # ------------------------------------------------------------------
    # Execution (raw HTTP to avoid dune-client performance-tier bug)
    # ------------------------------------------------------------------

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
        """HTTP call with exponential backoff on 429 / 5xx (honors Retry-After).

        Dune's free tier rate-limits the status/results endpoints; without this a
        long chunked pull dies on the first 429. Retries are capped, then the last
        response is returned for the caller to raise_for_status().
        """
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
                    "Dune %s .../%s → %d; backing off %.1fs (retry %d/%d)",
                    method,
                    url.rsplit("/", 1)[-1],
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            return resp
        assert resp is not None  # loop always runs >=1 iteration (max_retries >= 0)
        return resp

    def _execute_query(self, query_id: int) -> str:
        """Start a query execution. Returns execution_id."""
        resp = self._request(
            "POST",
            f"{_DUNE_BASE}/query/{query_id}/execute",
            json_body={},  # no performance field → Dune picks the plan default
            timeout=30,
        )
        resp.raise_for_status()
        exec_id = resp.json()["execution_id"]
        log.info("Started execution %s for query %d", exec_id, query_id)
        return exec_id

    def _wait_for_completion(self, exec_id: str, poll_interval: float = 5.0) -> None:
        while True:
            resp = self._request(
                "GET",
                f"{_DUNE_BASE}/execution/{exec_id}/status",
                timeout=30,
            )
            resp.raise_for_status()
            state = resp.json().get("state", "")
            log.debug("Execution %s: %s", exec_id, state)
            if state == "QUERY_STATE_COMPLETED":
                return
            if state in (
                "QUERY_STATE_FAILED",
                "QUERY_STATE_CANCELLED",
                "QUERY_STATE_EXPIRED",
            ):
                err = resp.json().get("error", state)
                raise RuntimeError(f"Dune execution {exec_id} {state}: {err}")
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _paginate_to_parquet(
        self,
        exec_id: str,
        protocol: str,
        sql_name: str,
        out_dir: Path,
    ) -> int:
        total_rows = 0
        offset = 0
        batch_num = 0

        # Remove any incomplete batches from a prior interrupted run
        for stale in out_dir.glob(f"{sql_name}_batch*.parquet"):
            stale.unlink()

        while True:
            log.debug(
                "Fetching batch %d (offset=%d) for %s/%s",
                batch_num,
                offset,
                protocol,
                sql_name,
            )
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
            df = normalize(df, protocol)
            out_path = out_dir / f"{sql_name}_batch{batch_num:04d}.parquet"
            pq.write_table(_df_to_arrow(df), out_path, compression="zstd")
            log.info(
                "Wrote batch %d: %d rows → %s", batch_num, len(rows), out_path.name
            )

            total_rows += len(rows)
            offset += len(rows)
            batch_num += 1

            # Dune returns next_uri when more pages exist
            if not data.get("next_uri") and len(rows) < _PAGE_SIZE:
                break

        return total_rows

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _range_key(start_block: Optional[int], end_block: Optional[int]) -> str:
        if start_block is None and end_block is None:
            return "full"
        if start_block is None or end_block is None:
            raise ValueError("start_block and end_block must be provided together")
        return f"blocks_{start_block:09d}_{end_block:09d}"

    def _checkpoint_path(
        self, protocol: str, sql_name: str, range_key: str = "full"
    ) -> Path:
        suffix = sql_name if range_key == "full" else f"{sql_name}_{range_key}"
        return self._checkpoint_dir / f"{protocol}_{suffix}.json"

    def _load_checkpoint(
        self, protocol: str, sql_name: str, range_key: str = "full"
    ) -> dict:
        path = self._checkpoint_path(protocol, sql_name, range_key)
        return json.loads(path.read_text()) if path.exists() else {}

    def _save_checkpoint(
        self, protocol: str, sql_name: str, data: dict, range_key: str = "full"
    ) -> None:
        self._checkpoint_path(protocol, sql_name, range_key).write_text(
            json.dumps(data, indent=2)
        )


def _df_to_arrow(df: pd.DataFrame):
    import pyarrow as pa

    try:
        return pa.Table.from_pandas(df, schema=CANONICAL_SCHEMA, safe=False)
    except Exception:
        return pa.Table.from_pandas(df)
