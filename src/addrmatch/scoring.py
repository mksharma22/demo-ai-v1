"""Weighted 0-100 similarity score over components present on both sides.

The score is a weighted average similarity, NOT a probability. It only becomes
interpretable as a likelihood after calibration against labelled data.
"""

from __future__ import annotations

from dataclasses import dataclass

from .comparison import ComponentComparison
from .scoring_config import ScoringConfig


@dataclass(frozen=True)
class ScoreResult:
    score: int | None          # None when no component is present on both sides
    compared_weight: float     # sum of weights of components present on both sides
    coverage: float            # compared_weight / weight of components present on either side


def compute_score(comparisons: list[ComponentComparison], cfg: ScoringConfig) -> ScoreResult:
    num = 0.0
    den = 0.0
    available = 0.0
    for c in comparisons:
        w = cfg.weights.get(c.field, 0.0)
        if w > 0 and c.outcome.value != "missing_both":
            available += w
        if w <= 0 or not c.outcome.both_present or c.similarity is None:
            continue  # missing components are unknown, not mismatches
        num += w * c.similarity
        den += w
    coverage = den / available if available else 0.0
    if den == 0:
        return ScoreResult(None, 0.0, coverage)
    return ScoreResult(int(round(100 * num / den)), den, coverage)
