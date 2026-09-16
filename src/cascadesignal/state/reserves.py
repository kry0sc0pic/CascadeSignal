"""Aave v2 Ethereum reserve registry (CAS-13/CAS-47).

Maps all 37 reserve addresses observed in the ingested Aave v2 core-event /
liquidation data (`data/raw/aave_v2/`) to token symbol, decimals, and risk
parameters (LTV, liquidation threshold, liquidation bonus).

`ONCHAIN_RESERVE_CONFIG` below is NOT memorized/guessed -- it's the literal
output of `scripts/onchain/fetch_aave_v2_reserve_config.py` run twice
independently against the live `AaveProtocolDataProvider` contract
(0x057835Ad21a177dbdd3090bB1CAE03EaCF78Fc6d, verified against
github.com/bgd-labs/aave-address-book) via a public JSON-RPC endpoint, pinned
to block 25479510 (2026-07-07). Both runs agreed on every value. Symbol and
decimals for all 37 reserves are real on-chain values, not guesses.

IMPORTANT -- read before using this for historical HF reconstruction:
Aave v2 Ethereum is now fully frozen (every one of the 37 reserves has
`isFrozen=true` on-chain) as part of its deprecation in favor of v3, and most
reserves were de-risked toward a near-zero liquidation threshold well before
the freeze (e.g. LINK/UNI/MKR/CRV/etc. show liquidation_threshold <= 10% or
even <0.1% *today*, despite having been meaningful, actively-used collateral
during the 2021-2022 study period). This snapshot is CURRENT-STATE ONLY.
Using it as-is to recompute health factors for the golden cascade episodes
(China 2021-05, Terra 2022-05/06) would systematically understate historical
collateral value -- a data artifact that could produce a spuriously low HF
(and a false T2 "pass") that has nothing to do with real insolvency risk.
Closing this properly needs a governance-history pull (per-block reserve
config, likely via `ReserveDataUpdated` logs or Aave governance proposal
history) -- not implemented here. `reserve_table()` marks every row
`params_verified=True` (verified against real on-chain state) but that only
means "not fabricated," not "correct for every historical block" -- see
`historical_reliable` below.
"""

from __future__ import annotations

import pandas as pd

SNAPSHOT_BLOCK = 25_479_510
SNAPSHOT_DATE = "2026-07-07"

# address (lowercase) -> (symbol, decimals, ltv, liquidation_threshold, liquidation_bonus)
# Source: scripts/onchain/fetch_aave_v2_reserve_config.py @ block 25479510.
# ltv/liquidation_threshold are fractions (e.g. 0.86 = 86%); liquidation_bonus
# is the bonus fraction over par (e.g. 0.05 = 5% bonus), 0.0 where the
# on-chain liquidationBonus is unset (paired with liquidation_threshold=0 --
# these reserves currently can't serve as collateral at all).
ONCHAIN_RESERVE_CONFIG: dict[str, tuple[str, int, float, float, float]] = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6, 0.0, 0.0, 0.0),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8, 0.72, 0.82, 0.05),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18, 0.825, 0.86, 0.05),
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": ("YFI", 18, 0.0, 0.0005, 0.1),
    "0xe41d2489571d322189246dafa5ebde1f4699f498": ("ZRX", 18, 0.0, 0.0001, 0.1),
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": ("UNI", 18, 0.0, 0.0001, 0.09),
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": ("AAVE", 18, 0.66, 0.73, 0.075),
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": ("BAT", 18, 0.0, 0.0005, 0.1),
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": ("BUSD", 18, 0.0, 0.0, 0.0),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18, 0.63, 0.77, 0.04),
    "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": ("ENJ", 18, 0.0, 0.0005, 0.1),
    "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": ("KNC", 18, 0.0, 0.0005, 0.1),
    "0x514910771af9ca656af840dff83e8264ecf986ca": ("LINK", 18, 0.0, 0.65, 0.07),
    "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": ("MANA", 18, 0.0, 0.0005, 0.1),
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": ("MKR", 18, 0.0, 0.1, 0.075),
    "0x408e41876cccdc0f92210600ef50372656052a38": ("REN", 18, 0.0, 0.0005, 0.1),
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": ("SNX", 18, 0.0, 0.0005, 0.075),
    "0x57ab1ec28d129707052df4df418d58a2d46d5f51": ("sUSD", 18, 0.0, 0.0, 0.0),
    "0x0000000000085d4780b73119b644ae5ecd22b376": ("TUSD", 18, 0.0, 0.65, 0.1),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6, 0.75, 0.875, 0.045),
    "0xd533a949740bb3306d119cc777fa900ba034cd52": ("CRV", 18, 0.0, 0.0001, 0.08),
    "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": ("GUSD", 2, 0.0, 0.0, 0.0),
    "0xba100000625a3754423978a60c9317c58a424e3d": ("BAL", 18, 0.0, 0.0005, 0.08),
    "0x8798249c2e607446efb7ad49ec89dd1865ff4272": ("xSUSHI", 18, 0.0, 0.0005, 0.1),
    "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": ("renFIL", 18, 0.0, 0.0, 0.0),
    "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": ("RAI", 18, 0.0, 0.0, 0.0),
    "0xd46ba6d942050d489dbd938a2c909a5d5039a161": ("AMPL", 9, 0.0, 0.0, 0.0),
    "0x8e870d67f660d95d5be530380d0ec0bd388289e1": ("USDP", 18, 0.0, 0.0, 0.0),
    "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": ("DPI", 18, 0.0, 0.0005, 0.1),
    "0x853d955acef822db058eb8505911ed77f175b99e": ("FRAX", 18, 0.0, 0.0, 0.0),
    "0x956f47f50a910163d8bf957cf5846d573e7f87ca": ("FEI", 18, 0.0, 0.0005, 0.1),
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": ("stETH", 18, 0.72, 0.83, 0.07),
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": ("ENS", 18, 0.0, 0.0005, 0.08),
    "0xa693b19d2931d498c5b318df961919bb4aee87a5": ("UST", 6, 0.0, 0.0, 0.0),
    "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": ("CVX", 18, 0.0, 0.0005, 0.085),
    "0x111111111117dc0aa78b770fa6a738034120c302": ("1INCH", 18, 0.0, 0.0005, 0.085),
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": ("LUSD", 18, 0.0, 0.0, 0.0),
}

