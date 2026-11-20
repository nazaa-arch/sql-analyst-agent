"""Unit tests for bilingual SQL planner and self-correction repairer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from sql_analyst.config import Settings
from sql_analyst.database import get_schema_context, get_schema_info
from sql_analyst.planner import (
    GeminiSQLPlanner,
    LocalSQLPlanner,
    OllamaSQLPlanner,
    OpenAISQLPlanner,
    _parse_json_payload,
    detect_language,
)
from sql_analyst.sandbox import execute_readonly_sql, validate_sql


def test_language_detection() -> None:
    assert detect_language("Tampilkan pendapatan bulanan tahun 2025") == "id"
    assert detect_language("Kurir mana yang paling sering terlambat?") == "id"
    assert detect_language("Show top 5 products by total revenue") == "en"
    assert detect_language("Compare profit margin by product category") == "en"


@pytest.mark.asyncio
async def test_local_planner_analytical_queries_execute_cleanly(seeded_db: Path) -> None:
    planner = LocalSQLPlanner()
    schema = get_schema_info(seeded_db)
    ctx = get_schema_context(seeded_db)

    prompts = [
        "Show monthly revenue trend across 2025",
        "Tampilkan 5 produk terlaris berdasarkan pendapatan",
        "Compare profit margin by product category",
        "Which carrier has the highest delay rate?",
        "Tampilkan tingkat pembatalan pesanan per gudang",
        "Compare revenue across customer segments",
    ]

    for prompt in prompts:
        plan = await planner.plan(prompt, ctx, schema.tables)
        assert plan.is_unsafe_request is False
        val = validate_sql(plan.sql)
        assert val.is_valid is True, f"Invalid SQL for '{prompt}': {val.blocked_reasons}"
        res = execute_readonly_sql(seeded_db, val.normalized_sql)
        assert res.row_count >= 1


@pytest.mark.asyncio
async def test_local_planner_repairs_sqlite_errors(seeded_db: Path) -> None:
    planner = LocalSQLPlanner()
    schema = get_schema_info(seeded_db)
    ctx = get_schema_context(seeded_db)

    # 1. Bad column repair
    repaired_col = await planner.repair(
        question="Total revenue by warehouse",
        failed_sql="SELECT w.warehouse_name, SUM(o.revenue) FROM orders o JOIN warehouses w ON w.warehouse_id = o.warehouse_id GROUP BY w.warehouse_id",
        error_message="no such column: o.revenue",
        schema_context=ctx,
        tables=schema.tables,
    )
    assert "o.total_amount" in repaired_col.sql
    assert "w.name" in repaired_col.sql
    res = execute_readonly_sql(seeded_db, repaired_col.sql)
    assert res.row_count == 5

    # 2. Bad table repair
    repaired_tbl = await planner.repair(
        question="Count orders",
        failed_sql="SELECT COUNT(*) AS n FROM order",
        error_message="no such table: order",
        schema_context=ctx,
        tables=schema.tables,
    )
    assert "FROM orders" in repaired_tbl.sql
    res2 = execute_readonly_sql(seeded_db, repaired_tbl.sql)
    assert res2.rows[0]["n"] == 600


def test_parse_json_payload_handles_fences_and_prose() -> None:
    fenced = 'Here is the plan:\n```JSON\n{"sql": "SELECT 1", "chart_type": "table"}\n```'
    assert _parse_json_payload(fenced)["sql"] == "SELECT 1"

    prose = 'Output: {"sql": "SELECT COUNT(*) FROM orders", "language": "en"} Trailing note.'
    assert _parse_json_payload(prose)["language"] == "en"

    with pytest.raises(ValueError):
        _parse_json_payload("no json here at all")


@pytest.mark.asyncio
async def test_external_llm_adapters_endpoints_headers_and_model_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class _MockAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def __aenter__(self) -> _MockAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def post(
            self,
            url: str,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            captured.append({"url": url, "json": json, "headers": headers or {}})
            req = httpx.Request("POST", url)
            if "/api/chat" in url:
                body = {"message": {"content": '{"sql": "SELECT 1", "reasoning": "ok", "chart_type": "table"}'}}
            elif ":generateContent" in url:
                body = {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": '```json\n{"sql": "SELECT 2", "reasoning": "ok", "chart_type": "table"}\n```'}
                                ]
                            }
                        }
                    ]
                }
            else:
                body = {
                    "choices": [
                        {"message": {"content": '{"sql": "SELECT 3", "reasoning": "ok", "chart_type": "table"}'}}
                    ]
                }
            return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)

    # Default settings have provider="local" & model="deterministic-bilingual-v1"
    # Verify provider-override resolves proper default models and never puts Gemini key in URL!
    cfg = Settings(
        provider="local",
        model="deterministic-bilingual-v1",
        ollama_base_url="http://localhost:11434",
        gemini_api_key="test-gemini-secret",
        openai_api_key="test-openai-secret",
    )

    ollama_plan = await OllamaSQLPlanner(cfg).plan("Count orders", "-- ddl", [])
    assert ollama_plan.sql == "SELECT 1"
    assert captured[-1]["url"] == "http://localhost:11434/api/chat"
    assert captured[-1]["json"]["model"] == "qwen2.5-coder:7b"

    gemini_plan = await GeminiSQLPlanner(cfg).plan("Count orders", "-- ddl", [])
    assert gemini_plan.sql == "SELECT 2"
    assert captured[-1]["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    )
    assert "key=" not in captured[-1]["url"]
    assert captured[-1]["headers"]["x-goog-api-key"] == "test-gemini-secret"

    openai_plan = await OpenAISQLPlanner(cfg).plan("Count orders", "-- ddl", [])
    assert openai_plan.sql == "SELECT 3"
    assert captured[-1]["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured[-1]["json"]["model"] == "gpt-4o-mini"
    assert captured[-1]["headers"]["Authorization"] == "Bearer test-openai-secret"
