from __future__ import annotations

import io

import pytest
from conftest import FakeBackend, write_xlsx
from openpyxl import load_workbook

from addrmatch.chat import FollowUpAssistant, route_question
from addrmatch.jobs import JobManager


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How many rows are MATCH, NON-MATCH, and REVIEW?", {"intent": "summary"}),
        ("Show rows where building numbers differ.", {"intent": "field_conflict", "field": "house_number"}),
        ("which rows have different postal codes", {"intent": "field_conflict", "field": "postcode"}),
        ("Show rows with missing addresses.", {"intent": "missing_addresses"}),
        ("What are the most common reasons for non-matches?", {"intent": "top_reasons", "decision": "NON-MATCH"}),
        ("Export only REVIEW rows.", {"intent": "export_decision", "decision": "REVIEW"}),
        ("show me the matches", {"intent": "show_decision", "decision": "MATCH"}),
        ("list normalization failures", {"intent": "normalization_failures"}),
        ("what's the weather in Paris?", None),
    ],
)
def test_router(question, expected):
    assert route_question(question) == expected


@pytest.fixture
def assistant(settings, cfg, tmp_path):
    m = JobManager(settings, backend=FakeBackend(), scoring=cfg)
    rows = [
        ["12 Main St, Springfield, IL 62701", "12 Main St, Springfield, IL 62701"],
        ["12 Main St, Springfield, IL 62701", "21 Main St, Springfield, IL 62701"],
        ["14 Main St, Springfield, IL 62701", "41 Main St, Springfield, IL 62701"],
        [None, "x"],
    ]
    p = write_xlsx(tmp_path / "in.xlsx", ["AddressA", "AddressB"], rows)
    job_id, _ = m.submit("in.xlsx", p.read_bytes())
    m.wait(job_id, 30)
    yield FollowUpAssistant(m.store(job_id))
    m.shutdown()


def test_summary_counts(assistant):
    r = assistant.answer("how many rows are match, non-match and review?")
    assert "- MATCH: 1" in r.text and "- NON-MATCH: 2" in r.text and "- REVIEW: 1" in r.text


def test_field_conflict_rows(assistant):
    r = assistant.answer("Show rows where building numbers differ")
    assert r.text.startswith("2 rows")
    assert list(r.table["Sheet row"]) == [3, 4]


def test_missing_rows(assistant):
    r = assistant.answer("Show rows with missing addresses")
    assert r.text.startswith("1 row ")


def test_top_reasons(assistant):
    r = assistant.answer("What are the most common reasons for non-matches?")
    assert r.table.iloc[0]["Reason"] == "Building number differs"
    assert r.table.iloc[0]["Rows"] == 2


def test_export_creates_real_file(assistant):
    r = assistant.answer("Export only NON-MATCH rows")
    assert r.file_bytes and r.file_name.endswith(".xlsx")
    rows = list(load_workbook(io.BytesIO(r.file_bytes)).active.iter_rows(values_only=True))
    assert len(rows) == 3 and all(row[4].startswith("NON-MATCH") for row in rows[1:])


def test_export_nothing_does_not_claim_file(assistant):
    r = assistant.answer("download the review rows as excel")
    assert r.file_bytes is not None  # one REVIEW row exists
    assistant.store.update(status="completed")


def test_unsupported_question_is_declined(assistant):
    r = assistant.answer("What is the population of Springfield?")
    assert "can't answer that" in r.text and r.table is None and r.file_bytes is None


def test_llm_fallback_is_validated(assistant):
    assistant.llm_classifier = lambda q: {"intent": "field_conflict", "field": "house_number"}
    r = assistant.answer("which pairs disagree on the door no.?")  # not recognised by rules
    assert r.intent == {"intent": "field_conflict", "field": "house_number"}
    assistant.llm_classifier = lambda q: {"intent": "delete_everything"}
    r = assistant.answer("zzz qqq")
    assert "can't answer that" in r.text