# Reserves whose *current* liquidation_threshold is still meaningfully
# nonzero (>1%) -- i.e. plausibly close to their historical value, though
# still not verified block-by-block. Everything else has been de-risked to
# near/exactly zero ahead of the freeze and should NOT be treated as
# representative of 2021-2022 risk parameters.
_STILL_MEANINGFUL_THRESHOLD = 0.01

# reserve address (lowercase) -> aToken address (lowercase). Source: the
# literal output of `scripts/onchain/fetch_aave_v2_atoken_addresses.py`,
# read via `AaveProtocolDataProvider.getReserveTokensAddresses(asset)` at
# block 25566609 (2026-07-19), same live-contract pattern
# ONCHAIN_RESERVE_CONFIG above uses. Needed by
# `scripts/onchain/fix_gateway_withdraw.py` (CAS-28) to identify which ERC20
# `Transfer` events are aToken transfers (as opposed to unrelated tokens
# also sent to a gateway/adapter contract in the same tx) when correlating a
# gateway-routed `Withdraw` back to its real position holder.
ATOKEN_ADDRESS_BY_RESERVE: dict[str, str] = {
    "0x0000000000085d4780b73119b644ae5ecd22b376": "0x101cc05f4a51c0319f570d5e146a8c625198e636",
    "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": "0xc9bc48c72154ef3e5425641a3c747242112a46af",
    "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": "0xd37ee7e4f452c6638c96536e68090de8cbcdb583",
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": "0x5165d24277cd063f5ac44efd447b27025e888f37",
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": "0x05ec93c0365baaeabf7aeffb0972ea7ecdd39cf1",
    "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": "0xa685a61171bb30d4072b338c80cb7b2c865c873e",
    "0x111111111117dc0aa78b770fa6a738034120c302": "0xb29130cbcc3f791f077eade0266168e808e5151e",
    "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": "0x6f634c6135d2ebd550000ac92f494f9cb8183dae",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "0xb9d7cb55f463405cdfbe4e90a6d2df01c2b92bf1",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "0x9ff58f4ffb29fa2266ab25e75e2a8b3503311656",
    "0x408e41876cccdc0f92210600ef50372656052a38": "0xcc12abe4ff81c9378d670de1b57f8e0dd228d77a",
    "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": "0x952749e07d7157bb9644a894dfaf3bad5ef6d918",
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": "0xa361718326c15715591c299427c62086f69923d9",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "0xa06bc25b5805d5f8d82847d191cb4af5a3e873e0",
    "0x57ab1ec28d129707052df4df418d58a2d46d5f51": "0x6c5024cd4f8a59110119c56f8933403a539555eb",
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": "0xce1871f791548600cb59efbeffc9c38719142079",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "0x028171bca77440897b824ca71d1c56cac55b68a3",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "0xffc97d72e13e01096502cb8eb52dee56f74dad7b",
    "0x853d955acef822db058eb8505911ed77f175b99e": "0xd4937682df3c8aef4fe912a96a74121c0829e664",
    "0x8798249c2e607446efb7ad49ec89dd1865ff4272": "0xf256cc7847e919fac9b808cc216cac87ccf2f47a",
    "0x8e870d67f660d95d5be530380d0ec0bd388289e1": "0x2e8f4bdbe3d47d7d7de490437aea9915d930f1a3",
    "0x956f47f50a910163d8bf957cf5846d573e7f87ca": "0x683923db55fead99a79fa01a27eec3cb19679cc3",
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": "0xc713e5e149d5d0715dcd1c156a020976e7e56b88",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "0xbcca60bb61934080951369a648fb03df4f96263c",
    "0xa693b19d2931d498c5b318df961919bb4aee87a5": "0xc2e2152647f4c26028482efaf64b2aa28779efc4",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "0x1982b2f5814301d4e9a8b0201555376e62f82428",
    "0xba100000625a3754423978a60c9317c58a424e3d": "0x272f97b7a56a387ae942350bbc7df5700f8a4576",
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": "0x35f6b052c598d933d69a4eec4d04c73a191fe6c2",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "0x030ba81f1c18d280636f32af80b9aad02cf0854e",
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": "0x9a14e23a58edf4efdcb360f68cd1b95ce2081a2f",
    "0xd46ba6d942050d489dbd938a2c909a5d5039a161": "0x1e6bb68acec8fefbd87d192be09bb274170a0548",
    "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": "0x514cd6756ccbe28772d4cb81bc3156ba9d1744aa",
    "0xd533a949740bb3306d119cc777fa900ba034cd52": "0x8dae6cb04688c62d939ed9b68d32bc62e49970b1",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "0x3ed3b47dd13ec9a98b44e6204a523e766b225811",
    "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": "0x39c6b3e42d6a679d7d776778fe880bc9487c2eda",
    "0xe41d2489571d322189246dafa5ebde1f4699f498": "0xdf7ff54aacacbff42dfe29dd6144a69b629f8c9e",
    "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": "0xac6df26a590f08dcc95d5a4705ae8abbc88509ef",
}

