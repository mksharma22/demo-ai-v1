"""Scenarios run through the REAL libpostal. Skipped automatically if it is not installed."""

from __future__ import annotations

import pytest

from addrmatch.matcher import AddressMatcher
from addrmatch.normalization import CachingNormalizer, NormStatus, normalize_address

pytestmark = pytest.mark.libpostal


@pytest.fixture(scope="module")
def matcher(libpostal_backend):
    from conftest import ROOT

    from addrmatch.scoring_config import load_scoring_config

    return AddressMatcher(CachingNormalizer(libpostal_backend), load_scoring_config(ROOT / "config" / "scoring.toml"))


def decision(matcher, a, b):
    return matcher.match(a, b)


def test_parse_and_normalize(libpostal_backend):
    r = normalize_address("12 Main St., Springfield, IL 62701", libpostal_backend)
    assert r.status is NormStatus.OK
    assert r.value("house_number") == "12"
    assert r.value("road") == "main street"
    assert r.normalized.startswith("12 main street")


def test_exact_same(matcher):
    r = decision(matcher, "12 Main Street, Springfield, IL 62701", "12 Main Street, Springfield, IL 62701")
    assert r.decision == "MATCH" and r.score == 100 and "identical" in r.explanation


def test_case_punctuation_spacing_abbreviation(matcher):
    r = decision(matcher, "12 Main Street, Springfield, IL 62701", "12  main st., springfield il 62701")
    assert r.decision == "MATCH" and "formatting" in r.explanation


def test_different_building_numbers(matcher):
    r = decision(matcher, "12 Main St, Springfield, IL 62701", "21 Main St, Springfield, IL 62701")
    assert r.decision == "NON-MATCH" and "12 vs 21" in r.explanation


def test_different_units(matcher):
    r = decision(matcher, "12 Main St Apt 4B, Springfield, IL 62701", "12 Main St Apt 5C, Springfield, IL 62701")
    assert r.decision == "NON-MATCH" and "Unit differs" in r.explanation


def test_unit_abbreviation_matches(matcher):
    r = decision(matcher, "12 Main St Apt 4B, Springfield, IL 62701", "12 Main Street Apartment 4B, Springfield IL 62701")
    assert r.decision == "MATCH"


def test_different_postcodes(matcher):
    r = decision(matcher, "12 Main St, Springfield, IL 62701", "12 Main St, Springfield, IL 62704")
    assert r.decision == "REVIEW" and "Postal code differs" in r.explanation


def test_partial_address(matcher):
    r = decision(matcher, "12 Main St, Springfield, IL", "Main St, Springfield, IL")
    assert r.decision == "REVIEW" and "Building number missing from AddressB" in r.explanation


def test_missing_address(matcher):
    r = decision(matcher, "12 Main St", None)
    assert r.result_text == "REVIEW | Score: N/A | AddressB is empty; unable to compare."


@pytest.mark.parametrize(
    "a,b",
    [
        ("Hauptstraße 5, 10115 Berlin", "Hauptstr. 5, 10115 Berlin, Germany"),
        ("15 rue de la Paix, 75002 Paris", "15 Rue de la Paix 75002 Paris France"),
        ("221B Baker Street, London NW1 6XE, UK", "221B Baker St, London NW16XE"),
        ("Flat 4B, 12 M.G. Road, Indiranagar, Bengaluru 560038", "Flat 4B, 12 MG Rd, Indiranagar, Bengaluru - 560038"),
    ],
)
def test_international_matches(matcher, a, b):
    assert decision(matcher, a, b).decision == "MATCH"


def test_international_number_conflict(matcher):
    r = decision(matcher, "Hauptstraße 5, 10115 Berlin", "Hauptstraße 7, 10115 Berlin")
    assert r.decision == "NON-MATCH"


def test_deterministic_with_real_libpostal(matcher):
    a, b = "350 5th Ave, New York, NY 10118", "350 Fifth Avenue, New York NY 10118"
    assert len({decision(matcher, a, b).result_text for _ in range(3)}) == 1
