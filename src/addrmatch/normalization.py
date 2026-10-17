"""Address normalization with libpostal.

Normalization rewrites an address into a canonical form (lower-case, expanded
abbreviations, labelled components). It does NOT verify that an address exists
and never adds components that are not in the input text.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .components import (
    FIELD_ORDER,
    LABEL_TO_FIELD,
    NUMBER_FIELDS,
    POSTCODE_FIELDS,
    STREET_TYPES,
    canonical_identifier,
)

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")


class LibpostalUnavailableError(RuntimeError):
    """Raised when the libpostal C library, its data or the Python binding is missing."""


class NormStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


class NormalizerBackend(Protocol):
    """What the matcher needs from an address library."""

    name: str

    def parse(self, text: str) -> list[tuple[str, str]]:
        """Return (value, label) pairs."""

    def expand(self, text: str, field: str | None = None) -> list[str]:
        """Return normalized expansions. `field` lets the backend apply only the
        abbreviation dictionaries relevant to that component (e.g. street types)."""


class LibpostalBackend:
    """Thin wrapper around the `postal` Python bindings (pypostal)."""

    name = "libpostal"

    def __init__(self, languages: tuple[str, ...] = ()) -> None:
        try:
            from postal.expand import expand_address  # type: ignore[import-not-found]
            from postal.parser import parse_address  # type: ignore[import-not-found]
        except Exception as exc:  # ImportError or a failing native load
            raise LibpostalUnavailableError(
                "libpostal is not available. Install the native libpostal library and its "
                "data files, then `pip install postal`. See README section C."
            ) from exc
        import postal.expand as pe  # type: ignore[import-not-found]

        self._expand_fn = expand_address
        self._parse_fn = parse_address
        toponym = pe.ADDRESS_TOPONYM
        self._field_masks = {
            "road": pe.ADDRESS_STREET,
            "unit": pe.ADDRESS_UNIT | pe.ADDRESS_LEVEL | pe.ADDRESS_STAIRCASE | pe.ADDRESS_ENTRANCE,
            "building_name": pe.ADDRESS_NAME,
            "locality": toponym, "city": toponym, "district": toponym, "state": toponym,
            "country": toponym, "island": toponym, "country_region": toponym,
        }
        self._languages = list(languages)
        # libpostal is loaded once per process; serialize calls to be safe.
        self._lock = threading.Lock()

    def parse(self, text: str) -> list[tuple[str, str]]:
        with self._lock:
            return [(str(v), str(l)) for v, l in self._parse_fn(text)]

    def expand(self, text: str, field: str | None = None) -> list[str]:
        kwargs: dict[str, Any] = {}
        if self._languages:
            kwargs["languages"] = self._languages
        if field in self._field_masks:
            kwargs["address_components"] = self._field_masks[field]
        with self._lock:
            return list(self._expand_fn(text, **kwargs))


_backend_lock = threading.Lock()
_default_backend: LibpostalBackend | None = None


def get_libpostal_backend(languages: tuple[str, ...] = ()) -> LibpostalBackend:
    """Process-wide libpostal backend (its model takes ~2 GB RAM and seconds to load)."""
    global _default_backend
    with _backend_lock:
        if _default_backend is None:
            _default_backend = LibpostalBackend(languages)
            _default_backend.expand("1 test street")  # force model load now, not mid-job
        return _default_backend


def libpostal_status() -> tuple[bool, str]:
    """(available, message) without raising."""
    try:
        from postal.parser import parse_address  # type: ignore[import-not-found]  # noqa: F401
    except Exception as exc:
        return False, f"libpostal Python binding could not be imported ({type(exc).__name__})."
    return True, "libpostal binding found."


@dataclass(frozen=True)
class NormalizedAddress:
    """Result of normalizing one address string."""

    status: NormStatus
    normalized: str = ""
    # field -> candidate canonical forms (first is the display form)
    components: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # raw libpostal (label, value) pairs, for debugging and audits
    parsed: tuple[tuple[str, str], ...] = ()
    error: str | None = None

    def value(self, fld: str) -> str | None:
        vals = self.components.get(fld)
        return vals[0] if vals else None

    def has(self, fld: str) -> bool:
        return bool(self.components.get(fld))


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def clean_text(value: Any) -> str:
    """Remove control characters and collapse whitespace (input to libpostal only)."""
    return _WS.sub(" ", _CONTROL.sub(" ", str(value))).strip()


def _dedupe(seq: list[str]) -> tuple[str, ...]:
    return tuple(OrderedDict.fromkeys(s for s in seq if s))


def normalize_address(
    value: Any,
    backend: NormalizerBackend,
    *,
    max_chars: int = 500,
    max_expansions: int = 5,
) -> NormalizedAddress:
    """Normalize one address. Never raises: failures are returned with status FAILED."""
    if is_blank(value):
        return NormalizedAddress(status=NormStatus.EMPTY)
    text = clean_text(value)
    if len(text) > max_chars:
        return NormalizedAddress(
            status=NormStatus.FAILED, error=f"address longer than {max_chars} characters"
        )
    try:
        parsed = backend.parse(text)
    except Exception as exc:
        log.warning("libpostal parse failed (%s)", type(exc).__name__)
        return NormalizedAddress(status=NormStatus.FAILED, error=f"parser error: {type(exc).__name__}")

    grouped: dict[str, list[str]] = {}
    for val, label in parsed:
        val = val.strip()
        if not val:
            continue
        fld = LABEL_TO_FIELD.get(label, label)
        grouped.setdefault(fld, []).append(val)
    if not grouped:
        return NormalizedAddress(status=NormStatus.FAILED, error="no address components recognised")

    components: dict[str, tuple[str, ...]] = {}
    try:
        for fld, vals in grouped.items():
            joined = " ".join(vals)
            if fld in POSTCODE_FIELDS:
                variants = [joined.lower()]  # compared via canonical_postcode()
            elif fld == "po_box":
                ident = canonical_identifier(joined, po_box=True)
                variants = [f"po box {ident}" if ident else joined.lower()]
            elif fld in NUMBER_FIELDS:
                variants = [canonical_identifier(joined) or joined.lower()]
            else:
                expansions = backend.expand(joined, fld)[:max_expansions]
                variants = prefer_display_form(fld, [e.strip().lower() for e in expansions]) or [joined.lower()]
                variants.append(joined.lower())  # parser's own form is also a candidate
            components[fld] = _dedupe(variants)
    except Exception as exc:
        log.warning("libpostal expand failed (%s)", type(exc).__name__)
        return NormalizedAddress(status=NormStatus.FAILED, error=f"expansion error: {type(exc).__name__}")

    return NormalizedAddress(
        status=NormStatus.OK,
        normalized=build_normalized_string(components),
        components=components,
        parsed=tuple((label, val) for val, label in parsed),
    )


def prefer_display_form(fld: str, variants: list[str]) -> list[str]:
    """Reorder expansions so the most plausible one is shown first.

    libpostal returns all readings of an ambiguous abbreviation ("main st" ->
    "main saint", "main street"). For streets, a reading that ends in a street
    type is preferred. All readings are still used for comparison.
    """
    if fld != "road" or len(variants) < 2:
        return variants
    ranked = sorted(
        enumerate(variants),
        key=lambda iv: (iv[1].split()[-1] not in STREET_TYPES if iv[1].split() else True, iv[0]),
    )
    return [v for _, v in ranked]


def build_normalized_string(components: dict[str, tuple[str, ...]]) -> str:
    """Comma-separated canonical string in a fixed component order."""
    first = {k: v[0] for k, v in components.items() if v}
    parts: list[str] = []
    for fld in FIELD_ORDER:
        if fld not in first:
            continue
        if fld == "road" and "house_number" in first:
            continue  # emitted together with the house number
        if fld == "house_number":
            parts.append(" ".join(x for x in (first["house_number"], first.get("road")) if x))
        else:
            parts.append(first[fld])
    parts.extend(v for k, v in first.items() if k not in FIELD_ORDER)
    return ", ".join(parts)


class CachingNormalizer:
    """Memoizes normalization so duplicate addresses in a file are processed once."""

    def __init__(
        self,
        backend: NormalizerBackend,
        *,
        max_chars: int = 500,
        max_expansions: int = 5,
        cache_size: int = 50_000,
    ) -> None:
        self.backend = backend
        self.max_chars = max_chars
        self.max_expansions = max_expansions
        self._cache: OrderedDict[str, NormalizedAddress] = OrderedDict()
        self._cache_size = cache_size

    def __call__(self, value: Any) -> NormalizedAddress:
        if is_blank(value):
            return NormalizedAddress(status=NormStatus.EMPTY)
        key = clean_text(value)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        result = normalize_address(
            key, self.backend, max_chars=self.max_chars, max_expansions=self.max_expansions
        )
        self._cache[key] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result
