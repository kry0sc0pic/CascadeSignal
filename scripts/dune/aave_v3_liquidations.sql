-- Aave v3 Ethereum LiquidationCall events with USD enrichment
-- Study period: Jan 2022 → Feb 2026 (v3 deployed Ethereum mainnet Jan 2023, v3 Polygon earlier)
-- Table: aave_v3_ethereum.pool_evt_liquidationcall

SELECT
 1 AS chain_id,
 evt.evt_block_number AS block_number,
 evt.evt_block_time AS block_timestamp,
 lower(concat('0x', to_hex(evt.evt_tx_hash))) AS tx_hash,
 CAST(evt.evt_index AS INTEGER) AS log_index,
 'aave_v3' AS protocol,
 'LiquidationCall' AS event_type,
 lower(concat('0x', to_hex(evt."user"))) AS "user",
 lower(concat('0x', to_hex(evt.collateralAsset))) AS collateral_asset,
 lower(concat('0x', to_hex(evt.debtAsset))) AS debt_asset,
 CAST(evt.debtToCover AS VARCHAR) AS amount_raw,
 TRY(CAST(evt.debtToCover AS DOUBLE)
 / POWER(10.0, p_debt.decimals)
 * p_debt.price) AS amount_usd,
 lower(concat('0x', to_hex(evt.liquidator))) AS liquidator,
 CAST(evt.liquidatedCollateralAmount AS VARCHAR) AS collateral_seized_raw,
 TRY(CAST(evt.liquidatedCollateralAmount AS DOUBLE)
 / POWER(10.0, p_coll.decimals)
 * p_coll.price) AS collateral_seized_usd

FROM aave_v3_ethereum.pool_evt_liquidationcall evt

LEFT JOIN prices.usd p_debt
 ON p_debt.contract_address = evt.debtAsset
 AND p_debt.blockchain = 'ethereum'
 AND p_debt.minute = date_trunc('minute', evt.evt_block_time)

LEFT JOIN prices.usd p_coll
 ON p_coll.contract_address = evt.collateralAsset
 AND p_coll.blockchain = 'ethereum'
 AND p_coll.minute = date_trunc('minute', evt.evt_block_time)

WHERE evt.evt_block_date >= DATE '2023-01-01'
 AND evt.evt_block_date <= DATE '2026-02-28'
 AND evt.evt_block_number >= {{start_block}}
 AND evt.evt_block_number <= {{end_block}}

ORDER BY evt.evt_block_number, evt.evt_index
