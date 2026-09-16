"""Unit tests for the Hawkes branching-ratio models (CAS-18: CAS-3/E3
univariate + the Mock 1 multivariate-by-collateral-asset slice), plus the
CAS-18 "param recovery verified on synthetic data (T5)" acceptance check.

Synthetic data only (runs in CI without the data lake). Covers the exponential
kernel recursion, the fit/score contract, fold-boundary continuity (no
cold-start reset), integration with the existing walk-forward harness, and MLE
param recovery on data simulated from the model's own discrete-time recursion.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cascadesignal.models.hawkes import (
    HawkesMultivariateBranchingRatio,
    HawkesUnivariateBranchingRatio,
    _decayed_history,
)


def _simulate_univariate_marks(
    mu: float, alpha: float, beta: float, n: int, rng: np.random.Generator
) -> np.ndarray:
    """Simulate marks from the exact discrete recursion the model fits
    (R_i uses history strictly before bar i; lambda_i = mu + alpha*R_i), so
    MLE param recovery on this data is a self-consistent test of the
    estimator, not of a mismatched continuous-time approximation."""
    decay = np.exp(-beta)
    marks = np.empty(n, dtype=float)
    r = 0.0
    for i in range(n):
        lam = mu + alpha * r
        marks[i] = rng.poisson(lam)
        r = decay * (r + marks[i])
    return marks


def _simulate_cov_marks(
    mu: float,
    gamma: float,
    alpha: float,
    beta: float,
    z: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Univariate marks from the covariate-baseline recursion the model fits:
    lambda_i = mu*exp(gamma*z_i) + alpha*R_i, z_i a standardized per-bar
    covariate. Self-consistent generative test of the gamma estimator."""
    decay = np.exp(-beta)
    n = len(z)
    marks = np.empty(n, dtype=float)
    r = 0.0
    for i in range(n):
        lam = mu * np.exp(gamma * z[i]) + alpha * r
        marks[i] = rng.poisson(lam)
        r = decay * (r + marks[i])
    return marks


def _simulate_excitation_marks(
    mu: float,
    delta: float,
    alpha: float,
    beta: float,
    z: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Univariate marks from the excitation-mode recursion: fragility enters
    as an exogenous pressure term, lambda_i = mu + alpha*R_i + delta*relu(z_i).
    Self-consistent generative test of the delta estimator."""
    decay = np.exp(-beta)
    n = len(z)
    marks = np.empty(n, dtype=float)
    r = 0.0
    for i in range(n):
        lam = mu + alpha * r + delta * max(z[i], 0.0)
        marks[i] = rng.poisson(lam)
        r = decay * (r + marks[i])
    return marks


def _simulate_multivariate_marks(
    mu: np.ndarray, alpha: np.ndarray, beta: float, n: int, rng: np.random.Generator
) -> np.ndarray:
    """Multivariate analogue of `_simulate_univariate_marks`: R is a K-vector,
    lambda_i = mu + alpha @ R_i, each channel's mark drawn Poisson(lambda_i)."""
    decay = np.exp(-beta)
    k = len(mu)
    marks = np.empty((n, k), dtype=float)
    r = np.zeros(k)
    for i in range(n):
        lam = mu + alpha @ r
        marks[i] = rng.poisson(np.clip(lam, 0, None))
        r = decay * (r + marks[i])
    return marks


def _bars_from_marks(marks: list[float], start: str = "2021-01-01") -> pd.DataFrame:
    n = len(marks)
    times = pd.date_range(start, periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "end_block": np.arange(n) * 5 + 5,
            "end_time": times,
            "n_liquidations": marks,
        }
    )


def test_decayed_history_hand_computed():
    # R_0 = 0; R_1 = decay*(0+m_0); R_2 = decay*(R_1+m_1)
    marks = np.array([2.0, 3.0, 0.0])
    decay = 0.5
    r = _decayed_history(marks, decay)
    assert r[0] == pytest.approx(0.0)
    assert r[1] == pytest.approx(0.5 * 2.0)
    assert r[2] == pytest.approx(0.5 * (1.0 + 3.0))


def test_decayed_history_continues_from_carried_state():
    marks = np.array([1.0, 1.0])
    decay = 0.5
    cold = _decayed_history(marks, decay, r0=0.0)
    warm = _decayed_history(marks, decay, r0=10.0)
    assert warm[0] == pytest.approx(10.0)
    assert warm[0] > cold[0]
    # the carried state decays into later positions too, not just position 0.
    assert warm[1] > cold[1]


