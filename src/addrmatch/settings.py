"""Runtime settings loaded from environment variables (optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:  # python-dotenv is optional
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


@dataclass(frozen=True)
class Settings:
    """All tunable runtime values. Business rules live in config/scoring.toml."""

    data_dir: Path
    scoring_config_path: Path
    max_upload_mb: int
    max_uncompressed_mb: int
    max_rows: int
    batch_size: int
    max_concurrent_jobs: int
    job_retention_hours: int
    max_address_chars: int
    libpostal_languages: tuple[str, ...]
    log_level: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_s: int

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_settings() -> Settings:
    langs = tuple(x.strip() for x in _str("LIBPOSTAL_LANGUAGES", "").split(",") if x.strip())
    data_dir = Path(_str("APP_DATA_DIR", str(PROJECT_ROOT / ".data" / "jobs")))
    cfg = Path(_str("SCORING_CONFIG_PATH", str(PROJECT_ROOT / "config" / "scoring.toml")))
    return Settings(
        data_dir=data_dir,
        scoring_config_path=cfg,
        max_upload_mb=_int("MAX_UPLOAD_MB", 20),
        max_uncompressed_mb=_int("MAX_UNCOMPRESSED_MB", 200),
        max_rows=_int("MAX_ROWS", 100_000),
        batch_size=max(1, _int("BATCH_SIZE", 200)),
        max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 1)),
        job_retention_hours=_int("JOB_RETENTION_HOURS", 24),
        max_address_chars=_int("MAX_ADDRESS_CHARS", 500),
        libpostal_languages=langs,
        log_level=_str("LOG_LEVEL", "INFO"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=_str("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_timeout_s=_int("LLM_TIMEOUT_S", 20),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
