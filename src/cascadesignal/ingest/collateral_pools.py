"""Collateral asset -> canonical DEX pool mapping (CAS-16 subtask).

Maps the Aave v2 collateral assets with the largest real liquidation volume
(see `src/cascadesignal/state/reserves.py` for the full 37-reserve registry)
to the single deepest real Uniswap v2 pair, Uniswap v3 pool, and (where the
asset's primary venue is Curve rather than Uniswap) Curve pool for that asset,
paired against WETH.

Scope: the 9 collateral assets covering >99% of real Aave v2 liquidation
`collateral_seized_usd` volume (computed from `data/raw/aave_v2/*liquidations*`,
2026-07-14): WETH, WBTC, USDC, LINK, stETH, AAVE, CRV, YFI, MANA, DAI.
"Top collateral assets" per the CAS-16 acceptance criteria is scoped to this
list rather than all 37 reserves -- the long tail (BAT, ZRX, ENJ, ...) each
account for well under 1% of seized value and would multiply the DEX query
count for negligible feature-signal gain.

Every pool address below was looked up live via the connected subgraph MCP
(`mcp__subgraph__execute_query_by_ipfs_hash`) on 2026-07-14 -- ordered by
`totalValueLockedUSD` (v3) / `reserveUSD` (v2) descending and taking the
deepest pool per asset -- not guessed or recalled from memory. See
`data/raw/provenance/source_status.md` row #7 for the query transcript
summary.

WETH itself has no "canonical pool" entry (it's the numeraire): its
slippage-to-sell is computed against its own USDC pool entry below (selling
WETH into USDC), i.e. look up `"weth"` and read the pool in the reverse
direction.
"""

from __future__ import annotations

from dataclasses import dataclass

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


@dataclass(frozen=True)
class PoolMapping:
    collateral_symbol: str
    collateral_asset: str  # lowercase address, matches state.reserves keys
    uniswap_v2_pair: str | None  # lowercase pair address, or None if no deep v2 pair
    uniswap_v3_pool: str | None  # lowercase pool address (deepest fee tier)
    uniswap_v3_fee_tier: int | None  # bps *100, e.g. 3000 = 0.3%
    curve_pool: (
        str | None
    )  # lowercase pool address, only set where Curve is primary venue


# collateral_symbol -> PoolMapping. Quote asset is WETH in every case except
# stETH, whose deepest and most contagion-relevant venue is the Curve
# ETH/stETH pool (not a Uniswap pair) -- this is the pool that famously
# de-pegged in 2022, so it's the one that matters for cascade features.
COLLATERAL_POOLS: dict[str, PoolMapping] = {
    "WETH": PoolMapping(
        "WETH",
        WETH,
        uniswap_v2_pair="0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc",  # USDC/WETH v2
        uniswap_v3_pool="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # USDC/WETH 0.05%
        uniswap_v3_fee_tier=500,
        curve_pool=None,
    ),
    "WBTC": PoolMapping(
        "WBTC",
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        uniswap_v2_pair="0xbb2b8038a1640196fbe3e38816f3e67cba72d940",
        uniswap_v3_pool="0xcbcdf9626bc03e24f779434178a73a0b4bad62ed",  # 0.3%
        uniswap_v3_fee_tier=3000,
        curve_pool=None,
    ),
    "USDC": PoolMapping(
        "USDC",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        uniswap_v2_pair="0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc",
        uniswap_v3_pool="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",  # 0.05%
        uniswap_v3_fee_tier=500,
        curve_pool=None,
    ),
    "LINK": PoolMapping(
        "LINK",
        "0x514910771af9ca656af840dff83e8264ecf986ca",
        uniswap_v2_pair="0xa2107fa5b38d9bbd2c461d6edf11b11a50f6b974",
        uniswap_v3_pool="0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8",  # 0.3%
        uniswap_v3_fee_tier=3000,
        curve_pool=None,
    ),
    "stETH": PoolMapping(
        "stETH",
        "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",
        uniswap_v2_pair=None,
        uniswap_v3_pool=None,
        uniswap_v3_fee_tier=None,
        curve_pool="0xdc24316b9ae028f1497c275eb9192a3ea0f67022",  # Curve.fi ETH/stETH
    ),
    "AAVE": PoolMapping(
        "AAVE",
        "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
        uniswap_v2_pair="0xdfc14d2af169b0d36c4eff567ada9b2e0cae044f",
        uniswap_v3_pool="0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb",  # 0.3%
        uniswap_v3_fee_tier=3000,
        curve_pool=None,
    ),
    "CRV": PoolMapping(
        "CRV",
        "0xd533a949740bb3306d119cc777fa900ba034cd52",
        uniswap_v2_pair="0x3da1313ae46132a397d90d95b1424a9a7e3e0fce",
        uniswap_v3_pool="0x4c83a7f819a5c37d64b4c5a2f8238ea082fa1f4e",  # 1%
        uniswap_v3_fee_tier=10000,
        curve_pool=None,
    ),
    "YFI": PoolMapping(
        "YFI",
        "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e",
        uniswap_v2_pair="0x2fdbadf3c4d5a8666bc06645b8358ab803996e28",
        uniswap_v3_pool="0x2e8daf55f212be91d3fa882cceab193a08fddeb2",  # 1%
        uniswap_v3_fee_tier=10000,
        curve_pool=None,
    ),
    "MANA": PoolMapping(
        "MANA",
        "0x0f5d2fb29fb7d3cfee444a200298f468908cc942",
        uniswap_v2_pair="0x11b1f53204d03e5529f09eb3091939e4fd8c9cf3",
        uniswap_v3_pool="0x8661ae7918c0115af9e3691662f605e9c550ddc9",  # 0.3%
        uniswap_v3_fee_tier=3000,
        curve_pool=None,
    ),
    "DAI": PoolMapping(
        "DAI",
        "0x6b175474e89094c44da98b954eedeac495271d0f",
        uniswap_v2_pair="0xa478c2975ab1ea89e8196811f51a7b7ade33eb11",
        uniswap_v3_pool="0xc2e9f25be6257c210d7adf0d4cd6e3e881ba25f8",  # 0.3%
        uniswap_v3_fee_tier=3000,
        curve_pool=None,
    ),
}
