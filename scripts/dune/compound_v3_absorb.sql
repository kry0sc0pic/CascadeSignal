-- Compound v3 (Comet) absorb calls = liquidations
-- Parameters: {{start_block}}, {{end_block}}
-- Compound v3 uses a different liquidation mechanism: absorb + buy collateral.
-- The decoded table records absorb() calls; accounts are unnested so each
-- underwater account gets one canonical liquidation row.

SELECT
    1                                                          AS chain_id,
    call.call_block_number                                     AS block_number,
    call.call_block_time                                       AS block_timestamp,
    lower(concat('0x', to_hex(call.call_tx_hash)))             AS tx_hash,
    CAST(COALESCE(call.call_tx_index, 0) AS INTEGER)           AS log_index,
    'compound_v3'                                              AS protocol,
    'Absorb'                                                   AS event_type,
    lower(concat('0x', to_hex(account)))                       AS "user",
    NULL                                                       AS collateral_asset,
    NULL                                                       AS debt_asset,
    NULL                                                       AS amount_raw,
    CAST(NULL AS DOUBLE)                                       AS amount_usd,
    lower(concat('0x', to_hex(call.absorber)))                 AS liquidator,
    NULL                                                       AS collateral_seized_raw,
    CAST(NULL AS DOUBLE)                                       AS collateral_seized_usd

FROM compound_v3_ethereum.comet_call_absorb call
CROSS JOIN UNNEST(call.accounts) AS accounts(account)

WHERE call.call_success
  AND call.call_block_date >= DATE '2022-08-01'
  AND call.call_block_date <= DATE '2026-02-28'
  AND call.call_block_number >= {{start_block}}
  AND call.call_block_number <= {{end_block}}

ORDER BY call.call_block_number, call.call_tx_index
