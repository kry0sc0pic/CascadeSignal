"""Aave v2 reserve -> Chainlink aggregator feed map.

Not memorized/guessed -- this is the literal output of
`scripts/onchain/map_chainlink_reserves.py`, which combines two independent
on-chain checks:

1. `scripts/onchain/fetch_aave_v2_price_oracle_sources.py` calls Aave v2's
 real `AaveOracle.getSourceOfAsset(reserve)` for all 37 reserves. That
 turned out to be a dead end for historical pricing: every reserve
 currently routes through a modern "Capped X/USD/ETH" wrapper contract
 (Aave's price-capping upgrade), and **none** of those 37 wrapper
 addresses appear anywhere in the 423,848-row golden-episode
 `AnswerUpdated` pull -- they didn't exist yet during China'21/Terra'22,
 same class of "current on-chain state isn't historical state" problem as
 `reserves.py`'s de-risked liquidation thresholds.
2. `scripts/onchain/describe_chainlink_aggregators.py` instead reads
 `description`/`decimals` for the 392 raw aggregator addresses that
 *did* emit real events in the pulled data, and reserves are matched to
 feeds by symbol (e.g. WETH -> "ETH / USD").

Each entry's `aggregators` list can hold 2-4 addresses: Chainlink migrates a
feed to a new proxy address periodically, so one logical feed's full
2021-2026 history is often split across successive contracts sharing the
same `description`. `ChainlinkPriceOracle` merges all of them into one
combined nearest-prior series per reserve.

`quote` is `"USD"` (raw event value, once divided by `10**decimals`, is
already a USD price) or `"ETH"` (divide by `10**decimals` to get an
ETH-denominated price, then multiply by the ETH/USD series in
`ETH_USD_AGGREGATORS` to get USD). `decimals` is NOT uniformly 8 -- most USD
feeds pulled are 8, but AMPL/USD is 18; always use the value stored here
rather than assuming.

`UNCOVERED_RESERVES` lists 5 reserves with zero matching feed rows anywhere
in the pull (GUSD, xSUSHI, stETH, ENS, CVX) -- `ChainlinkPriceOracle.price_at`
returns `None` for these, same as any other reserve missing from
`coverage`, so callers (`health_factor.compute_health_factor`) already
handle it via `fully_covered=False`. WBTC has no direct `WBTC/USD` or
`WBTC/BTC` feed in the pull either, so it's mapped to the raw `BTC/USD` feed
as a documented approximation (ignores the WBTC/BTC peg deviation). (2026-07-15): "zero rows in the pull" is not the same
claim as "no feed exists." Verified via Chainlink's public feed listing
(data.chain.link/ethereum/mainnet/crypto-usd/steth-usd) that a real,
currently-live STETH/USD feed exists on Ethereum mainnet (proxy
`0xCfE54B5cD566aB89272946F602D76Ea879CAb4a8`) -- it's just genuinely absent
from the 423,904-row golden-episode pull (checked directly: zero matching
rows), i.e. a pull-coverage gap, not a missing feed. Fixing it needs a
targeted historical pull for that one address, which hit the same blocker as
`fetch_svr_feed_events.py` (public RPC `eth_getLogs` is archive-gated /
key-gated / range-capped on every free endpoint tried) -- not attempted this
session. GUSD/xSUSHI/ENS/CVX were not independently re-checked; the original
"zero rows in the pull" finding for them still stands, but whether that's a
true no-feed-exists case or the same pull-gap pattern as stETH is unconfirmed.

`RESERVE_CHAINLINK_ETH_FEEDS` (2026-07-21): Aave v2's
`calculateUserAccountData` never actually reads the asset/USD feeds above
it prices every reserve via its **asset/ETH** aggregator (WETH is the
protocol's numeraire, so WETH's own price is exactly 1.0 by construction,
not a feed read) and sums in ETH. `RESERVE_CHAINLINK_FEEDS` reconstructs an
*implied* asset/ETH cross-rate by combining two independent asset/USD feeds
(ours and the ETH/USD feed) -- each with its own lag/noise, which compounds.
Pricing natively via each reserve's own asset/ETH feed removes that
compounding error. All 37 reserves now match (+ WETH's identity, handled by
`state.prices.EthNumeraire`, not listed here) and `UNCOVERED_ETH_RESERVES` is
empty: the original 30 from H1/H2, stETH/CVX added by Lever 11 (real,
standard aggregators the original description-sweep simply missed), GUSD/
ENS/LUSD added by Lever 11b (their real Aave oracle source is a custom
adapter with no event history of its own, but the formula is an exact,
replicable Chainlink cross-rate -- see those entries' comments and
`scripts/onchain/derive_custom_adapter_eth_feeds.py`), and xSUSHI added by
Lever 11c (a structurally harder case -- its adapter also reads live
`balanceOf`/`totalSupply` contract state, the SushiBar share price, exactly
reconstructed from `Transfer` event history instead -- see
`scripts/onchain/derive_xsushi_eth_feed.py`).

Each entry's `aggregators` list was built in two passes:

1. (H1) `scripts/onchain/map_chainlink_eth_feeds.py` -- `map_chainlink_reserves.py`
 run against the same already-pulled golden-episode aggregators, matching
 `"<SYMBOL> / ETH"` descriptions instead of `"<SYMBOL> / USD"` (that pull
 was never filtered to specific addresses, so these feeds' events were
 already sitting in `data/raw/chainlink/chain=1/` unused).
2. (H2) `scripts/onchain/discover_chainlink_eth_feed_phases.py` -- H1's
 aggregators only cover phases that happened to emit an event during the 5
 golden-episode windows, so this queries Chainlink's on-chain
 `FeedRegistry` (`0x47Fb2585D2C56Fe188D0E6ec628a38b74fCeeeDf`, verified live
 against a known pair before trusting it) for EVERY historical phase of
 each feed via `getPhaseFeed(base, ETH, phaseId)`, catching migrations
 that landed between episodes. 22 of the 30 gained a previously-unknown
 phase this way (e.g. DAI/ETH has exactly 2 phases on-chain; phase 1 was
 already known, phase 2 had never been pulled). Each addition is
 cross-validated: it's only trusted if at least one of the *other* phases
 for that (base, quote) pair matches an aggregator already in H1's list
 (proving the registry resolved the same feed, not an unrelated one).
 WBTC/BUSD/TUSD/renFIL/USDP/DPI/FRAX/FEI/UST have no `FeedRegistry` entry
 for their (base, ETH) pair (not every feed is registered there) and keep
 their H1-only aggregator list.
"""

