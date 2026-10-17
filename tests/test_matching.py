"""Decision, scoring and explanation rules on hand-built components (no libpostal needed)."""

from __future__ import annotations

import dataclasses

from conftest import make_norm

from addrmatch.comparison import Outcome, compare_addresses
from addrmatch.decision import Decision, decide
from addrmatch.explanation import build_explanation
from addrmatch.scoring import compute_score

FULL = dict(house_number="12", road="main street", city="springfield", state="illinois", postcode="62701")


def run(cfg, a, b):
    comps = compare_addresses(a, b, cfg)
    score = compute_score(comps, cfg)
    dec = decide(comps, score, cfg)
    expl = build_explanation(comps, dec, cfg, normalized_equal=a.normalized == b.normalized, raw_equal=False)
    return comps, score, dec, expl


def test_identical_components_match(cfg):
    _, score, dec, expl = run(cfg, make_norm(**FULL), make_norm(**FULL))
    assert dec.decision is Decision.MATCH
    assert score.score == 100
    assert "building number" in expl.lower() and "match" in expl


def test_building_number_conflict_is_non_match_despite_high_score(cfg):
    b = make_norm(**{**FULL, "house_number": "21"})
    comps, score, dec, expl = run(cfg, make_norm(**FULL), b)
    assert score.score >= 60  # aggregate is still fairly high...
    assert dec.decision is Decision.NON_MATCH  # ...but the contradiction wins
    assert "Building number differs: 12 vs 21" in expl


def test_house_number_suffix_is_conflict(cfg):
    comps, _, dec, expl = run(cfg, make_norm(**FULL), make_norm(**{**FULL, "house_number": "12a"}))
    assert dec.decision is Decision.NON_MATCH
    assert "same digits, different suffix" in expl


def test_unit_conflict_is_non_match(cfg):
    a = make_norm(**FULL, unit="apartment 4b")
    b = make_norm(**FULL, unit="apt #5c")
    _, _, dec, expl = run(cfg, a, b)
    assert dec.decision is Decision.NON_MATCH
    assert "Unit differs" in expl


def test_unit_formatting_variants_match(cfg):
    a = make_norm(**FULL, unit="apartment 4b")
    b = make_norm(**FULL, unit="apt # 4-B")
    _, _, dec, _ = run(cfg, a, b)
    assert dec.decision is Decision.MATCH


def test_unit_on_one_side_only_is_review(cfg):
    _, _, dec, expl = run(cfg, make_norm(**FULL, unit="apartment 4b"), make_norm(**FULL))
    assert dec.decision is Decision.REVIEW
    assert "Unit missing from AddressB" in expl


def test_postcode_conflict_is_review_by_default(cfg):
    _, _, dec, expl = run(cfg, make_norm(**FULL), make_norm(**{**FULL, "postcode": "62704"}))
    assert dec.decision is Decision.REVIEW
    assert "Postal code differs: 62701 vs 62704" in expl


def test_postcode_action_is_configurable(cfg):
    strict = dataclasses.replace(cfg, contradictions={**cfg.contradictions, "postcode": "non_match"})
    _, _, dec, _ = run(strict, make_norm(**FULL), make_norm(**{**FULL, "postcode": "62704"}))
    assert dec.decision is Decision.NON_MATCH


def test_zip_plus_four_agrees(cfg):
    _, _, dec, _ = run(cfg, make_norm(**FULL), make_norm(**{**FULL, "postcode": "62701-1234"}))
    assert dec.decision is Decision.MATCH


def test_missing_components_are_not_penalised(cfg):
    a = make_norm(**FULL)
    b = make_norm(house_number="12", road="main street", city="springfield", postcode="62701")  # no state
    comps, score, dec, _ = run(cfg, a, b)
    state = next(c for c in comps if c.field == "state")
    assert state.outcome is Outcome.MISSING_B
    assert score.score == 100
    assert dec.decision is Decision.MATCH


def test_missing_building_number_is_review(cfg):
    b = make_norm(road="main street", city="springfield", state="illinois", postcode="62701")
    _, _, dec, expl = run(cfg, make_norm(**FULL), b)
    assert dec.decision is Decision.REVIEW
    assert "Building number missing from AddressB" in expl


def test_street_and_city_alone_never_match(cfg):
    a = make_norm(road="main street", city="springfield")
    _, score, dec, _ = run(cfg, a, make_norm(road="main street", city="springfield"))
    assert score.score == 100
    assert dec.decision is Decision.REVIEW


def test_no_location_component_is_review(cfg):
    a = make_norm(house_number="12", road="main street")
    _, _, dec, expl = run(cfg, a, make_norm(house_number="12", road="main street"))
    assert dec.decision is Decision.REVIEW
    assert "to confirm" in expl


def test_no_overlap_gives_na_score(cfg):
    _, score, dec, _ = run(cfg, make_norm(city="springfield"), make_norm(postcode="62701"))
    assert score.score is None
    assert dec.decision is Decision.REVIEW


def test_numbered_street_conflict(cfg):
    a = make_norm(**{**FULL, "road": "5th avenue"})
    b = make_norm(**{**FULL, "road": "6th avenue"})
    _, _, dec, expl = run(cfg, a, b)
    assert dec.decision is Decision.NON_MATCH
    assert "numbers in street name differ" in expl


def test_city_found_under_other_label(cfg):
    a = make_norm(**FULL)
    b = make_norm(house_number="12", road="main street", locality="springfield", state="illinois", postcode="62701")
    comps, _, dec, _ = run(cfg, a, b)
    city = next(c for c in comps if c.field == "city")
    assert city.outcome is Outcome.MATCH and city.note
    assert dec.decision is Decision.MATCH


def test_scoring_is_deterministic(cfg):
    a, b = make_norm(**FULL), make_norm(**{**FULL, "road": "main st north"})
    results = {(run(cfg, a, b)[1].score, run(cfg, a, b)[3]) for _ in range(5)}
    assert len(results) == 1


def test_explanation_only_mentions_computed_conflicts(cfg):
    b = make_norm(**{**FULL, "house_number": "21", "postcode": "62704"})
    comps, _, dec, expl = run(cfg, make_norm(**FULL), b)
    conflict_fields = {c.field for c in comps if c.outcome is Outcome.CONFLICT}
    assert conflict_fields == {"house_number", "postcode"}
    assert "Building number differs" in expl and "Postal code differs" in expl
    assert "Street differs" not in expl and "City differs" not in expl
    # NON-MATCH outranks REVIEW, and the hard conflict is listed first
    assert dec.decision is Decision.NON_MATCH
    assert expl.index("Building number") < expl.index("Postal code")


def test_score_bounds(cfg):
    _, score, _, _ = run(cfg, make_norm(**FULL), make_norm(house_number="999", road="zzz", city="qqq", postcode="00000"))
    assert 0 <= score.score <= 100
