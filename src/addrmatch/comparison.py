"""Component-level comparison of two normalized addresses (deterministic)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from .components import (
    NUMBER_FIELDS,
    POSTCODE_FIELDS,
    UNIT_FIELDS,
    canonical_identifier,
    canonical_postcode,
    digits_of,
    numeric_tokens,
    unit_identifiers,
)
from .normalization import NormalizedAddress
from .scoring_config import ScoringConfig

# Components searched when a city is missing/conflicting under its own label,
# because libpostal may label the same place as city, suburb, district or state.
AREA_FIELDS = ("city", "locality", "district", "state")


class Outcome(str, Enum):
    MATCH = "match"
    SIMILAR = "similar"
    CONFLICT = "conflict"
    MISSING_A = "missing_a"
    MISSING_B = "missing_b"
    MISSING_BOTH = "missing_both"

    @property
    def both_present(self) -> bool:
        return self in (Outcome.MATCH, Outcome.SIMILAR, Outcome.CONFLICT)


@dataclass(frozen=True)
class ComponentComparison:
    field: str
    outcome: Outcome
    similarity: float | None  # 0..1, only when both sides have the component
    value_a: str | None
    value_b: str | None
    note: str | None = None


def text_similarity(va: tuple[str, ...], vb: tuple[str, ...]) -> float:
    """Best RapidFuzz similarity over candidate forms (handles case, punctuation, word order)."""
    best = 0.0
    for x in va:
        for y in vb:
            if x == y:
                return 1.0
            s = max(
                fuzz.token_sort_ratio(x, y, processor=default_process),
                fuzz.ratio(x, y, processor=default_process),
            )
            best = max(best, s / 100.0)
    return best


def _bucket(sim: float, cfg: ScoringConfig) -> Outcome:
    if sim >= cfg.text_match:
        return Outcome.MATCH
    if sim >= cfg.text_similar:
        return Outcome.SIMILAR
    return Outcome.CONFLICT


def _compare_number(fld: str, a: str, b: str) -> ComponentComparison:
    ca = canonical_identifier(a, po_box=fld == "po_box")
    cb = canonical_identifier(b, po_box=fld == "po_box")
    if ca == cb:
        return ComponentComparison(fld, Outcome.MATCH, 1.0, a, b)
    da, db = digits_of(a), digits_of(b)
    if da and da == db:
        return ComponentComparison(fld, Outcome.CONFLICT, 0.5, a, b, "same digits, different suffix")
    return ComponentComparison(fld, Outcome.CONFLICT, 0.0, a, b)


def _compare_postcode(fld: str, a_disp: str, b_disp: str) -> ComponentComparison:
    a, b = canonical_postcode(a_disp), canonical_postcode(b_disp)
    a_disp, b_disp = a_disp.upper(), b_disp.upper()
    if a == b:
        return ComponentComparison(fld, Outcome.MATCH, 1.0, a_disp, b_disp)
    if a.isdigit() and b.isdigit() and {len(a), len(b)} == {5, 9} and a[:5] == b[:5]:
        return ComponentComparison(fld, Outcome.MATCH, 1.0, a_disp, b_disp, "ZIP and ZIP+4 agree")
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_.startswith(short):
        return ComponentComparison(fld, Outcome.SIMILAR, 0.6, a_disp, b_disp, "one postal code is a prefix of the other")
    return ComponentComparison(fld, Outcome.CONFLICT, 0.0, a_disp, b_disp)


def _compare_unit(
    fld: str, va: tuple[str, ...], vb: tuple[str, ...], cfg: ScoringConfig
) -> ComponentComparison:
    ids_a = [unit_identifiers(v) for v in va]
    ids_b = [unit_identifiers(v) for v in vb]
    if all(ids_a) and all(ids_b):
        for x in ids_a:
            for y in ids_b:
                if x == y:
                    return ComponentComparison(fld, Outcome.MATCH, 1.0, va[0], vb[0])
        for x in ids_a:
            for y in ids_b:
                if x < y or y < x:
                    return ComponentComparison(
                        fld, Outcome.SIMILAR, 0.7, va[0], vb[0], "one unit is more specific"
                    )
        return ComponentComparison(fld, Outcome.CONFLICT, 0.0, va[0], vb[0])
    sim = text_similarity(va, vb)  # unit without identifiers, e.g. "basement"
    return ComponentComparison(fld, _bucket(sim, cfg), sim, va[0], vb[0])


def _compare_text(
    fld: str, va: tuple[str, ...], vb: tuple[str, ...], cfg: ScoringConfig
) -> ComponentComparison:
    sim = text_similarity(va, vb)
    outcome = _bucket(sim, cfg)
    if fld == "road":
        na, nb = numeric_tokens(va[0]), numeric_tokens(vb[0])
        if na and nb and na != nb:
            return ComponentComparison(fld, Outcome.CONFLICT, min(sim, 0.5), va[0], vb[0], "numbers in street name differ")
    return ComponentComparison(fld, outcome, sim, va[0], vb[0])


def _area_pool(addr: NormalizedAddress) -> tuple[str, ...]:
    out: list[str] = []
    for f in AREA_FIELDS:
        out.extend(addr.components.get(f, ()))
    return tuple(out)


def _city_cross_label(
    current: ComponentComparison, a: NormalizedAddress, b: NormalizedAddress, cfg: ScoringConfig
) -> ComponentComparison:
    """Recover a city found under a different libpostal label on the other side."""
    if current.outcome in (Outcome.MATCH, Outcome.MISSING_BOTH):
        return current
    if a.has("city"):
        sim = text_similarity(a.components["city"], _area_pool(b))
        if sim >= cfg.text_match:
            return ComponentComparison("city", Outcome.MATCH, sim, a.value("city"), a.value("city"),
                                       "found under another area label in AddressB")
    if b.has("city"):
        sim = text_similarity(b.components["city"], _area_pool(a))
        if sim >= cfg.text_match:
            return ComponentComparison("city", Outcome.MATCH, sim, b.value("city"), b.value("city"),
                                       "found under another area label in AddressA")
    return current


def compare_component(
    fld: str, a: NormalizedAddress, b: NormalizedAddress, cfg: ScoringConfig
) -> ComponentComparison:
    va, vb = a.components.get(fld, ()), b.components.get(fld, ())
    if not va and not vb:
        result = ComponentComparison(fld, Outcome.MISSING_BOTH, None, None, None)
    elif not va:
        result = ComponentComparison(fld, Outcome.MISSING_A, None, None, vb[0])
    elif not vb:
        result = ComponentComparison(fld, Outcome.MISSING_B, None, va[0], None)
    elif fld in NUMBER_FIELDS:
        result = _compare_number(fld, va[0], vb[0])
    elif fld in POSTCODE_FIELDS:
        result = _compare_postcode(fld, va[0], vb[0])
    elif fld in UNIT_FIELDS:
        result = _compare_unit(fld, va, vb, cfg)
    else:
        result = _compare_text(fld, va, vb, cfg)
    if fld == "city":
        result = _city_cross_label(result, a, b, cfg)
    return result


def compare_addresses(
    a: NormalizedAddress, b: NormalizedAddress, cfg: ScoringConfig
) -> list[ComponentComparison]:
    """Compare every weighted component, in config order."""
    return [compare_component(fld, a, b, cfg) for fld in cfg.weights]