from __future__ import annotations

RESERVE_CHAINLINK_FEEDS: dict[str, dict] = {
 "0xdac17f958d2ee523a2206206994597c13d831ec7": {
 "symbol": "USDT",
 "quote_symbol": "USDT",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0xa964273552c1dba201f5f000215f5bd5576e8f93"],
 },
 "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {
 "symbol": "WBTC",
 "quote_symbol": "BTC",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0xae74faa92cb67a95ebcab07358bc222e33a34da7"],
 },
 "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
 "symbol": "WETH",
 "quote_symbol": "ETH",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0x37bc7498f4ff12c19678ee8fe19d713b87f6a9e6"],
 },
 "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": {
 "symbol": "YFI",
 "quote_symbol": "YFI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0xe4b36bbc01ead5a378d4cb088604bfe5ab2cefe3"],
 },
 "0xe41d2489571d322189246dafa5ebde1f4699f498": {
 "symbol": "ZRX",
 "quote_symbol": "ZRX",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x3d47ef9690bd00c77c568b73140dc20f34453766",
 "0x28cc704536e1a6f7e6bd69d4a9d75ac8ebe832f7",
 "0x92877b6ea305830f20d8488ad658718a9c855236",
 "0x8ba1dd555c3addb6275dfd0b7ffd739aed6ab7cb",
 ],
 },
 "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": {
 "symbol": "UNI",
 "quote_symbol": "UNI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x68577f915131087199fe48913d8b416b3984fd38",
 "0xd6947812a94e323608dcd4f84b835a586baf21eb",
 "0x5b0e9ff11aae806067787d380967900551919c0d",
 "0xfceeea703896d85cc80de59ae3b5c0c036a0cf05",
 ],
 },
 "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": {
 "symbol": "AAVE",
 "quote_symbol": "AAVE",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xe3f0dede4b499c07e12475087ab1a084b5f93bc0",
 "0xc8f8d8d5ab0f874f00d74b1ef448151631bc3473",
 ],
 },
 "0x0d8775f648430679a709e98d2b0cb6250d2887ef": {
 "symbol": "BAT",
 "quote_symbol": "BAT",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xd90ca9ac986e453cf51d958071d68b82d17a47e6",
 "0x3fd19c04446b8e015eb70a817a3965daf9d0b408",
 "0x1078f9d2b8777fea7ed8b2a127622eb985c7607d",
 "0xc4efce115a81a9c7d89f8db62b05ac98ac9cab1e",
 ],
 },
 "0x4fabb145d64652a948d72533023f6e7a623c7c53": {
 "symbol": "BUSD",
 "quote_symbol": "BUSD",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0x73dc1b226f7dfac353bdb41a27c4212213e6af07"],
 },
 "0x6b175474e89094c44da98b954eedeac495271d0f": {
 "symbol": "DAI",
 "quote_symbol": "DAI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xdec0a100ead1faa37407f0edc76033426cf90b82",
 "0x4588ec4ddcf1d8dbcb5a1273d22f8485885c45a4",
 ],
 },
 "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": {
 "symbol": "ENJ",
 "quote_symbol": "ENJ",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xcbbe4ff0d8add07cce71afc0ccdf3492b8eaa76a",
 "0x26c4aef506789e3aaaa62b6a7fd73b92ea4f68d7",
 ],
 },
 "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": {
 "symbol": "KNC",
 "quote_symbol": "KNC",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xbc60258f775683ea28048030806ad3a80c4a33ae",
 "0xa811ff165b082c0507ce9a5a660fb3d7eeecb88a",
 "0x6ec6b0eb821b51ca47f2a24247ae253ad36cd9db",
 ],
 },
 "0x514910771af9ca656af840dff83e8264ecf986ca": {
 "symbol": "LINK",
 "quote_symbol": "LINK",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xdfd03bfc3465107ce570a0397b247f546a42d0fa",
 "0xbd11bc57fc140614190cabc1b4c316aba220bae4",
 "0x6bde934047162b87a09b5a3d2f81f3f9173c3237",
 "0xda4c3024236e7055491e7d7b68663e8450ba9bba",
 ],
 },
 "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": {
 "symbol": "MANA",
 "quote_symbol": "MANA",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x7be21aef96e2faeb8dc0d07306814319ca034cad",
 "0xba77636d9eccee5c337ae6f258ebaba1a755f08e",
 ],
 },
 "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": {
 "symbol": "MKR",
 "quote_symbol": "MKR",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x908edc7e1974ecab1ca7164424bc4cac287d83ad",
 "0xffe4b3e69fb463455faa535e7fdbc35bdb3c08fa",
 ],
 },
 "0x408e41876cccdc0f92210600ef50372656052a38": {
 "symbol": "REN",
 "quote_symbol": "REN",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0x3d0bb55d0d2f255d7a0eab8a53a91b3369728e36"],
 },
 "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": {
 "symbol": "SNX",
 "quote_symbol": "SNX",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x06ce8be8729b6ba18dd3416e3c223a5d4db5e755",
 "0xc8db8d5869510bb1fcd3bd7c7624c1b49c652ef8",
 "0x484c56876fd73f412e9d6760933657ca2e76e3a0",
 ],
 },
 "0x57ab1ec28d129707052df4df418d58a2d46d5f51": {
 "symbol": "sUSD",
 "quote_symbol": "SUSD",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x1187272a0e3a603ec4734cec73a0880055ecc593",
 "0xa9cdfde89aaad9155c7c29610fd40a44d2813852",
 "0x71561407e3c26b7c11b97af33cae1192a1ab863e",
 ],
 },
 "0x0000000000085d4780b73119b644ae5ecd22b376": {
 "symbol": "TUSD",
 "quote_symbol": "TUSD",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x98953e9c76573e06ec265bdde1dbb89fa02d56d3",
 "0xb579fd7b705a3a119227def323ec6e62f4ecedf6",
 ],
 },
 "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
 "symbol": "USDC",
 "quote_symbol": "USDC",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x789190466e21a8b78b8027866cbbdc151542a26c",
 "0x3b15a92872435c01c27201aae0968839fb45217d",
 "0x3660827eb8856f4a2eec9713fc6e09f5ad9e405c",
 ],
 },
 "0xd533a949740bb3306d119cc777fa900ba034cd52": {
 "symbol": "CRV",
 "quote_symbol": "CRV",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xb4c4a493ab6356497713a78ffa6c60fb53517c63",
 "0xafed606bd2cab6983fc6f10167c98aac2173d77f",
 "0x5ea974a35c37e42dfb91004cfe2b8aab9210f772",
 ],
 },
 "0xba100000625a3754423978a60c9317c58a424e3d": {
 "symbol": "BAL",
 "quote_symbol": "BAL",
 "quote": "ETH",
 "decimals": 18,
 "aggregators": ["0x2f2c0c1727ce8c429a237ddfbbb87357893fbd5d"],
 },
 "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": {
 "symbol": "renFIL",
 "quote_symbol": "FIL",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0xb4dd24b6b98ea9c18e2196e166f17b15f77b0a07"],
 },
 "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": {
 "symbol": "RAI",
 "quote_symbol": "RAI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x2abfc56aaa39be7a946ec39aac5d452e30614df1",
 "0x3d7fd18d814444023fcfd896d46155aad071a639",
 ],
 },
 "0xd46ba6d942050d489dbd938a2c909a5d5039a161": {
 "symbol": "AMPL",
 "quote_symbol": "AMPL",
 "quote": "USD",
 "decimals": 18,
 "aggregators": ["0xd5090674b4653240cd94ee886484ca808c6e6694"],
 },
 "0x8e870d67f660d95d5be530380d0ec0bd388289e1": {
 "symbol": "USDP",
 "quote_symbol": "PAX",
 "quote": "ETH",
 "decimals": 18,
 "aggregators": ["0x8034d486fc2620f87a9c32a1fb746d20ed9bfb96"],
 },
 "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": {
 "symbol": "DPI",
 "quote_symbol": "DPI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": ["0x5259aa3b262b3390eeef0a6e7b89c08d60c94622"],
 },
 "0x853d955acef822db058eb8505911ed77f175b99e": {
 "symbol": "FRAX",
 "quote_symbol": "FRAX",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x61eb091ea16a32ea5b880d0b3d09d518c340d750",
 "0x80e18a047612794f3904c0a676966b89ef1b5d15",
 ],
 },
 "0x956f47f50a910163d8bf957cf5846d573e7f87ca": {
 "symbol": "FEI",
 "quote_symbol": "FEI",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x1d244648d5a63618751d006886268ae3550d0dfd",
 "0x839f29d1f450e12f98b6633dd50b412b8e6c9c11",
 ],
 },
 "0xa693b19d2931d498c5b318df961919bb4aee87a5": {
 "symbol": "UST",
 "quote_symbol": "UST",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x5edd5f803b831b47715ad3e11a90dd244f0cd0a9",
 "0xe3d534ae4afa3e69a73c377eddc3b30ae892a92c",
 ],
 },
 "0x111111111117dc0aa78b770fa6a738034120c302": {
 "symbol": "1INCH",
 "quote_symbol": "1INCH",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0xd2bdd1e01fd2f8d7d42b209c111c7b32158b5a42",
 "0x2d29d728c48c3f75e221d28d844e2bdfe5656bfc",
 "0xae5de163cfdd58b7f2dada495d377951de9423e8",
 ],
 },
 "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": {
 "symbol": "LUSD",
 "quote_symbol": "LUSD",
 "quote": "USD",
 "decimals": 8,
 "aggregators": [
 "0x27b97a63091d185ce056e1747624b9b92baad056",
 "0x31a53a19ed62dbe521d0f82731bd5e77ca09189e",
 ],
 },
}

