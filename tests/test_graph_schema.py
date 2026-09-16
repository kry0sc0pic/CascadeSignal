"""Unit tests for the contagion-graph schema (CAS-31)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from cascadesignal.graph.schema import (
    COMPOSABILITY_WRAPS,
    NO_DEBT_BAND,
    hf_band_series,
)


def test_hf_band_series_boundaries_and_no_debt():
    hf = pd.Series([0.5, 1.0, 1.1, 1.25, 1.5, 2.0, 2.5, np.nan])
    bands = hf_band_series(hf)
    assert list(bands) == [
        "<=1.00",
        "<=1.00",
        "1.00-1.10",
        "1.10-1.25",
        "1.25-1.50",
        "1.50-2.00",
        ">2.00",
        NO_DEBT_BAND,
    ]


def test_composability_wraps_maps_steth_to_weth():
    assert COMPOSABILITY_WRAPS["stETH"] == "WETH"
