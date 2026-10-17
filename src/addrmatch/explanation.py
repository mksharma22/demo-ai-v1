"""Concise, evidence-based explanations built only from computed comparisons."""

from __future__ import annotations

from .comparison import ComponentComparison, Outcome
from .components import FIELD_LABELS
from .decision import Decision, DecisionResult
from .scoring_config import ScoringConfig

MAX_CLAUSES = 4
MAX_VALUE_CHARS = 40

REASON_LABELS: dict[str, str] = {
    "strong_agreement": "Key components agree",
    "low_score": "Low overall similarity",
    "borderline_score": "Score between thresholds",
    "low_coverage": "Too few comparable components",
    "no_comparable_components": "No components comparable on both sides",
    "empty_address": "Empty address",
    "normalization_failed": "Normalization failed",
    "processing_error": "Processing error",
}


def label(fld: str) -> str:
    return FIELD_LABELS.get(fld, fld.replace("_", " ").capitalize())


def _or_list(fields: list[str] | tuple[str, ...]) -> str:
    names = [label(f).lower() for f in fields]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _and_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def reason_label(code: str) -> str:
    """Human label for a reason code (used by summaries and the follow-up chat)."""
    if code in REASON_LABELS:
        return REASON_LABELS[code]
    if code.endswith("_conflict"):
        return f"{label(code[: -len('_conflict')])} differs"
    if code.endswith("_missing_one_side"):
        return f"{label(code[: -len('_missing_one_side')])} present on one side only"
    if code.startswith("insufficient_evidence:"):
        return f"No matching {_or_list(code.split(':', 1)[1].split('|'))}"
    return code


def _short(v: str | None) -> str:
    v = v or ""
    return v if len(v) <= MAX_VALUE_CHARS else v[: MAX_VALUE_CHARS - 1] + "…"


def _conflict_clause(c: ComponentComparison) -> str:
    text = f"{label(c.field)} differs: {_short(c.value_a)} vs {_short(c.value_b)}"
    return f"{text} ({c.note})" if c.note else text


def _missing_clause(c: ComponentComparison) -> str:
    side = "AddressB" if c.outcome is Outcome.MISSING_B else "AddressA"
    return f"{label(c.field)} missing from {side}"


def _similar_clause(c: ComponentComparison) -> str:
    return f"{label(c.field)} similar ({round((c.similarity or 0) * 100)}%)"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def build_explanation(
    comparisons: list[ComponentComparison],
    decision: DecisionResult,
    cfg: ScoringConfig,
    *,
    normalized_equal: bool,
    raw_equal: bool,
) -> str:
    """Return e.g. "Building number differs: 12 vs 21; street and city match."

    Every clause is derived from a ComponentComparison or a rule outcome; nothing
    is stated that was not computed.
    """
    by_field = {c.field: c for c in comparisons}
    matched = [label(c.field).lower() for c in comparisons if c.outcome is Outcome.MATCH]
    similar = [c for c in comparisons if c.outcome is Outcome.SIMILAR]
    conflicts = [c for c in comparisons if c.outcome is Outcome.CONFLICT]
    clauses: list[str] = []

    if decision.decision is Decision.MATCH:
        clauses.append(f"{_and_list(matched)} match")
        if raw_equal:
            clauses.append("addresses are identical")
        elif normalized_equal:
            clauses.append("differences are formatting/abbreviation only")
        clauses += [_similar_clause(c) for c in similar]
        clauses += [_conflict_clause(c) for c in conflicts]  # score_only conflicts
    else:
        ordered = sorted(
            conflicts,
            key=lambda c: (c.field not in decision.hard_conflicts, c.field not in decision.soft_conflicts),
        )
        clauses += [_conflict_clause(c) for c in ordered]
        clauses += [_missing_clause(by_field[f]) for f in decision.missing_one_side]
        if decision.decision is Decision.REVIEW:
            has_specific = bool(conflicts or decision.missing_one_side)
            if "no_comparable_components" in decision.reason_codes:
                clauses.append("no address component is present on both sides")
            elif not has_specific and decision.unmet_groups:
                clauses.append(f"no matching {_or_list(decision.unmet_groups[0])} to confirm")
            elif not has_specific and decision.low_coverage:
                clauses.append("too few components present on both sides")
            elif decision.reason_codes == ("borderline_score",):
                clauses.append("score is between the match and non-match thresholds")
        clauses += [_similar_clause(c) for c in similar]
        if matched:
            clauses.append(f"{_and_list(matched)} match")
        elif decision.decision is Decision.NON_MATCH and "low_score" in decision.reason_codes:
            clauses.append("no component matches")

    clauses = [c for c in clauses if c]
    if not clauses:
        clauses = ["no component-level evidence available"]
    return _cap("; ".join(clauses[:MAX_CLAUSES])) + "."