def test_score_before_fit_raises():
    m = HawkesUnivariateBranchingRatio()
    with pytest.raises(RuntimeError):
        m.score(_bars_from_marks([0.0, 1.0]))


def test_fit_recovers_elevated_branching_ratio_on_self_exciting_burst():
    # A quiet series with one big cluster of liquidations (self-excitation),
    # vs. a quiet series with the same total count spread out evenly (no
    # clustering). The fitted process should show higher alpha/(alpha+beta)-
    # driven excitation right after the cluster than the flat baseline.
    rng = np.random.default_rng(0)
    quiet = rng.poisson(0.01, size=2000).astype(float)
    burst = quiet.copy()
    burst[1000:1010] += 15  # sharp cluster mid-series

    m_flat = HawkesUnivariateBranchingRatio().fit(_bars_from_marks(list(quiet)))
    m_burst = HawkesUnivariateBranchingRatio().fit(_bars_from_marks(list(burst)))

    score_flat = m_flat.score(_bars_from_marks(list(quiet)))
    score_burst = m_burst.score(_bars_from_marks(list(burst)))

    assert np.isfinite(score_flat).all() and np.isfinite(score_burst).all()
    assert ((score_flat >= 0) & (score_flat < 1)).all()
    assert ((score_burst >= 0) & (score_burst < 1)).all()
    # n(t) right after the cluster should be well above the flat baseline's
    # typical level -- the self-excited share of intensity spikes.
    assert score_burst[1010:1030].mean() > score_flat.mean()


def test_score_continuity_across_fold_boundary_uses_carried_state():
    # Same held-out tail scored with vs. without a preceding history of
    # liquidations should differ -- carried state, not a cold start per fold.
    train_quiet = _bars_from_marks([0.0] * 200)
    train_burst = _bars_from_marks([0.0] * 190 + [10.0] * 10)
    test = _bars_from_marks([0.0] * 20, start="2021-01-10")

    m1 = HawkesUnivariateBranchingRatio().fit(train_quiet)
    m2 = HawkesUnivariateBranchingRatio().fit(train_burst)
    # Force identical fitted params so only the carried R state differs.
    m2._params = m1._params  # noqa: SLF001
    s_quiet_tail = m1.score(test)
    s_burst_tail = m2.score(test)
    assert s_burst_tail[0] > s_quiet_tail[0]


# --- T5: param recovery on synthetic data (CAS-18 acceptance criterion) ---


def test_univariate_param_recovery_on_synthetic_data():
    rng = np.random.default_rng(42)
    # decay*(1+alpha) < 1 (mean-recursion stability for this discrete-time
    # process -- a tighter bound than the continuous-time alpha/beta<1 rule)
    # or the simulated series diverges to Poisson overflow.
    true_mu, true_alpha, true_beta = 0.02, 0.15, 0.3
    marks = _simulate_univariate_marks(true_mu, true_alpha, true_beta, 20_000, rng)

    m = HawkesUnivariateBranchingRatio().fit(_bars_from_marks(list(marks)))
    fit_mu, fit_alpha, fit_beta = m.params

    assert fit_mu == pytest.approx(true_mu, rel=0.4)
    assert fit_alpha == pytest.approx(true_alpha, rel=0.4)
    assert fit_beta == pytest.approx(true_beta, rel=0.4)


# --- optional graph-covariate baseline (mu_i = mu*exp(gamma*z_i)) ---


def test_covariate_baseline_off_matches_constant_mu():
    # cov_col set but the column absent from X must be a no-op: identical
    # params and scores to the constant-mu model, so the existing reference
    # runs stay bit-reproducible when the graph baseline isn't supplied.
    rng = np.random.default_rng(0)
    marks = rng.poisson(0.05, size=3000).astype(float)
    bars = _bars_from_marks(list(marks))

    m_plain = HawkesUnivariateBranchingRatio().fit(bars)
    m_cov = HawkesUnivariateBranchingRatio(cov_col="frac_at_risk").fit(bars)

    assert m_cov.gamma == 0.0
    np.testing.assert_allclose(m_cov.params, m_plain.params)
    np.testing.assert_allclose(m_cov.score(bars), m_plain.score(bars))


