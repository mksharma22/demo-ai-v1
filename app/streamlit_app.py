"""Streamlit UI: upload -> validate -> background job -> results, download, follow-up chat.

Run:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from itertools import islice
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from addrmatch.chat import HELP_TEXT, FollowUpAssistant  # noqa: E402
from addrmatch.excel_io import InputValidationError, iter_input_rows, validate_workbook  # noqa: E402
from addrmatch.explanation import reason_label  # noqa: E402
from addrmatch.jobs import JobManager  # noqa: E402
from addrmatch.llm import classify_intent  # noqa: E402
from addrmatch.normalization import libpostal_status  # noqa: E402
from addrmatch.settings import get_settings  # noqa: E402

st.set_page_config(page_title="Address Matcher", page_icon="📍", layout="wide")
settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@st.cache_resource(show_spinner=False)
def get_manager() -> JobManager:
    return JobManager(settings)


def validate_upload(name: str, content: bytes):
    """Validate the upload in a private temp file that is always deleted."""
    with tempfile.TemporaryDirectory(prefix="addr-upload-") as tmp:
        path = Path(tmp) / ("upload" + Path(name).suffix.lower())
        path.write_bytes(content)
        info = validate_workbook(path, max_upload_mb=settings.max_upload_mb, max_uncompressed_mb=settings.max_uncompressed_mb)
        preview = [(r.excel_row, r.address_a, r.address_b) for r in islice(iter_input_rows(path, info, max_rows=10**9), 5)]
    return info, preview


# ---------------------------------------------------------------- sidebar
ok, lp_msg = libpostal_status()
with st.sidebar:
    st.header("Status")
    (st.success if ok else st.error)(f"libpostal: {'available' if ok else 'NOT available'}")
    if not ok:
        st.caption(lp_msg + " See README → Installation.")
    st.caption(f"Follow-up assistant: {'LLM routing on' if settings.llm_enabled else 'rule-based (no LLM configured)'}")
    st.caption(f"Limits: {settings.max_upload_mb} MB, {settings.max_rows:,} rows · batch size {settings.batch_size}")
    st.divider()
    try:
        manager = get_manager()
    except Exception as exc:  # e.g. bad scoring config
        st.error("The app could not start its job manager. Check config/scoring.toml and APP_DATA_DIR.")
        logging.getLogger(__name__).exception("JobManager init failed: %s", exc)
        st.stop()
    jobs = sorted(manager.list_jobs(), key=lambda m: m.created_at, reverse=True)[:10]
    if jobs:
        st.subheader("Recent jobs")
        for m in jobs:
            if st.button(f"{m.filename} · {m.status}", key=f"open-{m.id}", width="stretch"):
                st.session_state["job_id"] = m.id
    st.caption(f"Job data is deleted after {settings.job_retention_hours} h.")

st.title("Address Normalization & Matching")
st.caption("Upload an .xlsx with **AddressA** and **AddressB** columns. Addresses are normalized with libpostal "
           "and compared component by component. Scores are weighted similarity, not probabilities.")

# ---------------------------------------------------------------- upload
uploaded = st.file_uploader("Workbook (.xlsx)", type=["xlsx"], accept_multiple_files=False)
if uploaded is not None:
    content = uploaded.getvalue()
    try:
        info, preview = validate_upload(uploaded.name, content)
    except InputValidationError as exc:
        st.error(f"{exc}")
        st.info("Fix: make sure row 1 of a sheet contains headers named AddressA and AddressB, save as .xlsx, and upload again.")
    else:
        msg = f"Found AddressA and AddressB on sheet **{info.sheet_name}**."
        if info.extra_columns:
            msg += f" Other columns will be ignored: {', '.join(info.extra_columns[:8])}."
        st.success(msg)
        if preview:
            st.dataframe(pd.DataFrame(preview, columns=["Sheet row", "AddressA", "AddressB"]), hide_index=True)
        if st.button("Start processing", type="primary", disabled=not ok):
            try:
                job_id, reused = manager.submit(uploaded.name, content)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state["job_id"] = job_id
                st.session_state.pop("chat", None)
                if reused:
                    st.info("This file was already submitted with the same configuration; showing that job.")

job_id = st.session_state.get("job_id")
if not job_id:
    st.stop()

try:
    meta = manager.get(job_id)
except Exception:
    st.warning("That job is no longer available (it may have expired). Upload the file again.")
    st.session_state.pop("job_id", None)
    st.stop()

st.divider()
st.subheader(f"Job {meta.id}")


# ---------------------------------------------------------------- progress
@st.fragment(run_every=1.0)
def progress_panel(job_id: str) -> None:
    m = manager.get(job_id)
    if m.status in ("queued", "running"):
        total = f"{m.total_rows:,}" if m.total_rows is not None else "…"
        st.progress(m.progress, text=f"{m.status.capitalize()}: {m.processed_rows:,} / {total} rows")
    else:
        st.rerun()  # finished: re-render the full page once


if meta.status in ("queued", "running"):
    progress_panel(job_id)
    st.stop()

if meta.status == "failed":
    st.error(meta.error_message or "The job failed.")
    if st.button("Retry job"):
        try:
            manager.retry(job_id)
            st.rerun()
        except ValueError as exc:
            st.warning(str(exc))
    st.stop()

# ---------------------------------------------------------------- results
store = manager.store(job_id)
metrics = meta.metrics
dec = metrics.get("decisions", {})
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows", metrics.get("total_rows", 0))
c2.metric("MATCH", dec.get("MATCH", 0))
c3.metric("NON-MATCH", dec.get("NON-MATCH", 0))
c4.metric("REVIEW", dec.get("REVIEW", 0))
c5.metric("Errors / failures", metrics.get("normalization_failures", 0) + metrics.get("row_errors", 0))
extra = []
if metrics.get("empty_rows"):
    extra.append(f"{metrics['empty_rows']} row(s) with an empty address")
if metrics.get("duplicate_rows"):
    extra.append(f"{metrics['duplicate_rows']} duplicate row(s) (each kept and processed)")
if extra:
    st.caption(" · ".join(extra))

if store.output_path.exists():
    st.download_button(
        "⬇️ Download results (.xlsx)",
        data=store.output_path.read_bytes(),
        file_name=f"{Path(meta.filename).stem}_matched.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

df = store.results_frame()
tab_preview, tab_reasons = st.tabs(["Preview", "Reasons"])
with tab_preview:
    choice = st.segmented_control("Filter", ["All", "MATCH", "NON-MATCH", "REVIEW"], default="All")
    view = df if choice in (None, "All") else df[df["decision"] == choice]
    st.dataframe(
        view[["AddressA", "AddressB", "normalized_AddressA", "normalized_AddressB", "Result"]].head(500),
        hide_index=True, width="stretch",
    )
    if len(view) > 500:
        st.caption(f"Showing 500 of {len(view):,} rows. Download the file for all rows.")
with tab_reasons:
    reasons = metrics.get("reason_counts", {})
    if reasons:
        st.dataframe(pd.DataFrame([(reason_label(k), v) for k, v in reasons.items()], columns=["Reason", "Rows"]),
                     hide_index=True)

# ---------------------------------------------------------------- follow-up chat
st.divider()
st.subheader("Ask about this job")
chat_key = f"chat-{job_id}"
history: list[dict] = st.session_state.setdefault(chat_key, [])
llm = (lambda q: classify_intent(q, settings)) if settings.llm_enabled else None
assistant = FollowUpAssistant(store, llm_classifier=llm)

if not history:
    with st.chat_message("assistant"):
        st.markdown(HELP_TEXT)
for i, msg in enumerate(history):
    with st.chat_message(msg["role"]):
        st.markdown(msg["text"])
        if msg.get("table") is not None:
            st.dataframe(msg["table"], hide_index=True, width="stretch")
        if msg.get("file_bytes"):
            st.download_button(f"⬇️ {msg['file_name']}", msg["file_bytes"], file_name=msg["file_name"], key=f"dl-{job_id}-{i}")

question = st.chat_input("e.g. Show rows where building numbers differ")
if question:
    history.append({"role": "user", "text": question})
    try:
        resp = assistant.answer(question)
        history.append({"role": "assistant", "text": resp.text, "table": resp.table,
                        "file_bytes": resp.file_bytes, "file_name": resp.file_name})
    except Exception:
        logging.getLogger(__name__).exception("chat failed")
        history.append({"role": "assistant", "text": "Sorry, I couldn't answer that. Try rephrasing."})
    st.rerun()
