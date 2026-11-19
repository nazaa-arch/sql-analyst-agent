"""Configuration management using Pydantic v2 and AGENT_* environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ProviderType = Literal["local", "ollama", "gemini", "openai"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(dotenv_path: Path) -> None:
    """Load key=value pairs from a .env file without overriding existing env vars."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val is not None and val != "" else default


def _env_optional(name: str) -> str | None:
    val = os.getenv(name)
    return val if val is not None and val.strip() != "" else None


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val)


class Settings(BaseModel):
    """Runtime settings for SQL Analyst Agent."""

    provider: ProviderType = Field(default="local")
    model: str = Field(default="deterministic-bilingual-v1")
    ollama_base_url: str = Field(default="http://localhost:11434")
    gemini_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)
    openai_base_url: str = Field(default="https://api.openai.com/v1")
    db_path: str = Field(default=str(PROJECT_ROOT / "data" / "analytics.db"))
    max_retries: int = Field(default=3, ge=0, le=10)
    max_rows: int = Field(default=200, ge=1, le=5000)
    query_timeout_sec: float = Field(default=5.0, gt=0.0, le=60.0)
    request_timeout_sec: float = Field(default=30.0, gt=0.0, le=180.0)
    eval_report_path: str = Field(default=str(PROJECT_ROOT / "eval" / "latest_report.json"))
    log_level: str = Field(default="INFO")

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        """Build settings from environment variables (prefixed with AGENT_)."""
        _load_dotenv(dotenv_path or (PROJECT_ROOT / ".env"))
        provider = _env_str("AGENT_PROVIDER", "local").lower()
        if provider not in ("local", "ollama", "gemini", "openai"):
            provider = "local"

        default_models = {
            "local": "deterministic-bilingual-v1",
            "ollama": "qwen2.5-coder:7b",
            "gemini": "gemini-2.0-flash",
            "openai": "gpt-4o-mini",
        }

        return cls(
            provider=provider,  # type: ignore[arg-type]
            model=_env_str("AGENT_MODEL", default_models[provider]),
            ollama_base_url=_env_str("AGENT_OLLAMA_BASE_URL", "http://localhost:11434"),
            gemini_api_key=_env_optional("AGENT_GEMINI_API_KEY"),
            openai_api_key=_env_optional("AGENT_OPENAI_API_KEY"),
            openai_base_url=_env_str("AGENT_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            db_path=_env_str("AGENT_DB_PATH", str(PROJECT_ROOT / "data" / "analytics.db")),
            max_retries=_env_int("AGENT_MAX_RETRIES", 3),
            max_rows=_env_int("AGENT_MAX_ROWS", 200),
            query_timeout_sec=_env_float("AGENT_QUERY_TIMEOUT_SEC", 5.0),
            request_timeout_sec=_env_float("AGENT_REQUEST_TIMEOUT_SEC", 30.0),
            eval_report_path=_env_str(
                "AGENT_EVAL_REPORT_PATH",
                str(PROJECT_ROOT / "eval" / "latest_report.json"),
            ),
            log_level=_env_str("AGENT_LOG_LEVEL", "INFO"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings."""
    return Settings.from_env()
