-- Aave v2 Ethereum ReserveDataUpdated: liquidityIndex / variableBorrowIndex
-- per reserve per block -- the interest-accrual data state engine
-- needs and never had (blocked on exhausted Dune credits until this pull).
-- Table: aave_v2_ethereum.lendingpool_evt_reservedataupdated
-- Indices/rates are RAY-scaled (1e27) fixed point; kept as VARCHAR to avoid
-- precision loss, matching amount_raw's convention in ingest/schema.py.

SELECT
 1 AS chain_id,
 evt.evt_block_number AS block_number,
 evt.evt_block_time AS block_timestamp,
 lower(concat('0x', to_hex(evt.reserve))) AS reserve,
 CAST(evt.liquidityIndex AS VARCHAR) AS liquidity_index_raw,
 CAST(evt.variableBorrowIndex AS VARCHAR) AS variable_borrow_index_raw,
 CAST(evt.liquidityRate AS VARCHAR) AS liquidity_rate_raw,
 CAST(evt.variableBorrowRate AS VARCHAR) AS variable_borrow_rate_raw,
 CAST(evt.stableBorrowRate AS VARCHAR) AS stable_borrow_rate_raw

FROM aave_v2_ethereum.lendingpool_evt_reservedataupdated evt

WHERE evt.evt_block_date >= DATE '2021-01-01'
 AND evt.evt_block_date <= DATE '2026-02-28'
 AND evt.evt_block_number >= {{start_block}}
 AND evt.evt_block_number <= {{end_block}}

ORDER BY evt.evt_block_number
