from __future__ import annotations

import builtins
import math

import pytest

from addrmatch import normalization
from addrmatch.matcher import AddressMatcher
from addrmatch.normalization import (
    CachingNormalizer,
    LibpostalBackend,
    LibpostalUnavailableError,
    NormStatus,
    normalize_address,
)


@pytest.mark.parametrize("value", [None, "", "   ", float("nan")])
def test_blank_values_are_empty(value, fake_backend):
    r = normalize_address(value, fake_backend)
    assert r.status is NormStatus.EMPTY and r.normalized == ""
    assert fake_backend.parse_calls == 0


def test_backend_exception_is_recorded_not_raised():
    from conftest import FakeBackend

    backend = FakeBackend(fail_on={"12 Main St"})
    r = normalize_address("12 Main St", backend)
    assert r.status is NormStatus.FAILED
    assert "RuntimeError" in r.error


def test_overlong_address_fails_transparently(fake_backend):
    r = normalize_address("x" * 600, fake_backend, max_chars=500)
    assert r.status is NormStatus.FAILED and "500" in r.error


def test_normalization_does_not_invent_components(fake_backend):
    r = normalize_address("12 Main St", fake_backend)
    assert set(r.components) == {"house_number", "road"}
    assert r.normalized == "12 main street"


def test_caching_normalizer_parses_duplicates_once(fake_backend):
    n = CachingNormalizer(fake_backend)
    n("12 Main St, Springfield, IL 62701")
    n("12 Main St, Springfield, IL 62701")
    n("  12 Main St,  Springfield, IL 62701 ")  # same after whitespace cleanup
    assert fake_backend.parse_calls == 1


def test_libpostal_unavailable_raises_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("postal"):
            raise ImportError("No module named 'postal'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LibpostalUnavailableError, match="libpostal is not available"):
        LibpostalBackend()
    ok, msg = normalization.libpostal_status()
    assert ok is False and "could not be imported" in msg


def test_failed_normalization_row_is_review_na(cfg):
    from conftest import FakeBackend

    m = AddressMatcher(CachingNormalizer(FakeBackend(fail_on={"bad"})), cfg)
    r = m.match("12 Main St, Springfield, IL 62701", "bad")
    assert r.result_text.startswith("REVIEW | Score: N/A | Normalization failed for AddressB")


def test_empty_rows_result_format(cfg, fake_backend):
    m = AddressMatcher(CachingNormalizer(fake_backend), cfg)
    assert m.match(None, "12 Main St").result_text == "REVIEW | Score: N/A | AddressA is empty; unable to compare."
    assert m.match("", math.nan).explanation == "AddressA and AddressB are empty; unable to compare."


def test_safe_match_never_raises(cfg, fake_backend):
    m = AddressMatcher(CachingNormalizer(fake_backend), cfg)

    def boom(*_a, **_k):
        raise ValueError("unexpected")

    m.match = boom  # type: ignore[method-assign]
    r = m.safe_match("a", "b")
    assert r.decision == "REVIEW" and r.score is None and r.error == "ValueError"
