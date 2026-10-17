"""Rule-based MATCH / NON-MATCH / REVIEW decision.

Order of precedence (all configurable in config/scoring.toml):
1. No component comparable on both sides             -> REVIEW
2. Conflict on a component whose action is non_match  -> NON-MATCH (score cannot override)
3. Conflict on a component whose action is review     -> REVIEW
4. Score >= match threshold, enough coverage, every match_requires group satisfied,
   and no critical component present on one side only -> MATCH
5. Score < non_match threshold with enough coverage   -> NON-MATCH
6. Otherwise                                           -> REVIEW
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .comparison import ComponentComparison, Outcome
from .scoring import ScoreResult
from .scoring_config import ScoringConfig


class Decision(str, Enum):
    MATCH = "MATCH"
    NON_MATCH = "NON-MATCH"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    reason_codes: tuple[str, ...]
    hard_conflicts: tuple[str, ...]
    soft_conflicts: tuple[str, ...]
    missing_one_side: tuple[str, ...]
    unmet_groups: tuple[tuple[str, ...], ...]
    low_coverage: bool


def decide(
    comparisons: list[ComponentComparison], score: ScoreResult, cfg: ScoringConfig
) -> DecisionResult:
    by_field = {c.field: c for c in comparisons}
    conflicts = [c.field for c in comparisons if c.outcome is Outcome.CONFLICT]
    hard = tuple(f for f in conflicts if cfg.contradictions.get(f) == "non_match")
    soft = tuple(f for f in conflicts if cfg.contradictions.get(f) == "review")
    missing_one = tuple(
        f
        for f in cfg.review_if_missing_one_side
        if f in by_field and by_field[f].outcome in (Outcome.MISSING_A, Outcome.MISSING_B)
    )
    unmet = tuple(
        group
        for group in cfg.match_requires
        if not any(f in by_field and by_field[f].outcome is Outcome.MATCH for f in group)
    )
    low_cov = score.coverage < cfg.min_coverage

    def result(decision: Decision, codes: list[str]) -> DecisionResult:
        return DecisionResult(decision, tuple(codes), hard, soft, missing_one, unmet, low_cov)

    if score.score is None:
        return result(Decision.REVIEW, ["no_comparable_components"])
    if hard:
        return result(Decision.NON_MATCH, [f"{f}_conflict" for f in hard])
    if soft:
        return result(Decision.REVIEW, [f"{f}_conflict" for f in soft])
    if score.score >= cfg.match_threshold and not low_cov and not missing_one and not unmet:
        return result(Decision.MATCH, ["strong_agreement"])
    if score.score < cfg.non_match_threshold and not low_cov:
        return result(Decision.NON_MATCH, ["low_score"])

    codes = [f"{f}_missing_one_side" for f in missing_one]
    codes += [f"insufficient_evidence:{'|'.join(g)}" for g in unmet]
    if low_cov:
        codes.append("low_coverage")
    if not codes:
        codes.append("borderline_score")
    return result(Decision.REVIEW, codes)
