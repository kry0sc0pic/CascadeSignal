-- Compound v2 Ethereum LiquidateBorrow events (cErc20 + cEther combined)
-- Tables: compound_v2_ethereum.cerc20_evt_liquidateborrow
--         compound_v2_ethereum.cether_evt_liquidateborrow

WITH erc20_liqs AS (
    SELECT
        evt_block_number, evt_block_time, evt_tx_hash, evt_index,
        liquidator, borrower AS "user",
        cTokenCollateral     AS collateral_asset,
        contract_address     AS debt_market,
        repayAmount          AS amount_raw_num,
        seizeTokens          AS collateral_seized_num
    FROM compound_v2_ethereum.cerc20_evt_liquidateborrow
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
ether_liqs AS (
    SELECT
        evt_block_number, evt_block_time, evt_tx_hash, evt_index,
        liquidator, borrower AS "user",
        cTokenCollateral     AS collateral_asset,
        contract_address     AS debt_market,
        repayAmount          AS amount_raw_num,
        seizeTokens          AS collateral_seized_num
    FROM compound_v2_ethereum.cether_evt_liquidateborrow
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
all_liqs AS (
    SELECT * FROM erc20_liqs UNION ALL SELECT * FROM ether_liqs
)

SELECT
    1                                                           AS chain_id,
    l.evt_block_number                                          AS block_number,
    l.evt_block_time                                            AS block_timestamp,
    lower(concat('0x', to_hex(l.evt_tx_hash)))                  AS tx_hash,
    CAST(l.evt_index AS INTEGER)                                AS log_index,
    'compound_v2'                                               AS protocol,
    'LiquidateBorrow'                                           AS event_type,
    lower(concat('0x', to_hex(l."user")))                       AS "user",
    lower(concat('0x', to_hex(l.collateral_asset)))             AS collateral_asset,
    lower(concat('0x', to_hex(l.debt_market)))                  AS debt_asset,
    CAST(l.amount_raw_num AS VARCHAR)                           AS amount_raw,
    -- USD enrichment requires knowing the cToken → underlying mapping
    -- This is handled post-hoc in state reconstruction (CAS-13)
    CAST(NULL AS DOUBLE)                                        AS amount_usd,
    lower(concat('0x', to_hex(l.liquidator)))                   AS liquidator,
    CAST(l.collateral_seized_num AS VARCHAR)                    AS collateral_seized_raw,
    CAST(NULL AS DOUBLE)                                        AS collateral_seized_usd

FROM all_liqs l

ORDER BY l.evt_block_number, l.evt_index
