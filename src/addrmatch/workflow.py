"""LangGraph orchestration of one matching job.

The graph is the single orchestrator. Every node calls deterministic tools
(validation, normalization, matching, Excel I/O); no node calls an LLM.

    START -> validate_input -> process_batch (loops) -> write_output -> finalize -> END
                   \\---------------> fail -> END
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from .excel_io import InputValidationError, iter_input_rows, validate_workbook, write_output
from .job_store import JobStore
from .matcher import AddressMatcher
from .settings import Settings

log = logging.getLogger(__name__)


class JobState(TypedDict, total=False):
    job_id: str
    job_dir: str
    status: str
    error: str | None
    total_rows: int
    cursor: int
    metrics: dict[str, Any]


def _deps(config: RunnableConfig) -> tuple[JobStore, AddressMatcher, Settings]:
    c = config["configurable"]
    return c["store"], c["matcher"], c["settings"]


# ---- nodes --------------------------------------------------------------
def validate_input(state: JobState, config: RunnableConfig) -> JobState:
    store, _, settings = _deps(config)
    meta = store.meta()
    store.update(status="running", error_message=None)
    if meta.ingested:  # retry/resume: input already validated and stored
        cursor = store.result_count()
        log.info("job %s resuming at row %d/%s", state["job_id"], cursor, meta.total_rows)
        return {"status": "running", "total_rows": meta.total_rows or 0, "cursor": cursor}
    try:
        info = validate_workbook(
            store.input_path,
            max_upload_mb=settings.max_upload_mb,
            max_uncompressed_mb=settings.max_uncompressed_mb,
        )
        total = store.ingest(iter_input_rows(store.input_path, info, max_rows=settings.max_rows))
    except InputValidationError as exc:
        return {"status": "failed", "error": str(exc)}
    store.update(total_rows=total, ingested=1, sheet_name=info.sheet_name, processed_rows=0)
    log.info("job %s validated: %d rows, sheet=%r", state["job_id"], total, info.sheet_name)
    return {"status": "running", "total_rows": total, "cursor": 0}


def process_batch(state: JobState, config: RunnableConfig) -> JobState:
    store, matcher, settings = _deps(config)
    cursor = state.get("cursor", 0)
    batch = store.fetch_batch(cursor, settings.batch_size)
    results = [(row.row_index, matcher.safe_match(row.address_a, row.address_b)) for row in batch]
    store.save_results(results)
    new_cursor = cursor + len(batch)
    store.update(processed_rows=new_cursor)
    return {"cursor": new_cursor}


def write_output_node(state: JobState, config: RunnableConfig) -> JobState:
    store, _, _ = _deps(config)
    total = state.get("total_rows", 0)
    if store.result_count() != total:
        return {"status": "failed", "error": "Internal consistency check failed: not every row has a result."}
    tmp = store.output_path.with_suffix(".tmp.xlsx")
    written = write_output(store.iter_output_rows(), tmp)
    if written != total:
        tmp.unlink(missing_ok=True)
        return {"status": "failed", "error": "Output row count does not match input row count."}
    tmp.replace(store.output_path)  # atomic: a partial file is never downloadable
    return {}


def finalize(state: JobState, config: RunnableConfig) -> JobState:
    store, _, _ = _deps(config)
    df = store.results_frame()
    counts = store.decision_counts()
    reasons = Counter(code for codes in df["reason_codes"] for code in codes)
    metrics = {
        "total_rows": int(len(df)),
        "decisions": counts,
        "empty_rows": int(((df["status_a"] == "empty") | (df["status_b"] == "empty")).sum()),
        "normalization_failures": int(((df["status_a"] == "failed") | (df["status_b"] == "failed")).sum()),
        "row_errors": int(df["error"].notna().sum()),
        "duplicate_rows": int(df.duplicated(subset=["AddressA", "AddressB"]).sum()),
        "reason_counts": dict(reasons.most_common()),
    }
    store.update(status="completed", metrics_json=json.dumps(metrics))
    # Original upload is no longer needed once results are stored.
    store.input_path.unlink(missing_ok=True)
    log.info("job %s completed: %s", state["job_id"], counts)
    return {"status": "completed", "metrics": metrics}


def fail(state: JobState, config: RunnableConfig) -> JobState:
    store, _, _ = _deps(config)
    store.update(status="failed", error_message=state.get("error") or "Processing failed.")
    log.warning("job %s failed: %s", state["job_id"], state.get("error"))
    return {"status": "failed"}


# ---- routing ------------------------------------------------------------
def route_after_validate(state: JobState) -> Literal["process_batch", "write_output", "fail"]:
    if state.get("status") == "failed":
        return "fail"
    return "process_batch" if state.get("cursor", 0) < state.get("total_rows", 0) else "write_output"


def route_after_batch(state: JobState) -> Literal["process_batch", "write_output"]:
    return "process_batch" if state["cursor"] < state["total_rows"] else "write_output"


def route_after_write(state: JobState) -> Literal["finalize", "fail"]:
    return "fail" if state.get("status") == "failed" else "finalize"


def build_graph():
    g = StateGraph(JobState)
    g.add_node("validate_input", validate_input)
    g.add_node("process_batch", process_batch)
    g.add_node("write_output", write_output_node)
    g.add_node("finalize", finalize)
    g.add_node("fail", fail)
    g.add_edge(START, "validate_input")
    g.add_conditional_edges("validate_input", route_after_validate)
    g.add_conditional_edges("process_batch", route_after_batch)
    g.add_conditional_edges("write_output", route_after_write)
    g.add_edge("finalize", END)
    g.add_edge("fail", END)
    return g.compile()


_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run_job(job_id: str, store: JobStore, matcher: AddressMatcher, settings: Settings) -> JobState:
    """Run the workflow synchronously (call from a background worker)."""
    meta = store.meta()
    expected_batches = math.ceil((meta.total_rows or settings.max_rows) / settings.batch_size)
    config: RunnableConfig = {
        "configurable": {"store": store, "matcher": matcher, "settings": settings},
        # LangGraph counts every node visit; the batch loop needs one step per batch.
        "recursion_limit": expected_batches + 20,
    }
    return get_graph().invoke({"job_id": job_id, "job_dir": str(store.job_dir)}, config)