# Reserves with zero matching Chainlink feed rows anywhere in the pulled
# golden-episode data -- see module docstring.
UNCOVERED_RESERVES: dict[str, str] = {
 "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": "GUSD",
 "0x8798249c2e607446efb7ad49ec89dd1865ff4272": "xSUSHI",
 "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "stETH",
 "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": "ENS",
 "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": "CVX",
}

# ETH/USD is needed both to price WETH directly and to convert every
# ETH-quoted reserve feed (BAL, USDP) to USD.
#
# (2026-07-23): this list held only phase 5, and only
# golden-episode-window rows at that (35,412 rows, blocks
# [12,414,796, 17,238,713] -- silently missing 2023-06 onward entirely). The
# real on-chain ETH/USD proxy (0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419,
# confirmed live via `description`) has 7 real phases -- the same
# missed-phases-plus-missed-block-range gap H2/Lever 6 fixed for the
# asset/ETH feeds, just never applied to this separate, smaller list.
# Fixed as a prerequisite for replicating GUSD/ENS/LUSD's custom oracle
# adapters (all three read this exact proxy)
# `scripts/onchain/backfill_custom_adapter_underlying_feeds.py` pulled full
# history for all 7 phases (phase 2 confirmed real but zero-event, predates
# Aave v2 entirely). Phase list below via the same `phaseId`/
# `phaseAggregators(n)` walk as `validate_aave_oracle_sources_per_era.py`.
ETH_USD_AGGREGATORS: list[str] = [
 "0xf79d6afbb6da890132f9d7c355e3015f15f3406f", # phase 1
 "0x00c7a37b03690fb9f41b5c5af8131735c7275446", # phase 3
 "0xd3fcd40153e56110e6eeae13e12530e26c9cb4fd", # phase 4
 "0x37bc7498f4ff12c19678ee8fe19d713b87f6a9e6", # phase 5
 "0xe62b71cf983019bff55bc83b48601ce8419650cc", # phase 6
 "0x7d4e742018fb52e48b08be73d041c18b21de6fb5", # phase 7
 # phase 2 (0xb103ede8...) omitted -- confirmed zero AnswerUpdated rows
 # ever (pre-Aave-v2, entirely outside the study period).
]

