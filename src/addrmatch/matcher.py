"""Row-level pipeline: normalize -> compare -> score -> decide -> explain."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .comparison import compare_addresses
from .decision import Decision, decide
from .explanation import build_explanation
from .normalization import CachingNormalizer, NormalizedAddress, NormStatus
from .scoring import compute_score
from .scoring_config import ScoringConfig

log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    normalized_a: str
    normalized_b: str
    decision: str
    score: int | None
    explanation: str
    reason_codes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing_one_side: list[str] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    status_a: str = ""
    status_b: str = ""
    error: str | None = None

    @property
    def result_text(self) -> str:
        score = f"{self.score}/100" if self.score is not None else "N/A"
        return f"{self.decision} | Score: {score} | {self.explanation}"

    def details_json(self) -> str:
        d = asdict(self)
        for k in ("normalized_a", "normalized_b", "decision", "score", "explanation"):
            d.pop(k)
        return json.dumps(d, ensure_ascii=False)


def format_result(decision: str, score: int | None, explanation: str) -> str:
    return MatchResult("", "", decision, score, explanation).result_text


class AddressMatcher:
    """Deterministic matcher. Same inputs + same config => same output."""

    def __init__(self, normalizer: CachingNormalizer, cfg: ScoringConfig) -> None:
        self.normalize = normalizer
        self.cfg = cfg

    def _unusable(self, a_raw: Any, b_raw: Any, na: NormalizedAddress, nb: NormalizedAddress) -> MatchResult | None:
        problems: list[str] = []
        codes: list[str] = []
        for name, n in (("AddressA", na), ("AddressB", nb)):
            if n.status is NormStatus.EMPTY:
                problems.append(f"{name} is empty")
                codes.append("empty_address")
            elif n.status is NormStatus.FAILED:
                problems.append(f"Normalization failed for {name} ({n.error})")
                codes.append("normalization_failed")
        if not problems:
            return None
        if na.status is NormStatus.EMPTY and nb.status is NormStatus.EMPTY:
            text = "AddressA and AddressB are empty; unable to compare."
        else:
            text = "; ".join(problems) + "; unable to compare."
        return MatchResult(
            normalized_a=na.normalized,
            normalized_b=nb.normalized,
            decision=Decision.REVIEW.value,
            score=None,
            explanation=text,
            reason_codes=sorted(set(codes)),
            status_a=na.status.value,
            status_b=nb.status.value,
            error=None,
        )

    def match(self, address_a: Any, address_b: Any) -> MatchResult:
        na = self.normalize(address_a)
        nb = self.normalize(address_b)
        unusable = self._unusable(address_a, address_b, na, nb)
        if unusable is not None:
            return unusable

        comps = compare_addresses(na, nb, self.cfg)
        score = compute_score(comps, self.cfg)
        dec = decide(comps, score, self.cfg)
        raw_equal = str(address_a).strip() == str(address_b).strip()
        explanation = build_explanation(
            comps, dec, self.cfg, normalized_equal=na.normalized == nb.normalized, raw_equal=raw_equal
        )
        return MatchResult(
            normalized_a=na.normalized,
            normalized_b=nb.normalized,
            decision=dec.decision.value,
            score=score.score,
            explanation=explanation,
            reason_codes=list(dec.reason_codes),
            conflicts=[c.field for c in comps if c.outcome.value == "conflict"],
            missing_one_side=[c.field for c in comps if c.outcome.value in ("missing_a", "missing_b")],
            components=[
                {"field": c.field, "outcome": c.outcome.value, "similarity": c.similarity,
                 "a": c.value_a, "b": c.value_b, "note": c.note}
                for c in comps
                if c.outcome.value != "missing_both"
            ],
            status_a=na.status.value,
            status_b=nb.status.value,
        )

    def safe_match(self, address_a: Any, address_b: Any) -> MatchResult:
        """Like match() but turns unexpected exceptions into a REVIEW row (never drops a row)."""
        try:
            return self.match(address_a, address_b)
        except Exception as exc:  # defensive: one bad row must not fail the job
            log.exception("Unexpected error while matching a row (%s)", type(exc).__name__)
            return MatchResult(
                normalized_a="",
                normalized_b="",
                decision=Decision.REVIEW.value,
                score=None,
                explanation="Processing error for this row; unable to compare.",
                reason_codes=["processing_error"],
                error=type(exc).__name__,
            )