# reserve address (lowercase) -> stableDebtToken / variableDebtToken address
# (lowercase). Source: the same `getReserveTokensAddresses(asset)` call
# ATOKEN_ADDRESS_BY_RESERVE uses (that call actually returns all three
# addresses at once; the earlier pull only kept the aToken leg), re-run via
# the extended `scripts/onchain/fetch_aave_v2_atoken_addresses.py` at block
# 25585463 (2026-07-22) -- the re-pulled aToken addresses were byte-for-byte
# identical to ATOKEN_ADDRESS_BY_RESERVE above, cross-validating the call.
# Needed (CAS-28 H3) to pull each debt token's own `Mint`/`Burn` events for
# the exact token-level ledger -- see `state/engine.py`'s H3 section.
STABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE: dict[str, str] = {
    "0x0000000000085d4780b73119b644ae5ecd22b376": "0x7f38d60d94652072b2c44a18c0e14a481ec3c0dd",
    "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": "0x9c72b8476c33ae214ee3e8c20f0bc28496a62032",
    "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": "0xf8ac64ec6ff8e0028b37eb89772d21865321bce0",
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": "0xca823f78c2dd38993284bb42ba9b14152082f7bd",
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": "0x277f8676facf4daa5a6ea38ba511b7f65aa02f9f",
    "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": "0xd86c74ea2224f4b8591560652b50035e4e5c0a3b",
    "0x111111111117dc0aa78b770fa6a738034120c302": "0x1278d6ed804d59d2d18a5aa5638dfd591a79af0a",
    "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": "0xa3953f07f389d719f99fc378ebdb9276177d8a6e",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "0xd939f7430dc8d5a427f156de1012a56c18acb6aa",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "0x51b039b9afe64b78758f8ef091211b5387ea717c",
    "0x408e41876cccdc0f92210600ef50372656052a38": "0x3356ec1efa75d9d150da1ec7d944d9edf73703b7",
    "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": "0xb01eb1ce1da06179136d561766fc2d609c5f55eb",
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": "0x4a7a63909a72d268b1d8a93a9395d098688e0e5c",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "0xfb4aec4cc858f2539ebd3d37f2a43eae5b15b98a",
    "0x57ab1ec28d129707052df4df418d58a2d46d5f51": "0x30b0f7324fedf89d8eff397275f8983397efe4af",
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": "0x39f010127274b2dbdb770b45e1de54d974974526",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "0x778a13d3eeb110a4f7bb6529f99c000119a08e92",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "0x079d6a3e844bcecf5720478a718edb6575362c5f",
    "0x853d955acef822db058eb8505911ed77f175b99e": "0x3916e3b6c84b161df1b2733dffc9569a1da710c2",
    "0x8798249c2e607446efb7ad49ec89dd1865ff4272": "0x73bfb81d7dba75c904f430ea8bae82db0d41187b",
    "0x8e870d67f660d95d5be530380d0ec0bd388289e1": "0x2387119bc85a74e0bbcbe190d80676cb16f10d4f",
    "0x956f47f50a910163d8bf957cf5846d573e7f87ca": "0xd89cf9e8a858f8b4b31faf793505e112d6c17449",
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": "0xc01c8e4b12a89456a9fd4e4e75b72546bf53f0b5",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "0xe4922afab0bbadd8ab2a88e0c79d884ad337fca6",
    "0xa693b19d2931d498c5b318df961919bb4aee87a5": "0x7fdbfb0412700d94403c42ca3caeeea183f07b26",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "0x66457616dd8489df5d0afd8678f4a260088aaf55",
    "0xba100000625a3754423978a60c9317c58a424e3d": "0xe569d31590307d05da3812964f1edd551d665a0b",
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": "0x8575c8ae70bdb71606a53aea1c6789cb0fbf3166",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "0x4e977830ba4bd783c0bb7f15d3e243f73ff57121",
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": "0x34441ffd1948e49dc7a607882d0c38efd0083815",
    "0xd46ba6d942050d489dbd938a2c909a5d5039a161": "0x18152c9f77dadc737006e9430db913159645fa87",
    "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": "0xcaad05c49e14075077915cb5c820eb3245afb950",
    "0xd533a949740bb3306d119cc777fa900ba034cd52": "0x9288059a74f589c919c7cf1db433251cdfeb874b",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "0xe91d55ab2240594855abd11b3faae801fd4c4687",
    "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": "0x9915dfb872778b2890a117da1f35f335eb06b54f",
    "0xe41d2489571d322189246dafa5ebde1f4699f498": "0x071b4323a24e73a5afeebe34118cd21b8faaf7c3",
    "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": "0x943dcca156b5312aa24c1a08769d67fece4ac14c",
}

