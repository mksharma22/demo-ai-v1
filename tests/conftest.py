"""Shared fixtures.

Two kinds of tests:
* Unit/workflow tests use `FakeBackend`, a tiny deterministic stand-in for
  libpostal so the decision logic, Excel I/O and orchestration can be tested
  anywhere. It is NOT a real address parser.
* tests/test_libpostal_integration.py runs the same scenarios through the real
  libpostal and is skipped when libpostal is not installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from addrmatch.normalization import NormalizedAddress, NormStatus  # noqa: E402
from addrmatch.scoring_config import load_scoring_config  # noqa: E402
from addrmatch.settings import Settings  # noqa: E402

ABBREV = {"st": "street", "ave": "avenue", "rd": "road", "apt": "apartment"}


class FakeBackend:
    """Parses 'NUMBER ROAD [apt X], CITY, STATE POSTCODE'. Test-only."""

    name = "fake"

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.parse_calls = 0

    def parse(self, text: str) -> list[tuple[str, str]]:
        self.parse_calls += 1
        if text in self.fail_on:
            raise RuntimeError("simulated libpostal failure")
        parts = [p.strip().lower() for p in text.split(",") if p.strip()]
        out: list[tuple[str, str]] = []
        if not parts:
            return out
        first = parts[0]
        m = re.match(r"^(\d+\w?)\s+(.*)$", first)
        street = first
        if m:
            out.append((m.group(1), "house_number"))
            street = m.group(2)
        um = re.search(r"\b(apt|apartment|unit)\s+(\w+)$", street)
        if um:
            street = street[: um.start()].strip()
            out.append((street, "road"))
            out.append((um.group(0), "unit"))
        else:
            out.append((street, "road"))
        if len(parts) > 1:
            out.append((parts[1], "city"))
        if len(parts) > 2:
            sm = re.match(r"^([a-z]+)\s*(\d{5})?$", parts[2])
            if sm:
                out.append((sm.group(1), "state"))
                if sm.group(2):
                    out.append((sm.group(2), "postcode"))
        return out

    def expand(self, text: str, field: str | None = None) -> list[str]:
        words = re.sub(r"[^\w\s]", " ", text.lower()).split()
        return [" ".join(ABBREV.get(w, w) for w in words)]


@pytest.fixture
def cfg():
    return load_scoring_config(ROOT / "config" / "scoring.toml")


@pytest.fixture
def fake_backend():
    return FakeBackend()


def make_norm(**fields: str) -> NormalizedAddress:
    """Build a NormalizedAddress directly from component values (unit tests)."""
    comps = {k: (v,) for k, v in fields.items()}
    return NormalizedAddress(status=NormStatus.OK, normalized=", ".join(fields.values()), components=comps)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "jobs",
        scoring_config_path=ROOT / "config" / "scoring.toml",
        max_upload_mb=5,
        max_uncompressed_mb=50,
        max_rows=1000,
        batch_size=3,
        max_concurrent_jobs=1,
        job_retention_hours=24,
        max_address_chars=500,
        libpostal_languages=(),
        log_level="INFO",
        llm_api_key="",
        llm_base_url="http://localhost",
        llm_model="",
        llm_timeout_s=5,
    )


def write_xlsx(path: Path, header: list[str] | None, rows: list[list[object]]) -> Path:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    if header is not None:
        ws.append(header)
    for r in rows:
        cells = []
        for v in r:
            c = WriteOnlyCell(ws, value=v)
            if isinstance(v, str):
                c.data_type = "s"
            cells.append(c)
        ws.append(cells)
    wb.save(path)
    return path


@pytest.fixture(scope="session")
def libpostal_backend():
    try:
        from addrmatch.normalization import get_libpostal_backend

        return get_libpostal_backend()
    except Exception as exc:  # LibpostalUnavailableError or native load error
        pytest.skip(f"libpostal not available: {exc}")
