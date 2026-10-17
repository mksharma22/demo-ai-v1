"""Load and validate the matching configuration (config/scoring.toml)."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

VALID_ACTIONS = {"non_match", "review", "score_only"}


@dataclass(frozen=True)
class ScoringConfig:
    weights: dict[str, float]
    text_match: float = 0.90
    text_similar: float = 0.75
    max_expansions: int = 5
    match_threshold: int = 85
    non_match_threshold: int = 50
    min_coverage: float = 0.60
    match_requires: tuple[tuple[str, ...], ...] = ()
    review_if_missing_one_side: tuple[str, ...] = ()
    contradictions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 < self.text_similar <= self.text_match <= 1:
            raise ValueError("Require 0 < text_similar <= text_match <= 1")
        if not 0 <= self.non_match_threshold <= self.match_threshold <= 100:
            raise ValueError("Require 0 <= non_match <= match <= 100")
        if not 0 <= self.min_coverage <= 1:
            raise ValueError("min_coverage must be within [0, 1]")
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("Weights must be non-negative")
        bad = {k: v for k, v in self.contradictions.items() if v not in VALID_ACTIONS}
        if bad:
            raise ValueError(f"Unknown contradiction actions {bad}; use one of {sorted(VALID_ACTIONS)}")

    @property
    def total_weight(self) -> float:
        return float(sum(self.weights.values()))

    def fingerprint(self) -> str:
        """Stable hash, used so a changed config does not reuse cached job results."""
        blob = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_scoring_config(path: str | Path) -> ScoringConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scoring config not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    sim = raw.get("similarity", {})
    thr = raw.get("thresholds", {})
    rules = raw.get("rules", {})
    return ScoringConfig(
        weights={k: float(v) for k, v in raw.get("weights", {}).items()},
        text_match=float(sim.get("text_match", 0.90)),
        text_similar=float(sim.get("text_similar", 0.75)),
        max_expansions=int(sim.get("max_expansions", 5)),
        match_threshold=int(thr.get("match", 85)),
        non_match_threshold=int(thr.get("non_match", 50)),
        min_coverage=float(thr.get("min_coverage", 0.60)),
        match_requires=tuple(tuple(g) for g in rules.get("match_requires", [])),
        review_if_missing_one_side=tuple(rules.get("review_if_missing_one_side", [])),
        contradictions=dict(raw.get("contradictions", {})),
    )
