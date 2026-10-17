"""Follow-up questions about a completed job.

Every answer is computed from the stored job results. A keyword router handles
the supported questions deterministically; if an LLM is configured it is used
only as a fallback to map an unrecognised phrasing onto one of the same
supported intents. Unsupported requests are declined rather than guessed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from .excel_io import output_bytes
from .explanation import label, reason_label
from .job_store import JobStore

DECISIONS = ("MATCH", "NON-MATCH", "REVIEW")
FIELD_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("house_number", ("building number", "house number", "street number", "door number", "house no", "building no")),
    ("unit", ("unit", "apartment", "apt", "flat", "suite")),
    ("postcode", ("postal code", "postcode", "post code", "zip", "pin code", "pincode")),
    ("po_box", ("po box", "p.o. box")),
    ("road", ("street", "road")),
    ("city", ("city", "town")),
    ("state", ("state", "region", "province")),
    ("country", ("country",)),
    ("locality", ("locality", "neighborhood", "neighbourhood", "suburb", "area")),
    ("building_name", ("building name",)),
]
PREVIEW_COLUMNS = ["excel_row", "AddressA", "AddressB", "Result"]
MAX_TABLE_ROWS = 200

HELP_TEXT = (
    "I can answer questions about this job's stored results, for example:\n"
    "- *How many rows are MATCH, NON-MATCH and REVIEW?*\n"
    "- *Show rows where building numbers differ* (also unit, postal code, street, city…)\n"
    "- *Show rows with missing addresses* / *Show normalization failures*\n"
    "- *What are the most common reasons for non-matches?*\n"
    "- *Show REVIEW rows* / *Export only REVIEW rows*"
)


@dataclass
class ChatResponse:
    text: str
    table: pd.DataFrame | None = None
    file_bytes: bytes | None = None
    file_name: str | None = None
    intent: dict[str, Any] = field(default_factory=dict)


def _decision_in(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(non[\s-]?match(es|ed|ing)?|nonmatch(es)?|no[\s-]match(es)?|unmatched|mismatch(es|ed)?)\b", t):
        return "NON-MATCH"
    if re.search(r"\breview", t):
        return "REVIEW"
    if re.search(r"\bmatch(es|ed)?\b", t):
        return "MATCH"
    return None


def _field_in(text: str) -> str | None:
    t = text.lower()
    for fld, words in FIELD_KEYWORDS:
        if any(re.search(rf"\b{re.escape(w)}", t) for w in words):
            return fld
    return None


def route_question(question: str) -> dict[str, Any] | None:
    """Deterministic keyword routing. Returns None when the question is not recognised."""
    t = question.lower().strip()
    decision = _decision_in(t)
    fld = _field_in(t)
    if re.search(r"\b(export|download|save|file|excel|xlsx)\b", t) and decision:
        return {"intent": "export_decision", "decision": decision}
    if re.search(r"\b(missing|empty|blank)\b", t) and re.search(r"\baddress", t) and not fld:
        return {"intent": "missing_addresses"}
    if re.search(r"\b(fail|failed|failure|failures|error|errors)\b", t) and not re.search(r"\bhow many\b", t):
        return {"intent": "normalization_failures"}
    if re.search(r"\b(reason|reasons|why|cause|causes)\b", t):
        return {"intent": "top_reasons", "decision": decision}
    if fld and re.search(r"\b(differ|different|differs|conflict|conflicting|mismatch|mismatched|disagree)", t):
        return {"intent": "field_conflict", "field": fld}
    if re.search(r"\b(how many|count|counts|summary|summarise|summarize|breakdown|totals?|statistics|stats)\b", t):
        return {"intent": "summary"}
    if decision and re.search(r"\b(show|list|display|which|see|give)\b", t):
        return {"intent": "show_decision", "decision": decision}
    if re.search(r"\b(help|what can you)\b", t):
        return {"intent": "help"}
    return None


class FollowUpAssistant:
    def __init__(
        self,
        store: JobStore,
        *,
        llm_classifier: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.llm_classifier = llm_classifier
        self._df: pd.DataFrame | None = None

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            self._df = self.store.results_frame()
        return self._df

    def answer(self, question: str) -> ChatResponse:
        if self.store.meta().status != "completed":
            return ChatResponse("The job has not completed yet, so there are no results to query.")
        intent = route_question(question)
        if intent is None and self.llm_classifier is not None:
            try:
                intent = self._validate(self.llm_classifier(question))
            except Exception:
                intent = None
        if intent is None or intent.get("intent") in (None, "unsupported"):
            return ChatResponse("I can't answer that from the stored job results.\n\n" + HELP_TEXT, intent=intent or {})
        handler = getattr(self, f"_h_{intent['intent']}", None)
        if handler is None:
            return ChatResponse(HELP_TEXT, intent=intent)
        resp: ChatResponse = handler(intent)
        resp.intent = intent
        return resp

    @staticmethod
    def _validate(raw: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"summary", "show_decision", "export_decision", "field_conflict",
                   "missing_addresses", "normalization_failures", "top_reasons", "help", "unsupported"}
        intent = raw.get("intent")
        if intent not in allowed:
            return None
        out: dict[str, Any] = {"intent": intent}
        if raw.get("decision") in DECISIONS:
            out["decision"] = raw["decision"]
        if raw.get("field") in {f for f, _ in FIELD_KEYWORDS}:
            out["field"] = raw["field"]
        if intent in ("show_decision", "export_decision") and "decision" not in out:
            return None
        if intent == "field_conflict" and "field" not in out:
            return None
        return out

    # ---- handlers -----------------------------------------------------
    def _table(self, rows: pd.DataFrame) -> pd.DataFrame:
        return rows[PREVIEW_COLUMNS].head(MAX_TABLE_ROWS).rename(columns={"excel_row": "Sheet row"})

    def _rows_text(self, n: int, what: str) -> str:
        if n == 0:
            return f"No rows {what}."
        shown = f" (showing the first {MAX_TABLE_ROWS})" if n > MAX_TABLE_ROWS else ""
        return f"{n} row{'s' if n != 1 else ''} {what}{shown}."

    def _h_help(self, _: dict[str, Any]) -> ChatResponse:
        return ChatResponse(HELP_TEXT)

    def _h_summary(self, _: dict[str, Any]) -> ChatResponse:
        df = self.df
        counts = df["decision"].value_counts()
        errors = int(((df["status_a"] == "failed") | (df["status_b"] == "failed") | df["error"].notna()).sum())
        lines = [f"**{len(df)}** rows processed:"]
        for d in DECISIONS:
            n = int(counts.get(d, 0))
            pct = f" ({n / len(df):.0%})" if len(df) else ""
            lines.append(f"- {d}: {n}{pct}")
        lines.append(f"- Normalization failures / row errors (included in REVIEW): {errors}")
        return ChatResponse("\n".join(lines))

    def _h_show_decision(self, intent: dict[str, Any]) -> ChatResponse:
        rows = self.df[self.df["decision"] == intent["decision"]]
        return ChatResponse(self._rows_text(len(rows), f"are {intent['decision']}"), self._table(rows) if len(rows) else None)

    def _h_field_conflict(self, intent: dict[str, Any]) -> ChatResponse:
        fld = intent["field"]
        rows = self.df[self.df["conflicts"].map(lambda c: fld in c)]
        return ChatResponse(
            self._rows_text(len(rows), f"where the {label(fld).lower()} differs between AddressA and AddressB"),
            self._table(rows) if len(rows) else None,
        )

    def _h_missing_addresses(self, _: dict[str, Any]) -> ChatResponse:
        rows = self.df[(self.df["status_a"] == "empty") | (self.df["status_b"] == "empty")]
        return ChatResponse(self._rows_text(len(rows), "with an empty AddressA or AddressB"), self._table(rows) if len(rows) else None)

    def _h_normalization_failures(self, _: dict[str, Any]) -> ChatResponse:
        df = self.df
        rows = df[(df["status_a"] == "failed") | (df["status_b"] == "failed") | df["error"].notna()]
        return ChatResponse(self._rows_text(len(rows), "with a normalization failure or processing error"), self._table(rows) if len(rows) else None)

    def _h_top_reasons(self, intent: dict[str, Any]) -> ChatResponse:
        decision = intent.get("decision") or "NON-MATCH"
        rows = self.df[self.df["decision"] == decision]
        if rows.empty:
            return ChatResponse(f"There are no {decision} rows in this job.")
        counter = Counter(code for codes in rows["reason_codes"] for code in codes)
        table = pd.DataFrame(
            [(reason_label(code), n) for code, n in counter.most_common(10)], columns=["Reason", "Rows"]
        )
        top = table.iloc[0]
        return ChatResponse(
            f"Most common reasons among {len(rows)} {decision} rows (a row can have several). "
            f"Top: **{top['Reason']}** ({top['Rows']} rows).",
            table,
        )

    def _h_export_decision(self, intent: dict[str, Any]) -> ChatResponse:
        decision = intent["decision"]
        data, n = output_bytes(self.store.iter_output_rows(decision=decision))
        if n == 0:
            return ChatResponse(f"There are no {decision} rows to export.")
        name = f"{self.store.meta().id}_{decision.lower().replace('-', '_')}_rows.xlsx"
        return ChatResponse(
            f"Created an export with {n} {decision} row{'s' if n != 1 else ''} (same five columns, original order).",
            file_bytes=data,
            file_name=name,
        )
