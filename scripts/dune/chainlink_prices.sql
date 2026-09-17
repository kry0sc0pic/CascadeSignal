-- Chainlink AnswerUpdated oracle price events
-- Parameters: {{start_block}}, {{end_block}}
-- Used as oracle price features for the contagion graph and feature bars.
-- Note: Chainlink SVR (OEV recapture) paths are handled by the same event type
-- from different contract addresses starting Mar 2025.

SELECT
 CAST(1 AS INTEGER) AS chain_id,
 evt.evt_block_number AS block_number,
 evt.evt_block_time AS block_timestamp,
 lower(concat('0x', to_hex(evt.evt_tx_hash))) AS tx_hash,
 CAST(evt.evt_index AS INTEGER) AS log_index,
 'chainlink' AS protocol,
 'AnswerUpdated' AS event_type,
 lower(cast(evt.contract_address AS varchar)) AS "user",
 NULL AS collateral_asset,
 NULL AS debt_asset,
 -- current is the price in 8-decimal fixed point (for USD feeds)
 CAST(evt.current AS VARCHAR) AS amount_raw,
 TRY_CAST(CAST(evt.current AS DOUBLE) / 1e8 AS DOUBLE) AS amount_usd,
 NULL AS liquidator,
 NULL AS collateral_seized_raw,
 NULL AS collateral_seized_usd

FROM chainlink_ethereum.aggregator_evt_answerupdated evt

WHERE evt.evt_block_date >= DATE '2021-01-01'
 AND evt.evt_block_date <= DATE '2026-02-28'
 AND evt.evt_block_number >= {{start_block}}
 AND evt.evt_block_number <= {{end_block}}

ORDER BY evt.evt_block_number, evt.evt_index