def test_covariate_baseline_recovers_gamma_sign_and_bounded_score():
    rng = np.random.default_rng(5)
    z = rng.standard_normal(8000)
    marks = _simulate_cov_marks(0.05, 0.8, 0.1, 0.3, z, rng)
    bars = _bars_from_marks(list(marks)).assign(frac_at_risk=z)

    m = HawkesUnivariateBranchingRatio(cov_col="frac_at_risk").fit(bars)
    assert m.gamma > 0.2  # recovers the positive covariate->baseline coupling
    scores = m.score(bars)
    assert np.isfinite(scores).all()
    assert ((scores >= 0) & (scores < 1)).all()


def test_covariate_baseline_missing_rows_are_neutral():
    # Bars before the first graph snapshot carry NaN covariate; they must
    # score as z=0 (the plain constant-mu baseline), not crash or NaN out.
    rng = np.random.default_rng(6)
    z = rng.standard_normal(4000)
    marks = _simulate_cov_marks(0.05, 0.8, 0.1, 0.3, z, rng)
    bars = _bars_from_marks(list(marks)).assign(frac_at_risk=z)
    m = HawkesUnivariateBranchingRatio(cov_col="frac_at_risk").fit(bars)

    bars_gap = bars.copy()
    bars_gap.loc[:50, "frac_at_risk"] = np.nan
    scores = m.score(bars_gap)
    assert np.isfinite(scores).all()
    assert ((scores >= 0) & (scores < 1)).all()


def test_covariate_mode_rejects_unknown():
    with pytest.raises(ValueError):
        HawkesUnivariateBranchingRatio(cov_col="frac_at_risk", cov_mode="nope")


def test_covariate_excitation_recovers_positive_delta():
    rng = np.random.default_rng(8)
    z = rng.standard_normal(8000)
    marks = _simulate_excitation_marks(0.05, 0.4, 0.1, 0.3, z, rng)
    bars = _bars_from_marks(list(marks)).assign(frac_at_risk=z)

    m = HawkesUnivariateBranchingRatio(
        cov_col="frac_at_risk", cov_mode="excitation"
    ).fit(bars)
    assert m.cov_coef > 0.1  # recovers the fragility->pressure coupling
    scores = m.score(bars)
    assert ((scores >= 0) & (scores < 1)).all()


def test_covariate_excitation_raises_score_with_fragility():
    # Excitation mode's whole point: above-average fragility must RAISE n(t)
    # (unlike baseline mode, which lowers it). Hold the liquidation stream
    # flat/quiet so R contributes almost nothing, then a high-fragility bar
    # should still score above a low-fragility bar.
    rng = np.random.default_rng(9)
    n = 3000
    z = rng.standard_normal(n)
    marks = _simulate_excitation_marks(0.05, 0.5, 0.05, 0.3, z, rng)
    bars = _bars_from_marks(list(marks)).assign(frac_at_risk=z)
    m = HawkesUnivariateBranchingRatio(
        cov_col="frac_at_risk", cov_mode="excitation"
    ).fit(bars)
    scores = m.score(bars)
    assert scores[z > 1.0].mean() > scores[np.abs(z) < 0.2].mean()


def test_multivariate_param_recovery_on_synthetic_data():
    rng = np.random.default_rng(7)
    # Row sums of decay*(I+alpha) < 1 keeps the process stable (Perron-
    # Frobenius bound on the nonnegative growth operator).
    true_mu = np.array([0.01, 0.015])
    true_alpha = np.array([[0.25, 0.08], [0.05, 0.3]])
    true_beta = 0.35
    marks = _simulate_multivariate_marks(true_mu, true_alpha, true_beta, 20_000, rng)

    mark_cols = ["n_liq_a", "n_liq_b"]
    n = len(marks)
    bars = pd.DataFrame(
        {
            "end_block": np.arange(n) * 5 + 5,
            "end_time": pd.date_range("2021-01-01", periods=n, freq="h", tz="UTC"),
            mark_cols[0]: marks[:, 0],
            mark_cols[1]: marks[:, 1],
        }
    )

    m = HawkesMultivariateBranchingRatio(mark_cols=mark_cols).fit(bars)
    fit_mu, fit_alpha, fit_beta = m.params

    np.testing.assert_allclose(fit_mu, true_mu, rtol=0.4)
    np.testing.assert_allclose(fit_alpha, true_alpha, rtol=0.4, atol=0.05)
    assert fit_beta == pytest.approx(true_beta, rel=0.4)


# --- multivariate model contract ---


