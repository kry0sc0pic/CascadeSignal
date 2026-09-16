-- Aave v3 Ethereum core events: Supply, Borrow, Repay, Withdraw
-- Tables: aave_v3_ethereum.pool_evt_{supply,borrow,repay,withdraw}

WITH supply_evts AS (
    SELECT evt_block_number, evt_block_time, evt_tx_hash, evt_index,
           'Supply' AS event_type, onBehalfOf AS "user", reserve AS asset, amount AS amount_raw_num
    FROM aave_v3_ethereum.pool_evt_supply
    WHERE evt_block_date >= DATE '2023-01-01' AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}} AND evt_block_number <= {{end_block}}
),
borrow_evts AS (
    SELECT evt_block_number, evt_block_time, evt_tx_hash, evt_index,
           'Borrow' AS event_type, onBehalfOf AS "user", reserve AS asset, amount AS amount_raw_num
    FROM aave_v3_ethereum.pool_evt_borrow
    WHERE evt_block_date >= DATE '2023-01-01' AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}} AND evt_block_number <= {{end_block}}
),
repay_evts AS (
    SELECT evt_block_number, evt_block_time, evt_tx_hash, evt_index,
           'Repay' AS event_type, "user" AS "user", reserve AS asset, amount AS amount_raw_num
    FROM aave_v3_ethereum.pool_evt_repay
    WHERE evt_block_date >= DATE '2023-01-01' AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}} AND evt_block_number <= {{end_block}}
),
withdraw_evts AS (
    SELECT evt_block_number, evt_block_time, evt_tx_hash, evt_index,
           'Withdraw' AS event_type, "user" AS "user", reserve AS asset, amount AS amount_raw_num
    FROM aave_v3_ethereum.pool_evt_withdraw
    WHERE evt_block_date >= DATE '2023-01-01' AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}} AND evt_block_number <= {{end_block}}
),
all_events AS (
    SELECT * FROM supply_evts UNION ALL SELECT * FROM borrow_evts
    UNION ALL SELECT * FROM repay_evts UNION ALL SELECT * FROM withdraw_evts
)

SELECT
    1                                                           AS chain_id,
    e.evt_block_number                                          AS block_number,
    e.evt_block_time                                            AS block_timestamp,
    lower(concat('0x', to_hex(e.evt_tx_hash)))                  AS tx_hash,
    CAST(e.evt_index AS INTEGER)                                AS log_index,
    'aave_v3'                                                   AS protocol,
    e.event_type,
    lower(concat('0x', to_hex(e."user")))                       AS "user",
    NULL                                                        AS collateral_asset,
    lower(concat('0x', to_hex(e.asset)))                        AS debt_asset,
    CAST(e.amount_raw_num AS VARCHAR)                           AS amount_raw,
    TRY(CAST(e.amount_raw_num AS DOUBLE) / POWER(10.0, p.decimals) * p.price) AS amount_usd,
    NULL                                                        AS liquidator,
    NULL                                                        AS collateral_seized_raw,
    CAST(NULL AS DOUBLE)                                        AS collateral_seized_usd
FROM all_events e
LEFT JOIN prices.usd p
    ON  p.contract_address = e.asset
    AND p.blockchain = 'ethereum'
    AND p.minute = date_trunc('minute', e.evt_block_time)
ORDER BY e.evt_block_number, e.evt_index
