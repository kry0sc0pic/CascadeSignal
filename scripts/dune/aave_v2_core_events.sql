-- Aave v2 Ethereum core events: Deposit, Borrow, Repay, Withdraw
-- Used for position-state reconstruction (CAS-13) and feature bars (CAS-20)
-- Tables: aave_v2_ethereum.lendingpool_evt_{deposit,borrow,repay,withdraw}
--
-- CAS-28 fix: Borrow's `user` param is msg.sender (the caller), NOT the
-- position holder -- `onBehalfOf` is. For direct self-serve calls the two
-- are identical, but Aave v2's own periphery contracts (WETHGateway,
-- WrappedTokenGatewayV2, ParaSwap/Uniswap collateral-swap adapters) call
-- borrow() with onBehalfOf = the real end user but msg.sender = the
-- periphery contract, so selecting "user" silently misattributed debt to
-- the gateway's own aggregate ledger bucket instead of the real borrower
-- (see scripts/onchain/fix_gateway_onbehalfof.py, which backfills this
-- surgically for already-pulled data via Etherscan getLogs since Dune
-- credits were exhausted when this was found). Select onbehalfof here so a
-- fresh pull doesn't need that backfill.
--
-- Deposit has the exact same `user`-vs-`onBehalfOf` split on-chain, but is
-- deliberately NOT fixed the same way here: its counterpart, Withdraw, has
-- no onBehalfOf-equivalent field at all (a gateway withdrawal still
-- resolves "user" to the gateway, since it pulls the caller's aTokens via
-- transferFrom before calling withdraw) -- so fixing Deposit alone would
-- create phantom collateral (credited on deposit, never netted back out on
-- withdrawal) whenever a real account uses gateway on both legs. Confirmed
-- this regresses the T2 mismatch rate from 28.5% to 73% if applied without
-- an equivalent Withdraw fix (which needs correlating the preceding aToken
-- Transfer event, not built). Leave Deposit selecting "user" until that
-- Withdraw-side fix exists; see engine.py's module docstring.
WITH deposits AS (
    SELECT
        evt_block_number, evt_block_time, evt_block_date, evt_tx_hash, evt_index,
        'Deposit'                                       AS event_type,
        "user"                                          AS "user",
        reserve                                         AS asset,
        amount                                          AS amount_raw_num
    FROM aave_v2_ethereum.lendingpool_evt_deposit
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
borrows AS (
    SELECT
        evt_block_number, evt_block_time, evt_block_date, evt_tx_hash, evt_index,
        'Borrow'                                        AS event_type,
        onbehalfof                                      AS "user",
        reserve                                         AS asset,
        amount                                          AS amount_raw_num
    FROM aave_v2_ethereum.lendingpool_evt_borrow
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
repays AS (
    SELECT
        evt_block_number, evt_block_time, evt_block_date, evt_tx_hash, evt_index,
        'Repay'                                         AS event_type,
        "user"                                          AS "user",
        reserve                                         AS asset,
        amount                                          AS amount_raw_num
    FROM aave_v2_ethereum.lendingpool_evt_repay
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
withdrawals AS (
    SELECT
        evt_block_number, evt_block_time, evt_block_date, evt_tx_hash, evt_index,
        'Withdraw'                                      AS event_type,
        "user"                                          AS "user",
        reserve                                         AS asset,
        amount                                          AS amount_raw_num
    FROM aave_v2_ethereum.lendingpool_evt_withdraw
    WHERE evt_block_date >= DATE '2021-01-01'
      AND evt_block_date <= DATE '2026-02-28'
      AND evt_block_number >= {{start_block}}
      AND evt_block_number <= {{end_block}}
),
all_events AS (
    SELECT * FROM deposits
    UNION ALL SELECT * FROM borrows
    UNION ALL SELECT * FROM repays
    UNION ALL SELECT * FROM withdrawals
)

SELECT
    1                                                           AS chain_id,
    e.evt_block_number                                          AS block_number,
    e.evt_block_time                                            AS block_timestamp,
    lower(concat('0x', to_hex(e.evt_tx_hash)))                  AS tx_hash,
    CAST(e.evt_index AS INTEGER)                                AS log_index,
    'aave_v2'                                                   AS protocol,
    e.event_type,
    lower(concat('0x', to_hex(e."user")))                       AS "user",
    NULL                                                        AS collateral_asset,
    lower(concat('0x', to_hex(e.asset)))                        AS debt_asset,
    CAST(e.amount_raw_num AS VARCHAR)                           AS amount_raw,
    TRY(CAST(e.amount_raw_num AS DOUBLE)
        / POWER(10.0, p.decimals)
        * p.price)                                              AS amount_usd,
    NULL                                                        AS liquidator,
    NULL                                                        AS collateral_seized_raw,
    CAST(NULL AS DOUBLE)                                        AS collateral_seized_usd

FROM all_events e

LEFT JOIN prices.usd p
    ON  p.contract_address = e.asset
    AND p.blockchain = 'ethereum'
    AND p.minute = date_trunc('minute', e.evt_block_time)

ORDER BY e.evt_block_number, e.evt_index
