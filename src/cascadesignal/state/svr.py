"""Chainlink SVR (Smart Value Recapture) liquidation-path labeling (CAS-47).

Chainlink SVR is Aave-**v3**-only (confirmed via Aave's governance TEMP CHECK,
governance.aave.com/t/temp-check-aave-chainlink-svr-v1-integration/20378) --
Aave v2 never routes through it. It recaptures oracle-related MEV by routing
price updates through a *second*, dedicated proxy contract per asset: the
standard feed still updates via the public mempool as before, while the SVR
proxy updates via a private Flashbots MEV-Share auction that lets the winning
searcher backrun the fresh price with a liquidation in the *same block*
(docs.chain.link/data-feeds/svr-feeds). That same-block backrun -- not a
shared `tx_hash` -- is the on-chain signature this module looks for.

`AAVE_SVR_FEEDS`'s `svr_proxy` values are the literal output of interacting
with Chainlink's official Price Feed Contract Addresses page
(docs.chain.link/data-feeds/price-feeds/addresses, "Show Only SVR Feeds"
filter, Ethereum mainnet, 2026-07-15) -- not memorized/guessed. Only the
feeds explicitly labeled "Aave SVR" (vs. generic "SVR", which serve other
integrators) are Aave-dedicated per Chainlink's own docs.

IMPORTANT: `svr_proxy` is a stable `EACAggregatorProxy`-style address (what
Chainlink's docs list, and what on-chain consumers call) -- it does **not**
itself emit `AnswerUpdated`. Like every other Chainlink feed in this repo
(see `chainlink_feeds.py`'s docstring on feed migration), the proxy delegates
to an underlying aggregator contract that's the real event source, resolved
via the proxy's `aggregator()` view function (free `eth_call`, confirmed
working against `ethereum-rpc.publicnode.com`'s `"latest"` tag even though
historical `eth_getLogs` on that endpoint is archive-gated -- see
`fetch_svr_feed_events.py`). `svr_aggregator` below is that resolved address,
pulled 2026-07-15; each one's first `AnswerUpdated` lands within ~1 week of
2025-03-01 (verified via Etherscan's `getLogs` API), which lines up with
the pre-registered "Mar 2025" SVR adoption date and is itself decent
evidence these are the right contracts. Like standard feeds, a proxy's
underlying aggregator can be swapped again later -- `svr_aggregator` is
whichever one is current as of the resolution date above, not guaranteed to
be the *only* one across the full study period.

`SVR_REGIME_START` is the pre-registered "Mar 2025" adoption date
(Aave's Snapshot vote passed Jan 2025, phased rollout through mid-2025 per
the governance forum) -- used as a protocol-wide regime cutoff.
"""

from __future__ import annotations

import pandas as pd

SVR_REGIME_START = pd.Timestamp("2025-03-01", tz="UTC")

# reserve (mainnet ERC20) -> its dedicated Aave SVR feed.
AAVE_SVR_FEEDS: dict[str, dict[str, str]] = {
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": {
        "symbol": "AAVE",
        "svr_proxy": "0xf02c1e2a3b77c1cacc72f72b44f7d0a4c62e4a85",
        "svr_aggregator": "0xcd07b31d85756098334eddc92de755deae8fe62f",
    },
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {
        "symbol": "WBTC",  # via BTC/USD Aave SVR
        "svr_proxy": "0xb41e773f507f7a7ea890b1afb7d2b660c30c8b0a",
        "svr_aggregator": "0xdc715c751f1cc129a6b47fedc87d9918a4580502",
    },
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
        "symbol": "WETH",  # via ETH/USD Aave SVR
        "svr_proxy": "0x5424384b256154046e9667ddfaaa5e550145215e",
        "svr_aggregator": "0x7c7fdfca295a787ded12bb5c1a49a8d2cc20e3f8",
    },
    "0x514910771af9ca656af840dff83e8264ecf986ca": {
        "symbol": "LINK",
        "svr_proxy": "0xc7e9b623ed51f033b32ae7f1282b1ad62c28c183",
        "svr_aggregator": "0x64c67984a458513c6bab23a815916b1b1075cf3a",
    },
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
        "symbol": "USDC",
        "svr_proxy": "0xea674bbc33ae708bc9eb4ba348b04e4eb55b496b",
        "svr_aggregator": "0xe13fafe4fb769e0f4a1cb69d35d21ef99188eff7",
    },
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {
        "symbol": "USDT",
        "svr_proxy": "0x62c2ab773b7324ad9e030d777989b3b5d5c54c0a",
        "svr_aggregator": "0x9df238be059572d7211f1a1a5fee609f979aad2d",
    },
}


def label_svr_routed(liquidations: pd.DataFrame, svr_events: pd.DataFrame) -> pd.Series:
    """Boolean per liquidation row: was it (very likely) SVR-routed?

    A liquidation is labeled SVR-routed iff:
      1. `block_timestamp >= SVR_REGIME_START` (Aave's SVR pilot hadn't
         launched before this, so nothing earlier can be SVR-routed), AND
      2. an `AnswerUpdated` event from the SVR proxy for its collateral OR
         debt asset landed in the *same block* as the liquidation (the
         same-block-backrun signature described in the module docstring).

    Args:
        liquidations: canonical-schema event rows (needs `block_number`,
            `block_timestamp`, `collateral_asset`, `debt_asset`).
        svr_events: canonical-schema `AnswerUpdated` rows from the SVR
            proxies (needs `block_number`, `collateral_asset` -- the
            underlying reserve the feed prices, per
            `fetch_svr_feed_events.py`'s output).

    Returns:
        A boolean Series aligned to `liquidations.index`.
    """
    if svr_events.empty:
        return pd.Series(False, index=liquidations.index)

    svr_block_assets: set[tuple[int, str]] = set(
        zip(
            svr_events["block_number"].astype("int64"),
            svr_events["collateral_asset"].str.lower(),
        )
    )

    def _is_svr_routed(row: pd.Series) -> bool:
        if row["block_timestamp"] < SVR_REGIME_START:
            return False
        block = int(row["block_number"])
        for asset in (row.get("collateral_asset"), row.get("debt_asset")):
            if isinstance(asset, str) and (block, asset.lower()) in svr_block_assets:
                return True
        return False

    return liquidations.apply(_is_svr_routed, axis=1)
