"""Unit tests for bilingual SQL planner and self-correction repairer."""

from __future__ import annotations

from pathlib import Path

import pytest

from sql_analyst.database import get_schema_context, get_schema_info
from sql_analyst.planner import LocalSQLPlanner, detect_language
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
