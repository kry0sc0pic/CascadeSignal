"""Model persistence for the live daemon.

`HawkesUnivariateBranchingRatio` has no save/load of its own (every existing
caller re-`fit`s in-process each run) -- a long-running daemon needs
`(mu, alpha, beta, _r_end)` on disk so a restart resumes scoring instead of
replaying all of history to refit first. This wraps the model rather than
adding persistence to `models/hawkes.py` itself, so the offline algorithm
core stays untouched.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cascadesignal.models.hawkes import (
 HawkesUnivariateBranchingRatio,
 make_operating_model,
)

STATE_DIR = Path("data/live_state")


@dataclass
class LiveModelState:
 protocol: str
 bar_blocks: int
 origin_block: int # first live bar's start_block (anchors the bar clock)
 scored_through_block: int # last block covered by a bar already scored
 mu: float
 alpha: float
 beta: float
 r_end: float
 n_t_last: float
 # USD-mark normalization from fit time (ADR-007). Restored so a reload
 # scores identically; 1.0 is the count-model / legacy default.
 excite_scale: float = 1.0
 cluster_start_block: int | None = None # in-progress cascade cluster,
 cluster_last_block: int | None = None # see live/cascades.py -- so a
 cluster_count: int = 0 # restart resumes it, not restarts it.

 def to_model(self) -> HawkesUnivariateBranchingRatio:
 model = make_operating_model
 model._params = (self.mu, self.alpha, self.beta) # noqa: SLF001
 model._r_end = self.r_end # noqa: SLF001
 model._excite_scale = self.excite_scale # noqa: SLF001
 return model

 @classmethod
 def from_fit(
 cls,
 protocol: str,
 bar_blocks: int,
 origin_block: int,
 scored_through_block: int,
 model: HawkesUnivariateBranchingRatio,
 n_t_last: float,
 ) -> "LiveModelState":
 mu, alpha, beta = model.params
 return cls(
 protocol=protocol,
 bar_blocks=bar_blocks,
 origin_block=origin_block,
 scored_through_block=scored_through_block,
 mu=mu,
 alpha=alpha,
 beta=beta,
 r_end=model._r_end, # noqa: SLF001
 n_t_last=n_t_last,
 excite_scale=getattr(model, "_excite_scale", 1.0),
 )


def state_path(protocol: str, state_dir: Path = STATE_DIR) -> Path:
 return state_dir / f"{protocol}.json"


def save_state(state: LiveModelState, state_dir: Path = STATE_DIR) -> None:
 state_dir.mkdir(parents=True, exist_ok=True)
 path = state_path(state.protocol, state_dir)
 tmp = path.with_suffix(".json.tmp")
 tmp.write_text(json.dumps(asdict(state), indent=2))
 tmp.replace(path) # atomic on POSIX -- a crash mid-write can't corrupt state


def load_state(protocol: str, state_dir: Path = STATE_DIR) -> LiveModelState | None:
 path = state_path(protocol, state_dir)
 if not path.exists:
 return None
 return LiveModelState(**json.loads(path.read_text))


__all__ = ["LiveModelState", "state_path", "save_state", "load_state", "STATE_DIR"]
