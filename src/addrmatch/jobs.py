"""Background job management for the Streamlit POC.

Jobs run in a small in-process thread pool so the UI never blocks. Job state
lives in per-job SQLite files, so progress survives Streamlit reruns and a
process restart (a job interrupted by a restart is marked failed and can be
retried; it resumes from the last completed batch).

Limits: single process only; not suitable for multiple app replicas. Use
Celery/RQ + Redis and a shared database for that (see README section H).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .job_store import JobMeta, JobStore
from .matcher import AddressMatcher
from .normalization import CachingNormalizer, NormalizerBackend, get_libpostal_backend
from .scoring_config import ScoringConfig, load_scoring_config
from .settings import Settings
from .workflow import run_job

log = logging.getLogger(__name__)

GENERIC_FAILURE = "Processing failed unexpectedly. Retry the job; if it fails again, contact support with the job ID."


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        backend: NormalizerBackend | None = None,
        scoring: ScoringConfig | None = None,
    ) -> None:
        self.settings = settings
        self.scoring = scoring or load_scoring_config(settings.scoring_config_path)
        self._backend = backend  # injected in tests; libpostal loaded lazily otherwise
        self.data_dir = settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs, thread_name_prefix="addr-job")
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._recover_interrupted()
        self.cleanup_expired()

    # ---- lifecycle ----------------------------------------------------
    def _store(self, job_id: str) -> JobStore:
        if not job_id or not all(c.isalnum() or c == "-" for c in job_id):
            raise KeyError("invalid job id")
        return JobStore(self.data_dir / job_id)

    def _recover_interrupted(self) -> None:
        for meta in self.list_jobs():
            if meta.status in ("queued", "running"):
                self._store(meta.id).update(status="failed", error_message="Job was interrupted by an app restart; retry it.")

    def cleanup_expired(self) -> int:
        """Delete job folders older than the retention period."""
        cutoff = time.time() - self.settings.job_retention_hours * 3600
        removed = 0
        for meta in self.list_jobs():
            if meta.updated_at < cutoff and meta.id not in self._active:
                shutil.rmtree(self.data_dir / meta.id, ignore_errors=True)
                removed += 1
        return removed

    def list_jobs(self) -> list[JobMeta]:
        out = []
        for d in sorted(self.data_dir.iterdir()) if self.data_dir.exists() else []:
            store = JobStore(d)
            if d.is_dir() and store.exists():
                try:
                    out.append(store.meta())
                except Exception:  # corrupt/foreign folder
                    log.warning("Skipping unreadable job folder %s", d.name)
        return out

    # ---- submission ---------------------------------------------------
    def file_hash(self, content: bytes) -> str:
        h = hashlib.sha256(content)
        h.update(self.scoring.fingerprint().encode())  # new config => new results
        return h.hexdigest()

    def submit(self, filename: str, content: bytes) -> tuple[str, bool]:
        """Create (or reuse) a job. Returns (job_id, reused_existing)."""
        if len(content) > self.settings.max_upload_mb * 1024 * 1024:
            raise ValueError(f"File is larger than the {self.settings.max_upload_mb} MB limit.")
        digest = self.file_hash(content)
        with self._lock:
            for meta in self.list_jobs():
                if meta.file_hash == digest and meta.status in ("queued", "running", "completed"):
                    return meta.id, True  # duplicate submission
            job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            store = self._store(job_id)
            store.create(job_id, Path(filename).name[:200], digest)
            os.chmod(store.job_dir, 0o700)
            fd = os.open(store.input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            self._start(job_id)
        return job_id, False

    def retry(self, job_id: str) -> None:
        store = self._store(job_id)
        meta = store.meta()
        if meta.status != "failed":
            raise ValueError("Only failed jobs can be retried.")
        if not meta.ingested and not store.input_path.exists():
            raise ValueError("The original upload is no longer available; upload the file again.")
        store.update(status="queued", error_message=None)
        with self._lock:
            self._start(job_id)

    def _start(self, job_id: str) -> None:
        if job_id in self._active:
            return
        self._active.add(job_id)
        self._executor.submit(self._run, job_id)

    def _matcher(self) -> AddressMatcher:
        backend = self._backend or get_libpostal_backend(self.settings.libpostal_languages)
        normalizer = CachingNormalizer(
            backend,
            max_chars=self.settings.max_address_chars,
            max_expansions=self.scoring.max_expansions,
        )
        return AddressMatcher(normalizer, self.scoring)

    def _run(self, job_id: str) -> None:
        store = self._store(job_id)
        try:
            run_job(job_id, store, self._matcher(), self.settings)
        except Exception as exc:
            log.exception("job %s crashed (%s)", job_id, type(exc).__name__)
            msg = GENERIC_FAILURE
            if type(exc).__name__ == "LibpostalUnavailableError":
                msg = "libpostal is not installed or its data files are missing on the server."
            store.update(status="failed", error_message=msg)
        finally:
            with self._lock:
                self._active.discard(job_id)

    # ---- queries ------------------------------------------------------
    def get(self, job_id: str) -> JobMeta:
        return self._store(job_id).meta()

    def store(self, job_id: str) -> JobStore:
        return self._store(job_id)

    def wait(self, job_id: str, timeout: float = 600) -> JobMeta:
        """Block until a job finishes (used by tests and scripts, not by the UI)."""
        end = time.time() + timeout
        while time.time() < end:
            meta = self.get(job_id)
            if meta.status in ("completed", "failed") and job_id not in self._active:
                return meta
            time.sleep(0.05)
        raise TimeoutError(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
