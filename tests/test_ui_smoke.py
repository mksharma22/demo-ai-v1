"""Streamlit smoke test: the page renders and a completed job shows results and answers chat."""

from __future__ import annotations

import pytest
from conftest import ROOT, FakeBackend, write_xlsx

from addrmatch.jobs import JobManager

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(ROOT / "app" / "streamlit_app.py")


@pytest.fixture(autouse=True)
def _fresh_streamlit_cache():
    import streamlit as st

    st.cache_resource.clear()  # the app caches its JobManager per process
    yield
    st.cache_resource.clear()


def test_page_renders_without_job(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "jobs"))
    from addrmatch.settings import get_settings

    get_settings.cache_clear()
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert "Address Normalization" in at.title[0].value
    get_settings.cache_clear()


def test_completed_job_view_and_chat(monkeypatch, tmp_path, settings, cfg):
    monkeypatch.setenv("APP_DATA_DIR", str(settings.data_dir))
    from addrmatch.settings import get_settings

    get_settings.cache_clear()
    m = JobManager(settings, backend=FakeBackend(), scoring=cfg)
    p = write_xlsx(tmp_path / "in.xlsx", ["AddressA", "AddressB"], [
        ["12 Main St, Springfield, IL 62701", "12 Main St, Springfield, IL 62701"],
        ["12 Main St, Springfield, IL 62701", "21 Main St, Springfield, IL 62701"],
    ])
    job_id, _ = m.submit("in.xlsx", p.read_bytes())
    m.wait(job_id, 30)
    m.shutdown()

    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["job_id"] = job_id
    at.run()
    assert not at.exception
    metrics = {x.label: x.value for x in at.metric}
    assert metrics["MATCH"] == "1" and metrics["NON-MATCH"] == "1"
    at.chat_input[0].set_value("Show rows where building numbers differ").run()
    assert at.chat_message[-1].markdown[0].value.startswith("1 row")
    get_settings.cache_clear()