def _bars_from_channel_marks(
    marks: np.ndarray, mark_cols: list[str], start: str = "2021-01-01"
) -> pd.DataFrame:
    n = len(marks)
    times = pd.date_range(start, periods=n, freq="h", tz="UTC")
    data = {
        "end_block": np.arange(n) * 5 + 5,
        "end_time": times,
    }
    for j, col in enumerate(mark_cols):
        data[col] = marks[:, j]
    return pd.DataFrame(data)


def test_multivariate_score_before_fit_raises():
    m = HawkesMultivariateBranchingRatio(mark_cols=["a", "b"])
    bars = _bars_from_channel_marks(np.zeros((5, 2)), ["a", "b"])
    with pytest.raises(RuntimeError):
        m.score(bars)


def test_multivariate_rejects_empty_mark_cols():
    with pytest.raises(ValueError):
        HawkesMultivariateBranchingRatio(mark_cols=[])


def test_multivariate_score_bounded_and_finite():
    rng = np.random.default_rng(3)
    mu = np.array([0.01, 0.01, 0.01])
    alpha = np.array([[0.15, 0.05, 0.0], [0.1, 0.15, 0.05], [0.0, 0.1, 0.15]])
    marks = _simulate_multivariate_marks(mu, alpha, 0.3, 2000, rng)
    cols = ["a", "b", "c"]
    bars = _bars_from_channel_marks(marks, cols)

    m = HawkesMultivariateBranchingRatio(mark_cols=cols).fit(bars)
    scores = m.score(bars)
    channel = m.channel_scores(bars)

    assert np.isfinite(scores).all()
    assert ((scores >= 0) & (scores < 1)).all()
    assert list(channel.columns) == cols
    assert ((channel.to_numpy() >= 0) & (channel.to_numpy() < 1)).all()


def test_multivariate_at_k1_matches_univariate_definition():
    # At K=1 the multivariate aggregate n(t) is defined to collapse to the
    # exact univariate formula -- a correctness check on the aggregation.
    rng = np.random.default_rng(9)
    marks = _simulate_univariate_marks(0.02, 0.15, 0.3, 3000, rng)
    bars_uni = _bars_from_marks(list(marks))
    bars_multi = _bars_from_channel_marks(marks.reshape(-1, 1), ["n_liquidations"])

    m_uni = HawkesUnivariateBranchingRatio().fit(bars_uni)
    m_multi = HawkesMultivariateBranchingRatio(mark_cols=["n_liquidations"]).fit(
        bars_multi
    )
    # Force identical fitted params (both are consistent estimators of the
    # same generative process but may land at slightly different optima).
    mu, alpha, beta = m_uni.params
    m_multi._params = (np.array([mu]), np.array([[alpha]]), beta)  # noqa: SLF001
    m_multi._r_end = np.array([m_uni._r_end])  # noqa: SLF001

    np.testing.assert_allclose(
        m_multi.score(bars_multi), m_uni.score(bars_uni), rtol=1e-10
    )


def test_multivariate_analytic_gradient_matches_finite_difference():
    # The multivariate fit uses jac=True (an analytic gradient) instead of
    # L-BFGS-B's finite-difference default -- needed to keep a 56-fold
    # expanding-window walk-forward run tractable at K=5 channels (31
    # params). A wrong gradient wouldn't crash, just silently mis-fit, so
    # check it against scipy's numerical gradient at a few points.
    from scipy.optimize import check_grad

    rng = np.random.default_rng(11)
    k = 3
    marks = rng.poisson(0.05, size=(400, k)).astype(float)
    marks[100:105, 0] += 5

    m = HawkesMultivariateBranchingRatio(mark_cols=[f"c{i}" for i in range(k)])

    def f(x):
        return m._neg_log_lik(x, marks)  # noqa: SLF001

    def g(x):
        return m._neg_log_lik_and_grad(x, marks)[1]  # noqa: SLF001

    for seed in range(3):
        x0 = np.random.default_rng(seed).normal(scale=0.3, size=k + k * k + 1)
        assert check_grad(f, g, x0) < 1e-3


def test_multivariate_spectral_radius_matches_manual_eigenvalues():
    m = HawkesMultivariateBranchingRatio(mark_cols=["a", "b"])
    m._params = (
        np.array([0.01, 0.01]),
        np.array([[0.6, 0.2], [0.1, 0.4]]),
        1.0,
    )  # noqa: SLF001
    expected = float(np.max(np.abs(np.linalg.eigvals([[0.6, 0.2], [0.1, 0.4]]))))
    assert m.spectral_radius == pytest.approx(expected)
