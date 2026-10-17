"""Mapping of libpostal parser labels to comparison fields, plus value canonicalizers.

libpostal labels: https://github.com/openvenues/libpostal#parser-labels
"""

from __future__ import annotations

import re
import unicodedata

# libpostal label -> our comparison field. Several labels may feed one field.
LABEL_TO_FIELD: dict[str, str] = {
    "house": "building_name",
    "house_number": "house_number",
    "road": "road",
    "unit": "unit",
    "level": "unit",
    "staircase": "unit",
    "entrance": "unit",
    "po_box": "po_box",
    "postcode": "postcode",
    "suburb": "locality",
    "city_district": "locality",
    "city": "city",
    "state_district": "district",
    "state": "state",
    "country_region": "country_region",
    "island": "island",
    "country": "country",
    "world_region": "world_region",
    "category": "category",
    "near": "near",
}

# Order used when building the human-readable normalized string.
FIELD_ORDER: tuple[str, ...] = (
    "building_name",
    "house_number",
    "road",
    "unit",
    "po_box",
    "locality",
    "city",
    "district",
    "island",
    "state",
    "country_region",
    "postcode",
    "country",
    "world_region",
    "category",
    "near",
)

FIELD_LABELS: dict[str, str] = {
    "house_number": "Building number",
    "road": "Street",
    "unit": "Unit",
    "building_name": "Building name",
    "po_box": "PO box",
    "postcode": "Postal code",
    "locality": "Locality",
    "city": "City",
    "district": "District",
    "state": "State/region",
    "country": "Country",
}

# Fields compared as identifiers rather than fuzzy text.
NUMBER_FIELDS = frozenset({"house_number", "po_box"})
UNIT_FIELDS = frozenset({"unit"})
POSTCODE_FIELDS = frozenset({"postcode"})

# Words that designate a unit but are not part of its identifier.
UNIT_DESIGNATORS = frozenset(
    {
        "apartment", "apt", "unit", "suite", "ste", "flat", "floor", "fl", "level",
        "lvl", "room", "rm", "office", "ofc", "building", "bldg", "block", "blk",
        "tower", "wing", "staircase", "entrance", "door", "no", "number", "num",
        "house", "plot", "shop", "basement", "lot", "#",
    }
)

# Final-word street types used only to choose the displayed expansion.
STREET_TYPES = frozenset(
    {
        "street", "road", "avenue", "boulevard", "lane", "drive", "court", "place", "terrace",
        "way", "highway", "parkway", "circle", "square", "crescent", "close", "marg", "nagar",
        "path", "alley", "row", "walk", "trail", "expressway", "freeway", "gardens", "grove",
        "hill", "mews", "park", "plaza", "quay", "rise", "view", "strasse", "gasse", "weg",
        "platz", "rue", "calle", "via", "straat", "laan",
    }
)

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


_HOUSE_NOISE = frozenset({"no", "number", "num", "nr"})
_PO_BOX_NOISE = _HOUSE_NOISE | {"box", "po", "p", "o", "pob", "post", "office", "postbox"}


def canonical_identifier(value: str, *, po_box: bool = False) -> str:
    """Canonical form for house numbers / PO boxes: lowercase alphanumerics joined by '-'.

    "No. 12-A" -> "12-a", "12 A" -> "12-a", "#12" -> "12", "P.O. Box 45" -> "45" (po_box=True).
    """
    noise = _PO_BOX_NOISE if po_box else _HOUSE_NOISE
    tokens = [t for t in _NON_ALNUM.split(strip_accents(value).lower()) if t and t not in noise]
    return "-".join(tokens)


def digits_of(value: str) -> str:
    return "".join(re.findall(r"\d+", value))


def unit_identifiers(value: str) -> frozenset[str]:
    """Identifier tokens of a unit after removing designator words.

    "apartment 4b" -> {"4b"}, "apt #4-B" -> {"4b"}, "floor 2 unit 7" -> {"2", "7"}.
    Letter-number splits such as "4 b" are re-joined.
    """
    text = strip_accents(value).lower().replace("#", " # ")
    text = re.sub(r"(\d)\s*-\s*([a-z])\b", r"\1\2", text)
    text = re.sub(r"\b(\d+)\s+([a-z])\b", r"\1\2", text)
    tokens = [t for t in _NON_ALNUM.split(text) if t and t not in UNIT_DESIGNATORS]
    return frozenset(tokens)


def canonical_postcode(value: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", strip_accents(value).upper())


def numeric_tokens(text: str) -> frozenset[str]:
    """Numbers in a street name ("5th avenue" -> {"5"}) for detecting 5th vs 6th Ave."""
    out = set()
    for tok in _NON_ALNUM.split(strip_accents(text).lower()):
        m = _ORDINAL.match(tok)
        if m:
            out.add(m.group(1))
        elif tok.isdigit():
            out.add(tok)
    return frozenset(out)
