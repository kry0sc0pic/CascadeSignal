"""M1: Hawkes processes + branching ratio n(t) .
multivariate by collateral asset -> multiplex-network Hawkes) is split across
milestones -- own acceptance criteria call for univariate +
multivariate fits , with the multiplex-network (channel-specific
kernels over the contagion graph) deferred, since that needs the
contagion graph (not built yet). This module holds both
pieces:

* `HawkesUnivariateBranchingRatio` -- one self-exciting process on the
 aggregate liquidation-count series (already validated: n(t) rises
 pre-episode and beats B1).
* `HawkesMultivariateBranchingRatio` -- a mutually-exciting process over a
 small set of per-collateral-asset liquidation-count channels (e.g. WETH,
 stETH, WBTC, "other"), so cross-asset contagion (liquidating WETH collateral
 exciting stETH liquidations, etc.) shows up as off-diagonal alpha terms
 instead of being averaged away. Multiplex kernels (channel-specific decay
 per *contagion mechanism* -- shared collateral vs. shared DEX liquidity vs.
 composability) .

Model (univariate): exponential-kernel Hawkes on the bar-level liquidation-
count series (marks = n_liquidations per 5-block bar from).
Conditional intensity recursion (marks m_0..m_{n-1}, decay = exp(-beta)):

 R_0 = 0
 R_i = decay * (R_{i-1} + m_{i-1}) # self-excitation carried into bar i
 lambda_i = mu + alpha * R_i

R_i uses only marks strictly before bar i (the standard Hawkes intensity
convention), so it never depends on the bar's own count -- consistent with the. (mu, alpha, beta) are fit by Poisson MLE on the train
fold only; `score` continues the same recursion into the test fold carrying
the train fold's final R state, so there's no cold-start leak or bias at fold
boundaries -- fold continuity, not future data.

n(t), the instantaneous branching ratio, is the self-excited share of the
intensity: n(t) = alpha*R(t) / lambda(t) in [0, 1). It rises toward 1 as
liquidation clustering intensifies (the process approaches criticality) and
is the score this module exposes via a fit(X,y)/score(X) interface, so it
plugs directly into models/harness.py.

Model (multivariate): K channels, each with the same recursion applied
per-channel (a shared scalar decay across channels, not one per channel
keeps the param count at K + K*K + 1 instead of K + K*K + K, which matters
given how sparse most per-asset liquidation-count channels are):

 R_i = decay * (R_{i-1} + m_{i-1}) # per-channel, R_i is a K-vector
 lambda_i = mu + alpha @ R_i # alpha[a, b]: channel b -> channel a

n(t) generalizes the same way, aggregated across channels by intensity share
so it collapses exactly to the univariate formula at K=1:

 n(t) = sum_a (alpha @ R(t))[a] / sum_a lambda(t)[a]

`spectral_radius` (the spectral radius of alpha/beta, the branching matrix) is
the standard multivariate-Hawkes stability threshold -- < 1 for a stationary
process -- and is exposed as a per-fit criticality diagnostic alongside n(t).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter, lfiltic

_LOG_BOUNDS = (-25.0, 5.0)
_GAMMA_BOUNDS = (-10.0, 10.0)
_DELTA_BOUNDS = (0.0, 1e6)


def _decayed_history(marks: np.ndarray, decay: float, r0: float = 0.0) -> np.ndarray:
 """R_i (self-excited history strictly before bar i) for i = 0..len(marks)-1,
 continuing from a carried-over initial state `r0`."""
 if len(marks) == 0:
 return np.empty(0, dtype=float)
 zi = lfiltic([1.0], [1.0, -decay], y=[r0])
 tail, _ = lfilter([1.0], [1.0, -decay], decay * marks, zi=zi)
 return np.concatenate(([r0], tail[:-1]))


def _decayed_history_multi(
 marks: np.ndarray, decay: float, r0: np.ndarray | None = None
) -> np.ndarray:
 """Per-channel `_decayed_history`, K independent recursions sharing one
 scalar decay. `marks` is (T, K); returns R of shape (T, K)."""
 n, k = marks.shape
 if r0 is None:
 r0 = np.zeros(k, dtype=float)
 if n == 0:
 return np.empty((0, k), dtype=float)
 return np.stack(
 [_decayed_history(marks[:, j], decay, r0=float(r0[j])) for j in range(k)],
 axis=1,
 )


class HawkesUnivariateBranchingRatio:
 """Univariate exponential-kernel Hawkes; scores bars by the instantaneous
 branching ratio n(t) = alpha*R(t) / (mu + alpha*R(t)).

 Optional graph covariate: pass `cov_col` (the contagion-graph systemic-
 fragility scalar, see graph/fragility.py) to fold a per-bar covariate z_i
 into the model. `cov_mode` picks WHERE it enters -- and, decisively for an
 n(t) alarm, whether it raises or lowers the score:

 * "baseline": log-linear background rate, lambda_i = mu*exp(gamma*z_i) +
 alpha*R_i. This is the literal "plug the graph into the base event rate"
 form. But n(t) is the *self-excited share*, so a larger baseline enlarges
 the denominator and pushes n(t) DOWN when fragility is high -- the graph
 signal fights the alarm's direction (verified: it costs ~15-20% AUPRC
 lift on Aave v2). Kept for that comparison, not recommended.

 * "excitation": fragility enters the NUMERATOR as exogenous cascade
 pressure, lambda_i = mu + alpha*R_i + delta*relu(z_i), delta >= 0, and
 n(t) = (alpha*R_i + delta*relu(z_i)) / lambda_i. Now above-average
 fragility RAISES the alarm, the same direction as self-excitation
 structural fragility is treated as part of criticality alongside
 endogenous clustering. relu(z_i) so only above-average fragility adds
 pressure (never subtracts, which would push n(t) below 0).

 Both add exactly one parameter over (mu, alpha, beta) and collapse to the
 constant-mu model at gamma=0 / delta=0. z_i is standardized with the TRAIN
 fold's own mean/std (stored at fit, reused at score) so the walk-forward
 stays leakage-free, and a bar with no snapshot yet (NaN covariate) gets
 z=0 (neutral). With `cov_col` unset the model is numerically identical to
 the plain n(t) form.
 """

 name = "M1_hawkes_branching_ratio"

 def __init__(
 self,
 mark_col: str = "n_liquidations",
 init: tuple[float, float, float] = (1e-4, 0.5, 0.1),
 cov_col: str | None = None,
 cov_mode: str = "baseline",
 excite_col: str | None = None,
 dollar_weight: float | None = None,
 ):
 if cov_mode not in ("baseline", "excitation"):
 raise ValueError(f"cov_mode must be baseline|excitation, got {cov_mode!r}")
 self.mark_col = mark_col
 self.init = init
 self.cov_col = cov_col
 self.cov_mode = cov_mode
 # USD-marked Hawkes (ADR-007): when `excite_col` is set, the self-
 # exciting history R is driven by liquidated USD, not just event counts,
 # while the Poisson likelihood stays on `mark_col` (counts) -- the
 # standard marked ground-intensity form (marks modulate future event
 # rate). `dollar_weight` picks the mark:
 # excite_col None -> counts (count model)
 # excite_col set, weight None -> usd (pure USD mark)
 # excite_col set, weight = w -> counts + w*usd_scaled (blend; w=0.5
 # is the deployed operating point -- keeps count sensitivity for
 # fast-cascade lead, adds dollar volume to kill count-dense/dollar-
 # light false alarms). usd is scaled by its fit-time positive mean.
 self.excite_col = excite_col
 self.dollar_weight = dollar_weight
 self._params: tuple[float, float, float] | None = None
 self._cov_coef: float = 0.0 # gamma (baseline) or delta (excitation)
 self._cov_mean: float = 0.0
 self._cov_std: float = 1.0
 self._excite_scale: float = 1.0 # conditions L-BFGS; n(t) is scale-free
 self._r_end: float = 0.0

 def _active_cov(self, X: pd.DataFrame) -> bool:
 return self.cov_col is not None and self.cov_col in X.columns

 def _excite_marks(self, X: pd.DataFrame) -> np.ndarray:
 """Marks that drive the self-exciting history R: the USD column
 (scaled by the fit-time mean for conditioning) when `excite_col` is
 set and present, else the count marks (`mark_col`) -- the original
 behaviour. n(t) is invariant to the scale, so it only conditions the
 optimizer."""
 counts = X[self.mark_col].to_numpy(dtype=float)
 if self.excite_col is None or self.excite_col not in X.columns:
 return counts
 usd = np.nan_to_num(X[self.excite_col].to_numpy(dtype=float), nan=0.0)
 usd = usd / self._excite_scale
 if self.dollar_weight is None:
 return usd # pure USD-marked
 return counts + self.dollar_weight * usd # blend

 def _standardize(self, X: pd.DataFrame) -> np.ndarray:
 """Train-standardized covariate, NaN (no snapshot yet) -> 0 (neutral)."""
 cov = X[self.cov_col].to_numpy(dtype=float) # type: ignore[index]
 z = (cov - self._cov_mean) / self._cov_std
 return np.nan_to_num(z, nan=0.0)

 def _excitation_and_lambda(
 self, mu: float, alpha: float, r: np.ndarray, z: np.ndarray | None
 ) -> tuple[np.ndarray | float, np.ndarray | float]:
 """(excited numerator, lambda) for the two covariate modes; `z=None`
 is the plain constant-mu process. The alarm is excited/lambda."""
 ar = alpha * r
 if z is None:
 return ar, mu + ar
 if self.cov_mode == "excitation":
 frag = self._cov_coef * np.maximum(z, 0.0)
 return ar + frag, mu + ar + frag
 return ar, mu * np.exp(self._cov_coef * z) + ar # baseline

 def _neg_log_lik(
 self,
 free: np.ndarray,
 obs_marks: np.ndarray,
 excite_marks: np.ndarray,
 z: np.ndarray | None,
 ) -> float:
 mu, alpha, beta = np.exp(free[:3])
 decay = np.exp(-beta)
 r = _decayed_history(excite_marks, decay)
 ar = alpha * r
 if z is None:
 lam = mu + ar
 elif self.cov_mode == "excitation":
 lam = mu + ar + free[3] * np.maximum(z, 0.0)
 else:
 lam = mu * np.exp(free[3] * z) + ar
 lam = np.clip(lam, 1e-12, None)
 # Poisson NLL on event COUNTS, dropping the marks!-normalizing constant
 # (irrelevant to argmin). Excitation R is dollar-marked when excite_col
 # is set; the observed process is still the liquidation-event count.
 return float(np.sum(lam - obs_marks * np.log(lam)))

 def fit(
 self, X: pd.DataFrame, y: np.ndarray | None = None
 ) -> "HawkesUnivariateBranchingRatio":
 marks = X[self.mark_col].to_numpy(dtype=float)
 if self.excite_col is not None and self.excite_col in X.columns:
 raw = np.nan_to_num(X[self.excite_col].to_numpy(dtype=float), nan=0.0)
 pos = raw[raw > 0]
 self._excite_scale = float(pos.mean) if pos.size else 1.0
 excite_marks = self._excite_marks(X)
 z: np.ndarray | None = None
 if self._active_cov(X):
 cov = X[self.cov_col].to_numpy(dtype=float) # type: ignore[index]
 finite = cov[np.isfinite(cov)]
 self._cov_mean = float(finite.mean) if finite.size else 0.0
 std = float(finite.std) if finite.size else 0.0
 self._cov_std = std if std > 0 else 1.0
 z = self._standardize(X)

 log_init = np.log(np.array(self.init))
 if z is None:
 x0 = log_init
 bounds = [_LOG_BOUNDS] * 3
 elif self.cov_mode == "excitation":
 # delta init ~ mu init so delta*relu(z) starts comparable to mu.
 x0 = np.concatenate([log_init, [self.init[0]]])
 bounds = [_LOG_BOUNDS] * 3 + [_DELTA_BOUNDS]
 else:
 x0 = np.concatenate([log_init, [0.0]])
 bounds = [_LOG_BOUNDS] * 3 + [_GAMMA_BOUNDS]
 res = minimize(
 self._neg_log_lik,
 x0,
 args=(marks, excite_marks, z),
 method="L-BFGS-B",
 bounds=bounds,
 )
 mu, alpha, beta = np.exp(res.x[:3])
 self._cov_coef = float(res.x[3]) if z is not None else 0.0
 self._params = (mu, alpha, beta)
 decay = np.exp(-beta)
 r = _decayed_history(excite_marks, decay)
 self._r_end = (
 float(decay * (r[-1] + excite_marks[-1])) if len(excite_marks) else 0.0
 )
 return self

 def score(self, X: pd.DataFrame) -> np.ndarray:
 if self._params is None:
 raise RuntimeError(
 "HawkesUnivariateBranchingRatio must be fit before score"
 )
 mu, alpha, beta = self._params
 decay = np.exp(-beta)
 r = _decayed_history(self._excite_marks(X), decay, r0=self._r_end)
 z = self._standardize(X) if self._active_cov(X) else None
 excited, lam = self._excitation_and_lambda(mu, alpha, r, z)
 return excited / np.clip(lam, 1e-12, None)

 def intensity(self, X: pd.DataFrame) -> np.ndarray:
 """Raw conditional intensity lambda(t), for diagnostics/plots (not used
 as the harness score -- n(t) is, since it's the bounded criticality
 indicator PLAN §7/E3 asks for)."""
 if self._params is None:
 raise RuntimeError(
 "HawkesUnivariateBranchingRatio must be fit before intensity"
 )
 mu, alpha, beta = self._params
 decay = np.exp(-beta)
 r = _decayed_history(self._excite_marks(X), decay, r0=self._r_end)
 z = self._standardize(X) if self._active_cov(X) else None
 _, lam = self._excitation_and_lambda(mu, alpha, r, z)
 return np.asarray(lam, dtype=float)

 @property
 def params(self) -> tuple[float, float, float]:
 if self._params is None:
 raise RuntimeError(
 "HawkesUnivariateBranchingRatio must be fit before params"
 )
 return self._params

 @property
 def cov_coef(self) -> float:
 """Fitted covariate coefficient: gamma (baseline mode) or delta
 (excitation mode); 0.0 when `cov_col` is off."""
 if self._params is None:
 raise RuntimeError(
 "HawkesUnivariateBranchingRatio must be fit before cov_coef"
 )
 return self._cov_coef

 # Back-compat alias: in the default baseline mode the coefficient is gamma.
 gamma = cov_coef


class HawkesMultivariateBranchingRatio:
 """Mutually-exciting exponential-kernel Hawkes over K liquidation-count
 channels (e.g. per collateral asset); scores bars by the intensity-
 weighted aggregate branching ratio n(t) = sum(alpha @ R) / sum(lambda),
 which collapses to the univariate n(t) formula at K=1."""

 name = "M1_hawkes_multivariate_branching_ratio"

 def __init__(
 self,
 mark_cols: Sequence[str],
 init_mu: float = 1e-4,
 init_alpha_diag: float = 0.3,
 init_alpha_offdiag: float = 0.02,
 init_beta: float = 0.1,
 ):
 if len(mark_cols) == 0:
 raise ValueError("mark_cols must be non-empty")
 self.mark_cols = list(mark_cols)
 self.init_mu = init_mu
 self.init_alpha_diag = init_alpha_diag
 self.init_alpha_offdiag = init_alpha_offdiag
 self.init_beta = init_beta
 self._params: tuple[np.ndarray, np.ndarray, float] | None = None
 self._r_end: np.ndarray | None = None

 @property
 def _k(self) -> int:
 return len(self.mark_cols)

 def _unpack(self, log_theta: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
 k = self._k
 mu = np.exp(log_theta[:k])
 alpha = np.exp(log_theta[k : k + k * k]).reshape(k, k)
 beta = float(np.exp(log_theta[-1]))
 return mu, alpha, beta

 def _neg_log_lik(self, log_theta: np.ndarray, marks: np.ndarray) -> float:
 mu, alpha, beta = self._unpack(log_theta)
 decay = np.exp(-beta)
 r = _decayed_history_multi(marks, decay)
 lam = np.clip(mu[None, :] + r @ alpha.T, 1e-12, None)
 return float(np.sum(lam - marks * np.log(lam)))

 def _neg_log_lik_and_grad(
 self, log_theta: np.ndarray, marks: np.ndarray
 ) -> tuple[float, np.ndarray]:
 """NLL + analytic gradient wrt log_theta. With K + K*K + 1 params
 (K=5 -> 31), L-BFGS-B's default finite-difference gradient needs one
 extra full pass over the (up to multi-million-row, expanding-window)
 marks array per parameter per iteration; supplying the closed form
 here is what keeps a 56-fold walk-forward run tractable.

 d(lam - m*log(lam))/dlam = 1 - m/lam =: w (T, K). Since lam is linear
 in mu and alpha (mu_i, alpha_ij don't affect R):
 d NLL/d mu_i = sum_t w[t, i]
 d NLL/d alpha_ij = sum_t w[t, i] * R[t, j] -> w.T @ R
 beta enters only through decay = exp(-beta) inside R, so
 d NLL/d beta = sum_t sum_j (w @ alpha)[t, j] * dR[t, j]/dbeta, where
 dR/dbeta follows its own AR(1) recursion (differentiate
 R_i = decay*(R_{i-1}+m_{i-1}) wrt beta, decay'(beta) = -decay):
 dR_i/dbeta = decay * dR_{i-1}/dbeta - R_i, dR_{-1}/dbeta = 0
 i.e. dR/dbeta = lfilter([1], [1, -decay], -R) (zero initial state).
 Finally, chain rule to log-params: d/d log(theta) = d/d theta * theta.
 """
 mu, alpha, beta = self._unpack(log_theta)
 decay = np.exp(-beta)
 r = _decayed_history_multi(marks, decay)
 lam = np.clip(mu[None, :] + r @ alpha.T, 1e-12, None)
 nll = float(np.sum(lam - marks * np.log(lam)))

 w = 1.0 - marks / lam # (T, K)
 grad_mu = w.sum(axis=0) # (K,)
 grad_alpha = w.T @ r # (K, K)
 d_r_d_beta = lfilter([1.0], [1.0, -decay], -r, axis=0) # (T, K)
 grad_beta = float(np.sum((w @ alpha) * d_r_d_beta))

 grad_log = np.concatenate(
 [grad_mu * mu, (grad_alpha * alpha).ravel, [grad_beta * beta]]
 )
 return nll, grad_log

 def fit(
 self, X: pd.DataFrame, y: np.ndarray | None = None
 ) -> "HawkesMultivariateBranchingRatio":
 marks = X[self.mark_cols].to_numpy(dtype=float)
 k = self._k
 mu0 = np.full(k, self.init_mu)
 alpha0 = np.full((k, k), self.init_alpha_offdiag)
 np.fill_diagonal(alpha0, self.init_alpha_diag)
 x0 = np.concatenate(
 [np.log(mu0), np.log(alpha0.ravel), [np.log(self.init_beta)]]
 )
 bounds = [_LOG_BOUNDS] * len(x0)
 res = minimize(
 self._neg_log_lik_and_grad,
 x0,
 args=(marks,),
 method="L-BFGS-B",
 jac=True,
 bounds=bounds,
 )
 mu, alpha, beta = self._unpack(res.x)
 self._params = (mu, alpha, beta)
 decay = np.exp(-beta)
 r = _decayed_history_multi(marks, decay)
 self._r_end = decay * (r[-1] + marks[-1]) if len(marks) else np.zeros(k)
 return self

 def _intensity_and_excitation(
 self, X: pd.DataFrame
 ) -> tuple[np.ndarray, np.ndarray]:
 if self._params is None:
 raise RuntimeError(
 "HawkesMultivariateBranchingRatio must be fit before score/intensity"
 )
 mu, alpha, beta = self._params
 marks = X[self.mark_cols].to_numpy(dtype=float)
 decay = np.exp(-beta)
 r = _decayed_history_multi(marks, decay, r0=self._r_end)
 excitation = r @ alpha.T # (T, K): (alpha @ R)_a per bar
 lam = np.clip(mu[None, :] + excitation, 1e-12, None)
 return excitation, lam

 def score(self, X: pd.DataFrame) -> np.ndarray:
 """System-level n(t): intensity-weighted aggregate branching ratio."""
 excitation, lam = self._intensity_and_excitation(X)
 return excitation.sum(axis=1) / lam.sum(axis=1)

 def channel_scores(self, X: pd.DataFrame) -> pd.DataFrame:
 """Per-channel n_a(t), for diagnostics (which asset is driving the
 aggregate signal)."""
 excitation, lam = self._intensity_and_excitation(X)
 return pd.DataFrame(excitation / lam, columns=self.mark_cols, index=X.index)

 def intensity(self, X: pd.DataFrame) -> pd.DataFrame:
 _, lam = self._intensity_and_excitation(X)
 return pd.DataFrame(lam, columns=self.mark_cols, index=X.index)

 @property
 def params(self) -> tuple[np.ndarray, np.ndarray, float]:
 if self._params is None:
 raise RuntimeError(
 "HawkesMultivariateBranchingRatio must be fit before params"
 )
 return self._params

 @property
 def spectral_radius(self) -> float:
 """Spectral radius of the branching matrix alpha/beta -- the standard
 multivariate-Hawkes stability threshold (stationary iff < 1), a
 constant-per-fit criticality diagnostic alongside the time-varying
 n(t) score."""
 _, alpha, beta = self.params
 eigvals = np.linalg.eigvals(alpha / beta)
 return float(np.max(np.abs(eigvals)))


# ADR-007 deployed operating model: the USD-marked blend n(t) = count + w*usd.
# The live monitor, calibration, historical replay, and the paper's operating-
# point figures all build the alarm model here, so the deployed signal is
# defined in exactly one place. `liquidated_usd` comes from
# models/labels.build_liquidation_bars; on a bars frame without it (Compound v2 /
# Maker, count-only) the model degrades gracefully to the count signal.
OPERATING_EXCITE_COL = "liquidated_usd"
OPERATING_DOLLAR_WEIGHT = 0.5


def make_operating_model -> HawkesUnivariateBranchingRatio:
 """The deployed alarm model (ADR-007): USD-marked blend, dollar_weight 0.5."""
 return HawkesUnivariateBranchingRatio(
 excite_col=OPERATING_EXCITE_COL, dollar_weight=OPERATING_DOLLAR_WEIGHT
 )


__all__ = [
 "HawkesUnivariateBranchingRatio",
 "HawkesMultivariateBranchingRatio",
 "make_operating_model",
 "OPERATING_EXCITE_COL",
 "OPERATING_DOLLAR_WEIGHT",
]
