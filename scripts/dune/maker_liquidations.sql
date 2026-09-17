-- MakerDAO liquidations: Dog Bark (Liq 2.0, Nov 2021+) + Cat Bite (legacy)
-- Tables: maker_ethereum.dog_evt_bark, maker_ethereum.cat_evt_bite

WITH bark_evts AS (
 SELECT
 evt_block_number, evt_block_time, evt_tx_hash, evt_index,
 'Bark' AS event_type,
 urn AS "user",
 ilk AS collateral_type,
 ink AS collateral_seized_num,
 art AS amount_raw_num
 FROM maker_ethereum.dog_evt_bark
 WHERE evt_block_date >= DATE '2021-01-01'
 AND evt_block_date <= DATE '2026-02-28'
 AND evt_block_number >= {{start_block}}
 AND evt_block_number <= {{end_block}}
),
bite_evts AS (
 SELECT
 evt_block_number, evt_block_time, evt_tx_hash, evt_index,
 'Bite' AS event_type,
 urn AS "user",
 ilk AS collateral_type,
 ink AS collateral_seized_num,
 art AS amount_raw_num
 FROM maker_ethereum.cat_evt_bite
 WHERE evt_block_date >= DATE '2021-01-01'
 AND evt_block_date <= DATE '2026-02-28'
 AND evt_block_number >= {{start_block}}
 AND evt_block_number <= {{end_block}}
),
all_evts AS (
 SELECT * FROM bark_evts UNION ALL SELECT * FROM bite_evts
)

SELECT
 1 AS chain_id,
 e.evt_block_number AS block_number,
 e.evt_block_time AS block_timestamp,
 lower(concat('0x', to_hex(e.evt_tx_hash))) AS tx_hash,
 CAST(e.evt_index AS INTEGER) AS log_index,
 'maker' AS protocol,
 e.event_type,
 lower(concat('0x', to_hex(e."user"))) AS "user",
 -- ilk is bytes32 collateral type identifier stored as varchar
 CAST(e.collateral_type AS VARCHAR) AS collateral_asset,
 NULL AS debt_asset,
 CAST(e.amount_raw_num AS VARCHAR) AS amount_raw,
 CAST(NULL AS DOUBLE) AS amount_usd,
 NULL AS liquidator,
 CAST(e.collateral_seized_num AS VARCHAR) AS collateral_seized_raw,
 CAST(NULL AS DOUBLE) AS collateral_seized_usd

FROM all_evts e

ORDER BY e.evt_block_number, e.evt_index