VARIABLE_DEBT_TOKEN_ADDRESS_BY_RESERVE: dict[str, str] = {
    "0x0000000000085d4780b73119b644ae5ecd22b376": "0x01c0eb1f8c6f1c1bf74ae028697ce7aa2a8b0e92",
    "0x03ab458634910aad20ef5f1c8ee96f1d6ac54919": "0xb5385132ee8321977fff44b60cde9fe9ab0b4e6b",
    "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": "0x279af5b99540c1a3a7e3cdd326e19659401ef99e",
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": "0x7ebd09022be45ad993baa1cec61166fcc8644d97",
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": "0xfc218a6dfe6901cb34b1a5281fc6f1b8e7e56877",
    "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": "0x0a68976301e46ca6ce7410db28883e309ea0d352",
    "0x111111111117dc0aa78b770fa6a738034120c302": "0xd7896c1b9b4455aff31473908eb15796ad2295da",
    "0x1494ca1f11d487c2bbe4543e90080aeba4ba3c2b": "0x4ddff5885a67e4effec55875a3977d7e60f82ae0",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "0x5bdb050a92cadccfcdcccbfc17204a1c9cc0ab73",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "0x9c39809dec7f95f5e0713634a4d0701329b3b4d2",
    "0x408e41876cccdc0f92210600ef50372656052a38": "0xcd9d82d33bd737de215cdac57fe2f7f04df77fe0",
    "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": "0x4ae5e4409c6dbc84a00f9f89e4ba096603fb7d50",
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": "0xba429f7011c9fa04cdd46a2da24dc0ff0ac6099c",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "0x0b8f12b1788bfde65aa1ca52e3e9f3ba401be16d",
    "0x57ab1ec28d129707052df4df418d58a2d46d5f51": "0xdc6a3ab17299d9c2a412b0e0a4c1f55446ae0817",
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": "0x411066489ab40442d6fc215ad7c64224120d33f2",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "0x6c3c78838c761c6ac7be9f59fe808ea2a6e4379d",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "0xf7dba49d571745d9d7fcb56225b05bea803ebf3c",
    "0x853d955acef822db058eb8505911ed77f175b99e": "0xfe8f19b17ffef0fdbfe2671f248903055afaa8ca",
    "0x8798249c2e607446efb7ad49ec89dd1865ff4272": "0xfafedf95e21184e3d880bd56d4806c4b8d31c69a",
    "0x8e870d67f660d95d5be530380d0ec0bd388289e1": "0xfdb93b3b10936cf81fa59a02a7523b6e2149b2b7",
    "0x956f47f50a910163d8bf957cf5846d573e7f87ca": "0xc2e10006accab7b45d9184fcf5b7ec7763f5baae",
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": "0xba728ead5e496be00dcf66f650b6d7758ecb50f8",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "0x619beb58998ed2278e08620f97007e1116d5d25b",
    "0xa693b19d2931d498c5b318df961919bb4aee87a5": "0xaf32001cf2e66c4c3af4205f6ea77112aa4160fe",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "0xa9deac9f00dc4310c35603fcd9d34d1a750f81db",
    "0xba100000625a3754423978a60c9317c58a424e3d": "0x13210d4fe0d5402bd7ecbc4b5bc5cfca3b71adb0",
    "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f": "0x267eb8cf715455517f9bd5834aeae3cea1ebdbd8",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "0xf63b34710400cad3e044cffdcab00a0f32e33ecf",
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": "0x176808047cc9b7a2c9ae202c593ed42ddd7c0d13",
    "0xd46ba6d942050d489dbd938a2c909a5d5039a161": "0xf013d90e4e4e3baf420dfea60735e75dbd42f1e1",
    "0xd5147bc8e386d91cc5dbe72099dac6c9b99276f5": "0x348e2ebd5e962854871874e444f4122399c02755",
    "0xd533a949740bb3306d119cc777fa900ba034cd52": "0x00ad8ebf64f141f1c81e9f8f792d3d1631c6c684",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "0x531842cebbdd378f8ee36d171d6cc9c4fcf475ec",
    "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": "0x6b05d1c608015ccb8e205a690cb86773a96f39f1",
    "0xe41d2489571d322189246dafa5ebde1f4699f498": "0x85791d117a392097590bded3bd5abb8d5a20491a",
    "0xf629cbd94d3791c9250152bd8dfbdf380e2a3b9c": "0x38995f292a6e31b78203254fe1cdd5ca1010a446",
}


def reserve_table() -> pd.DataFrame:
    """Return the reserve registry as a DataFrame.

    Columns: address, symbol, decimals, ltv, liquidation_threshold,
    liquidation_bonus, params_verified, historical_reliable.

    `params_verified=True` for every row: symbol/decimals/risk-params all
    come from a live on-chain read (see module docstring), not memory.
    `historical_reliable` is a narrower, separate signal: True only for
    reserves whose current liquidation_threshold is still >1% (WETH, WBTC,
    stETH, DAI, USDC, AAVE, LINK, TUSD, MKR) -- callers computing HF for
    blocks from the 2021-2022 golden episodes should treat
    `historical_reliable=False` rows' thresholds as unreliable for that
    period, not as "this asset had no collateral value back then."
    """
    rows = []
    for address, (symbol, decimals, ltv, lt, bonus) in ONCHAIN_RESERVE_CONFIG.items():
        rows.append(
            {
                "address": address,
                "symbol": symbol,
                "decimals": decimals,
                "ltv": ltv,
                "liquidation_threshold": lt,
                "liquidation_bonus": bonus,
                "params_verified": True,
                "historical_reliable": lt > _STILL_MEANINGFUL_THRESHOLD,
            }
        )
    return pd.DataFrame(rows)
