"""Run a workbook through the same LangGraph workflow as the UI, without Streamlit.

Usage: python scripts/run_batch.py input.xlsx output.xlsx
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from addrmatch.jobs import JobManager  # noqa: E402
from addrmatch.settings import get_settings  # noqa: E402


def main(src: Path, dst: Path) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    manager = JobManager(get_settings())
    try:
        job_id, reused = manager.submit(src.name, src.read_bytes())
        meta = manager.wait(job_id, timeout=3600)
        if meta.status != "completed":
            print(f"Job {job_id} failed: {meta.error_message}", file=sys.stderr)
            return 1
        shutil.copyfile(manager.store(job_id).output_path, dst)
        print(f"Job {job_id}{' (reused)' if reused else ''}: {meta.metrics['decisions']} -> {dst}")
        return 0
    finally:
        manager.shutdown()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
