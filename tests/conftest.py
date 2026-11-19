"""Shared pytest fixtures with isolated temporary SQLite databases."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sql_analyst.agent import SQLAnalystAgent  # noqa: E402
from sql_analyst.api import create_app  # noqa: E402
from sql_analyst.config import Settings  # noqa: E402
from sql_analyst.database import seed_database  # noqa: E402


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_file = tmp_path_factory.mktemp("sql_analyst_db") / "test_analytics.db"
    return seed_database(db_file, force=True, seed=42)


@pytest.fixture()
def settings(seeded_db: Path) -> Settings:
    return Settings(
        provider="local",
        db_path=str(seeded_db),
        max_retries=3,
        max_rows=200,
        query_timeout_sec=5.0,
        eval_report_path=str(PROJECT_ROOT / "eval" / "latest_report.json"),
    )


@pytest.fixture()
def agent(settings: Settings) -> SQLAnalystAgent:
    return SQLAnalystAgent(settings)


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    with TestClient(app) as tc:
        yield tc