# WETH is Aave v2's numeraire: `calculateUserAccountData` treats its price as
# exactly 1.0 by construction, never a feed read. `state.prices.EthNumeraire`
# special-cases this address rather than looking it up in
# RESERVE_CHAINLINK_ETH_FEEDS below.
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# Reserve -> native Chainlink asset/ETH feed -- literal output of
# `scripts/onchain/map_chainlink_eth_feeds.py`; see module docstring.
RESERVE_CHAINLINK_ETH_FEEDS: dict[str, dict] = {
 "0xdac17f958d2ee523a2206206994597c13d831ec7": {
 "symbol": "USDT",
 "quote_symbol": "USDT",
 "decimals": 18,
 "aggregators": [
 "0xa874fe207df445ff19e7482c746c4d3fd0cb9ace",
 "0x1058a82c25f55ab8ab0ce717f3e6e164e80f1a0b",
 "0x0b539d864c16398dcc7353521c62186380de6b56",
 "0x7de0d6fce0c128395c488cb4df667cdbfb35d7de",
 ],
 # Early-2021 gap (2026-07-22): the two early phases
 # predate this reserve's currently-known aggregators by ~2-8 months
 # (real liquidations start block 11,471,171 -- weeks after launch).
 # Boundaries are each phase's own real first AnswerUpdated block.
 "aggregator_eras": {
 "0x1058a82c25f55ab8ab0ce717f3e6e164e80f1a0b": (None, 11_363_420),
 "0xa874fe207df445ff19e7482c746c4d3fd0cb9ace": (11_363_420, 12_015_632),
 },
 },
 "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {
 "symbol": "WBTC",
 "quote_symbol": "BTC",
 "decimals": 18,
 "aggregators": [
 "0x0133aa47b6197d0ba090bf2cd96626eb71ffd13c",
 "0xbd72da70007e47aaf1bbd84918675392cf6885f7",
 "0x81076d6ff2620ea9dd7ba9c1015f0d09a3a732e6",
 ],
 "aggregator_eras": {
 "0x0133aa47b6197d0ba090bf2cd96626eb71ffd13c": (None, 11_366_343),
 "0xbd72da70007e47aaf1bbd84918675392cf6885f7": (11_366_343, 12_069_614),
 },
 # NOTE: Aave's real AaveOracle.AssetSourceUpdated
 # history shows this whole proxy (0xdeb288f7...) was replaced as
 # WBTC's top-level source at block 17_400_308 (2023-06-03) by a
 # "wBTC/BTC/ETH" live-computation wrapper (0xfd858c8b...) that emits
 # no AnswerUpdated events at all (confirmed via direct getLogs, 0
 # rows) -- not pullable without adapter replication, same failure
 # mode as the 2024/2025 stablecoin migration. A `coverage_end:
 # 17_400_308` clip was tried (technically correct -- Aave
 # demonstrably stopped reading this proxy) but MEASURED WORSE:
 # 8.38% -> 9.26% in isolation, because the clip's only fallback
 # (BlendedPriceOracle's USD cross-rate) lost coverage outright for
 # most of the affected population (952 positions, 838 previously
 # *correctly matching*) rather than just getting noisier, and even
 # the still-measurable remainder was net negative (broken > fixed).
 # Reverted -- see the Lever 11 follow-up section in
 # CAS28_mismatch_next_steps.md. The `coverage_end` mechanism itself
 # (prices.py's EthNumeraire) is kept, unit-tested, and unused by any
 # real entry -- ready if a future case has a usable fallback.
 },
 "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": {
 "symbol": "YFI",
 "quote_symbol": "YFI",
 "decimals": 18,
 "aggregators": [
 "0x4a03707a1bfefc2836a69b1a6a6bd752270041a9",
 "0x8fee58b0f1a9d47a4eaa2ecd6b020f6f1be31d35",
 "0xaa5aa80e416f9d32ffe6c390e24410d02d203f70",
 ],
 "aggregator_eras": {
 "0x4a03707a1bfefc2836a69b1a6a6bd752270041a9": (None, 12_153_648),
 },
 },
 "0xe41d2489571d322189246dafa5ebde1f4699f498": {
 "symbol": "ZRX",
 "quote_symbol": "ZRX",
 "decimals": 18,
 "aggregators": [
 "0xa0f9d94f060836756ffc84db4c78d097ca8c23e8",
 "0xe03b49682965a1eb5230d41f96e10896dc563f0d",
 "0x3547473da7deb396acf07d57340a8ef931d7414e",
 "0x6b39588d2fc7990cc81544dfd4674c909e9efeea",
 ],
 "aggregator_eras": {
 "0xe03b49682965a1eb5230d41f96e10896dc563f0d": (None, 11_363_419),
 "0xa0f9d94f060836756ffc84db4c78d097ca8c23e8": (11_363_419, 12_015_600),
 },
 },
 "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": {
 "symbol": "UNI",
 "quote_symbol": "UNI",
 "decimals": 18,
 "aggregators": [
 "0x5977d45ba0a1ffc3740506d07f5693bbc45df3c7",
 "0x08b383db68ee48cef76d3a48c4e0de9b558704f5",
 "0xc1d1d0da0fcf78157ea25d0e64e3be679813a1f7",
 ],
 "aggregator_eras": {
 "0x5977d45ba0a1ffc3740506d07f5693bbc45df3c7": (None, 12_154_018),
 },
 },
 "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": {
 "symbol": "AAVE",
 "quote_symbol": "AAVE",
 "decimals": 18,
 "aggregators": [
 "0x347b3886bdc7242ae7f5f00398e801c8bfa8f52c",
 "0x42f3b59f72772eb5794b04d2d85afac0d30a5683",
 "0xdf0da6b3d19e4427852f2112d0a963d8a158e9c7",
 ],
 },
 "0x0d8775f648430679a709e98d2b0cb6250d2887ef": {
 "symbol": "BAT",
 "quote_symbol": "BAT",
 "decimals": 18,
 "aggregators": [
 "0x9b4e2579895efa2b4765063310dc4109a7641129",
 "0x3146392934da3ae09447cd7fe4061d8aa96b50ae",
 "0x0b83b36bdb49e5010c2aee53b3cbd131fd24261c",
 "0x821f24daca9ad4910c1ede316d2713fc923da698",
 ],
 "aggregator_eras": {
 "0x9b4e2579895efa2b4765063310dc4109a7641129": (None, 11_364_435),
 "0x3146392934da3ae09447cd7fe4061d8aa96b50ae": (11_364_435, 12_070_728),
 },
 },
 "0x4fabb145d64652a948d72533023f6e7a623c7c53": {
 "symbol": "BUSD",
 "quote_symbol": "BUSD",
 "decimals": 18,
 "aggregators": ["0x5952c7f1ab270d22d677762be3dad0ba9e5cd23d"],
 },
 "0x6b175474e89094c44da98b954eedeac495271d0f": {
 "symbol": "DAI",
 "quote_symbol": "DAI",
 "decimals": 18,
 "aggregators": [
 "0x037e8f2125bf532f3e228991e051c8a7253b642c",
 "0xd866a07dea5ee3c093e21d33660b5579c21f140b",
 "0x158228e08c52f3e2211ccbc8ec275fa93f6033fc",
 "0x84e32ab7a70be2be619ebcb06d2c725f8b7fb839",
 ],
 "aggregator_eras": {
 "0xd866a07dea5ee3c093e21d33660b5579c21f140b": (None, 11_363_415),
 "0x037e8f2125bf532f3e228991e051c8a7253b642c": (11_363_415, 12_016_380),
 },
 },
 "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": {
 "symbol": "ENJ",
 "quote_symbol": "ENJ",
 "decimals": 18,
 "aggregators": [
 "0x3e0de81e212eb9eccd23bb3a9b0e1fac6c8170fc",
 "0x20aff4833e5d261bb34bc3980d88ad17a3fe90dc",
 "0x63f71cb5c29c33656dcd5dca144e12532a361bef",
 "0xdbd66e8d31f506e0cc8cb2f346de4c7fa3f655de",
 ],
 "aggregator_eras": {
 "0x3e0de81e212eb9eccd23bb3a9b0e1fac6c8170fc": (None, 11_365_296),
 "0x20aff4833e5d261bb34bc3980d88ad17a3fe90dc": (11_365_296, 12_069_755),
 },
 },
 "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": {
 "symbol": "KNC",
 "quote_symbol": "KNC",
 "decimals": 18,
 "aggregators": [
 "0x714ff6b6fc99c2ee37bac73ab41c8e4ae30508a5",
 "0xb3b1882c0a7eb5097f12547bcd20dc6fae7ac8a6",
 ],
 },
 "0x514910771af9ca656af840dff83e8264ecf986ca": {
 "symbol": "LINK",
 "quote_symbol": "LINK",
 "decimals": 18,
 "aggregators": [
 "0xecfa53a8bda4f0c4dd39c55cc8def3757acfdd07",
 "0x7e6c635d6a53b5033d1b0cee84eccea9096859e4",
 "0x3357974b41466c9adb453dc9d8a5a07278887174",
 "0xbba12740de905707251525477bad74985dec46d2",
 ],
 "aggregator_eras": {
 "0x7e6c635d6a53b5033d1b0cee84eccea9096859e4": (None, 11_363_423),
 "0xecfa53a8bda4f0c4dd39c55cc8def3757acfdd07": (11_363_423, 12_016_690),
 },
 },
 "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": {
 "symbol": "MANA",
 "quote_symbol": "MANA",
 "decimals": 18,
 "aggregators": [
 "0xc89c4ed8f52bb17314022f6c0dcb26210c905c97",
 "0x3162c2de0c254b97d869a070929b518b5b9b56b3",
 "0x46b77070f9256523c2f31c333b72c3e102f8a8a7",
 "0x90902b68e5049d56954bdfe4c3b235a805c8f153",
 ],
 "aggregator_eras": {
 "0x3162c2de0c254b97d869a070929b518b5b9b56b3": (None, 11_363_418),
 "0xc89c4ed8f52bb17314022f6c0dcb26210c905c97": (11_363_418, 12_070_743),
 },
 },
 "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": {
 "symbol": "MKR",
 "quote_symbol": "MKR",
 "decimals": 18,
 "aggregators": [
 "0xda3d675d50ff6c555973c4f0424964e1f6a4e7d3",
 "0x204a6fe11de66aa463879f47f3533dd87d47020d",
 "0x6d68a0636246d1de3ebe972ad8bee886b10610ee",
 "0xffc14a3b26708545bccf8e915e2e8348123f5460",
 ],
 "aggregator_eras": {
 "0x204a6fe11de66aa463879f47f3533dd87d47020d": (None, 11_363_420),
 "0xda3d675d50ff6c555973c4f0424964e1f6a4e7d3": (11_363_420, 12_016_554),
 },
 },
 "0x408e41876cccdc0f92210600ef50372656052a38": {
 "symbol": "REN",
 "quote_symbol": "REN",
 "decimals": 18,
 "aggregators": [
 "0xb7b1c8f4095d819bdae25e7a63393cdf21fd02ea",
 "0x1a53bf1bfffb7a2b33e1931d33423c7c94f675ee",
 "0xee34b3ce92a6b635450b9cc6faa976f70a106be7",
 "0xfff8fdc3c2b041c783d90dfefddd842b15a98712",
 ],
 "aggregator_eras": {
 "0xb7b1c8f4095d819bdae25e7a63393cdf21fd02ea": (None, 11_365_408),
 "0x1a53bf1bfffb7a2b33e1931d33423c7c94f675ee": (11_365_408, 12_069_949),
 },
 },
 "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": {
 "symbol": "SNX",
 "quote_symbol": "SNX",
 "decimals": 18,
 "aggregators": [
 "0xe23d1142de4e83c08bb048bcab54d50907390828",
 "0x93d7bbf4cf42bda5bb86eadfad09271040cc10e8",
 "0x84cf90cff80828dd32c69a2f25a09fc1ccbb7fc3",
 "0xbafe3cb0e563e914806a99d547bdbf2cfcf5fdf6",
 ],
 "aggregator_eras": {
 "0xe23d1142de4e83c08bb048bcab54d50907390828": (None, 11_363_604),
 "0x93d7bbf4cf42bda5bb86eadfad09271040cc10e8": (11_363_604, 11_880_406),
 },
 },
 "0x57ab1ec28d129707052df4df418d58a2d46d5f51": {
 "symbol": "sUSD",
 "quote_symbol": "SUSD",
 "decimals": 18,
 "aggregators": [
 "0x6d626ff97f0e89f6f983de425dc5b24a18de26ea",
 "0x060f728deb96875f992c97414eff2b3ef6c58ec7",
 "0x45bb69b89d60878d1e42522342ffca9f2077dd84",
 "0x9929624dab8665ddcaa1acf888d9e770859c5a63",
 ],
 "aggregator_eras": {
 "0x060f728deb96875f992c97414eff2b3ef6c58ec7": (None, 11_363_423),
 "0x6d626ff97f0e89f6f983de425dc5b24a18de26ea": (11_363_423, 12_016_804),
 },
 },
 "0x0000000000085d4780b73119b644ae5ecd22b376": {
 "symbol": "TUSD",
 "quote_symbol": "TUSD",
 "decimals": 18,
 "aggregators": ["0x9534df8f2c9289bbdb0c736e9fef402b20f1828e"],
 },
 "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
 "symbol": "USDC",
 "quote_symbol": "USDC",
 "decimals": 18,
 "aggregators": [
 "0xde54467873c3bcaa76421061036053e371721708",
 "0x00d02526ca08488342ab634de3b2d0050ecc7f60",
 "0x26ae9b951f84e6c28f58a92133c30e312d42e0fe",
 "0xe5bbbdb2bb953371841318e1edfbf727447cef2e",
 ],
 "aggregator_eras": {
 "0x00d02526ca08488342ab634de3b2d0050ecc7f60": (None, 11_363_420),
 "0xde54467873c3bcaa76421061036053e371721708": (11_363_420, 12_016_804),
 },
 },
 "0xd533a949740bb3306d119cc777fa900ba034cd52": {
 "symbol": "CRV",
 "quote_symbol": "CRV",
 "decimals": 18,
 "aggregators": [
 "0xabb243ac767d0b7fcaa0bd5e19a7ba0d339d0b33",
 "0x7f67ca2ce5299a67acd83d52a064c5b8e41ddb80",
 "0x9eb524da226328d8ff69440f0f4bae7dc0bff34c",
 "0xb3478ac41a7acd9a33eb15d7a764b7119e571a3c",
 ],
 "aggregator_eras": {
 "0xabb243ac767d0b7fcaa0bd5e19a7ba0d339d0b33": (None, 11_880_352),
 },
 },
 "0xba100000625a3754423978a60c9317c58a424e3d": {
 "symbol": "BAL",
 "quote_symbol": "BAL",
 "decimals": 18,
 "aggregators": [
 "0x2f6bfbbb5d9cd374574aa552dc6942c01d330c75",
 "0x4f5e9704b1d7cc032553f63471d96fcb63ff2bc3",
 "0x2f2c0c1727ce8c429a237ddfbbb87357893fbd5d",
 "0x84bb206a5b39dbb5ea378074c9cbede397f575dd",
 ],
 "aggregator_eras": {
 "0x2f6bfbbb5d9cd374574aa552dc6942c01d330c75": (None, 11_880_815),
 "0x4f5e9704b1d7cc032553f63471d96fcb63ff2bc3": (11_880_815, 12_016_698),
 },
 },
 "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": {
 "symbol": "renFIL",
 "quote_symbol": "FIL",
 "decimals": 18,
 "aggregators": ["0x9965ad91b4877d29c246445011ce370b3890c5c2"],
 },
 "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": {
 "symbol": "RAI",
 "quote_symbol": "RAI",
 "decimals": 18,
 "aggregators": [
 "0x019699e5b12331cf77df9e39818c2e15c8b06215",
 "0x8d6d808ec1f8803b54e2286bd6992f5601fcf3a8",
 ],
 },
 "0xd46ba6d942050d489dbd938a2c909a5d5039a161": {
 "symbol": "AMPL",
 "quote_symbol": "AMPL",
 "decimals": 18,
 "aggregators": [
 "0xb92ee05e7514ffedddbcd76f5e3064691f6ec79e",
 "0xcc1843b09ba15b829095cbca8d7ab460d669236a",
 ],
 },
 "0x8e870d67f660d95d5be530380d0ec0bd388289e1": {
 "symbol": "USDP",
 "quote_symbol": "PAX",
 "decimals": 18,
 "aggregators": ["0x8034d486fc2620f87a9c32a1fb746d20ed9bfb96"],
 },
 "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": {
 "symbol": "DPI",
 "quote_symbol": "DPI",
 "decimals": 18,
 "aggregators": ["0x989b836d68700da948b5c04a65b3bba39f400ad7"],
 },
 "0x853d955acef822db058eb8505911ed77f175b99e": {
 "symbol": "FRAX",
 "quote_symbol": "FRAX",
 "decimals": 18,
 "aggregators": ["0x56f98706c14df5c290b02cec491bb4c20834bb51"],
 },
 "0x956f47f50a910163d8bf957cf5846d573e7f87ca": {
 "symbol": "FEI",
 "quote_symbol": "FEI",
 "decimals": 18,
 "aggregators": ["0x4be991b4d560bba8308110ed1e0d7f8da60acf6a"],
 },
 "0xa693b19d2931d498c5b318df961919bb4aee87a5": {
 "symbol": "UST",
 "quote_symbol": "UST",
 "decimals": 18,
 "aggregators": ["0xf14278039b6fd72dd3ddbc994ff7e071c81c1890"],
 },
 "0x111111111117dc0aa78b770fa6a738034120c302": {
 "symbol": "1INCH",
 "quote_symbol": "1INCH",
 "decimals": 18,
 "aggregators": [
 "0x2b16c345e0558458e919e3351c62ecad57ca7f36",
 "0xb2f68c82479928669b0487d1daed6ef47b63411e",
 ],
 },
 "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": {
 "symbol": "stETH",
 "quote_symbol": "STETH",
 "decimals": 18,
 "aggregators": ["0x716bb759a5f6facdff91f0afb613133d510e1573"],
 # Aave's real AaveOracle.AssetSourceUpdated history (
 # diagnostic) shows this is the only aggregator Aave actually read for
 # stETH, [14_289_297, 17_546_061) -- a second on-chain phase of this
 # same proxy (0xc9c8efa8...) exists but starts at block 20_203_023,
 # entirely after Aave's own switch-away below, so it's never real
 # coverage and is deliberately not added. See `coverage_end`.
 "coverage_end": 17_546_061,
 },
 "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": {
 "symbol": "CVX",
 "quote_symbol": "CVX",
 "decimals": 18,
 "aggregators": [
 "0xf1f7f7bfcc5e9d6bb8d9617756bec06a5cbe1a49",
 "0xa3c0d69cedff5b173bc496074003dce9c503e861",
 ],
 "aggregator_eras": {
 "0xf1f7f7bfcc5e9d6bb8d9617756bec06a5cbe1a49": (None, 20_436_165),
 "0xa3c0d69cedff5b173bc496074003dce9c503e861": (20_436_165, None),
 },
 },
 # GUSD/ENS/LUSD (2026-07-23): Aave's real oracle source
 # for each of these is a custom adapter contract (confirmed via
 # `getsourcecode`: `GusdPriceProxy`/`ExtendedGusdPriceProxy`,
 # `EnsUsdToEnsEth`, `LSUDUsdToLUSDEth`) that computes its answer fresh on
 # every call from 1-2 ordinary Chainlink feeds -- not an approximation,
 # this IS Aave's real formula, just never pullable as a normal event
 # history since the adapter itself never emits `AnswerUpdated`.
 # `scripts/onchain/derive_custom_adapter_eth_feeds.py` replays each
 # formula exactly (plain-int Solidity-style floor division) over the
 # union of both underlying feeds' real update points -- exact, not an
 # approximation, since the ratio is constant between any two consecutive
 # breakpoints. Spot-verified against live `eth_call`s to the real wrapper
 # contracts at matching historical blocks (exact match everywhere except
 # same-block multi-update cases, where this project's own log_index-level
 # resolution is strictly more precise than a whole-block `eth_call`, and
 # LUSD outside its real usage era -- see that script's docstring).
 "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": {
 "symbol": "GUSD",
 "quote_symbol": "GUSD",
 "decimals": 18,
 # GusdPriceProxy/ExtendedGusdPriceProxy: latestAnswer = (1e8 * 1
 # ether) / ETH_USD.latestAnswer -- GUSD's 1:1 USD peg is
 # hard-coded, no separate GUSD/USD feed read at all. Both of Aave's
 # historical GUSD sources (era1/era2) are byte-for-byte the same
 # formula/constant/address, so one derived series covers both eras.
 "aggregators": ["0x61322e7eb0853efdecdb0570f6d0870a41a689c5"],
 },
 "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": {
 "symbol": "ENS",
 "quote_symbol": "ENS",
 "decimals": 18,
 # EnsUsdToEnsEth: latestAnswer = (ENS_USD.latestAnswer * 1
 # ether) / ETH_USD.latestAnswer. Single source, unchanged since
 # Aave listed ENS (block 14,338,029) -- no coverage_end needed.
 "aggregators": ["0xd4641b75015e6536e8102d98479568d05d7123db"],
 },
 "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": {
 "symbol": "LUSD",
 "quote_symbol": "LUSD",
 "decimals": 18,
 # LSUDUsdToLUSDEth: latestAnswer = (LUSD_USD.latestAnswer * 1
 # ether) / ETH_USD.latestAnswer -- this is LUSD's ORIGINAL
 # 2022-2024 source only. Aave's real AssetSourceUpdated history
 # shows a switch away at block 19_723_911 (part of the 2024 mass
 # stablecoin migration, Lever 11 finding 3) to another dead,
 # event-less wrapper -- coverage_end bounds this derived series to
 # the era it's actually valid for.
 "aggregators": ["0x60c0b047133f696334a2b7f68af0b49d2f3d4f72"],
 "coverage_end": 19_723_911,
 },
 # xSUSHI (2026-07-23): Aave's real oracle source
 # (`XSushiPriceAdapter`, confirmed via `getsourcecode`) is structurally
 # different from GUSD/ENS/LUSD -- it reads live SushiBar contract state,
 # not just Chainlink feeds:
 # exchangeRate = SUSHI.balanceOf(xSUSHI) * 1 ether / xSUSHI.totalSupply
 # latestAnswer = SUSHI_ORACLE.latestAnswer * exchangeRate / 1 ether
 # `SUSHI_ORACLE` is an ordinary 3-phase Chainlink SUSHI/ETH feed.
 # `balanceOf`/`totalSupply` have no event of their own, but xSUSHI is
 # confirmed (via `getsourcecode`) to be SushiSwap's plain OpenZeppelin
 # `SushiBar` ERC20 -- both are exactly reconstructable as running
 # cumulative sums over standard `Transfer` event history (SUSHI
 # transfers to/from the xSUSHI contract; xSUSHI's own mint/burn via
 # `Transfer` with the zero address). `scripts/onchain/derive_xsushi_eth_feed.py`
 # replays the formula exactly over the union of all three inputs'
 # breakpoints -- verified against live `eth_call`s (exact match, modulo
 # the same same-block multi-update precision noted for GUSD/ENS/LUSD).
 "0x8798249c2e607446efb7ad49ec89dd1865ff4272": {
 "symbol": "xSUSHI",
 "quote_symbol": "XSUSHI",
 "decimals": 18,
 "aggregators": ["0x9b26214bec078e68a394aaebfbfff406ce14893f"],
 },
}

# Reserves with zero matching raw asset/ETH feed rows (either genuinely no
# feed, or an untried pull-coverage gap) -- see module docstring. GUSD/ENS/
# LUSD/xSUSHI moved out: every one of Aave v2's 37
# reserves now has ETH-numeraire coverage.
UNCOVERED_ETH_RESERVES: dict[str, str] = {}
