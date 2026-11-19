"""Tests for the multi-step SQLAnalystAgent orchestrator, self-correction loop, and ChartSpec."""

from __future__ import annotations

import pytest

from sql_analyst.agent import SQLAnalystAgent
from sql_analyst.config import Settings
from sql_analyst.planner import BaseSQLPlanner
from sql_analyst.schemas import SQLPlan, TableSchema


class FlakyMockPlanner(BaseSQLPlanner):
    """Test double that fails with a real SQLite error twice before producing valid SQL."""

    provider_name = "flaky-mock"

    def __init__(self) -> None:
        self.repair_calls = 0

    async def plan(
        self,
        question: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        del question, schema_context, tables
        return SQLPlan(
            reasoning="Initial buggy draft referencing wrong column.",
            sql="SELECT w.name, SUM(o.bad_col_1) AS total_revenue FROM orders o JOIN warehouses w ON w.warehouse_id = o.warehouse_id GROUP BY w.name",
            chart_type="bar",
            selected_tables=["orders", "warehouses"],
            language="en",
        )

    async def repair(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        del question, failed_sql, schema_context, tables
        self.repair_calls += 1
        if self.repair_calls == 1:
            return SQLPlan(
                reasoning=f"First repair attempt after '{error_message}', still has typo.",
                sql="SELECT w.name, SUM(o.bad_col_2) AS total_revenue FROM orders o JOIN warehouses w ON w.warehouse_id = o.warehouse_id GROUP BY w.name",
                chart_type="bar",
                selected_tables=["orders", "warehouses"],
                language="en",
            )
        return SQLPlan(
            reasoning=f"Second repair attempt fixed column after '{error_message}'.",
            sql="SELECT w.name, ROUND(SUM(o.total_amount), 2) AS total_revenue FROM orders o JOIN warehouses w ON w.warehouse_id = o.warehouse_id GROUP BY w.name ORDER BY total_revenue DESC",
            chart_type="bar",
            selected_tables=["orders", "warehouses"],
            language="en",
        )


@pytest.mark.asyncio
async def test_agent_full_pipeline_and_chart_spec(agent: SQLAnalystAgent) -> None:
    resp = await agent.ask("Show monthly revenue trend across 2025")
    assert resp.status == "completed"
    assert resp.language == "en"
    assert resp.retries_used == 0
    assert resp.execution is not None
    assert resp.execution.row_count == 12
    assert resp.chart is not None
    assert resp.chart.chart_type == "line"
    assert len(resp.chart.labels) == 12
    assert len(resp.kpis) >= 2

    step_names = [s.step_name for s in resp.trace]
    assert step_names == [
        "inspect_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "synthesize_report",
    ]


@pytest.mark.asyncio
async def test_agent_blocks_adversarial_prompt_immediately(agent: SQLAnalystAgent) -> None:
    resp = await agent.ask("DROP TABLE customers; SELECT 1")
    assert resp.status == "blocked"
    assert resp.execution is None
    assert resp.validation.is_valid is False
    assert any(s.step_name == "validate_sql" and s.status == "blocked" for s in resp.trace)
    assert not any(s.step_name == "execute_sql" for s in resp.trace)


@pytest.mark.asyncio
async def test_agent_self_correction_loop_with_simulated_fault(agent: SQLAnalystAgent) -> None:
    resp = await agent.ask(
        "Compare total revenue across all warehouses",
        simulate_initial_error=True,
    )
    assert resp.status == "completed"
    assert resp.retries_used == 1
    assert resp.execution is not None
    assert resp.execution.row_count == 5
    step_names = [s.step_name for s in resp.trace]
    assert "self_correct" in step_names
    assert step_names.count("execute_sql") == 2


@pytest.mark.asyncio
async def test_agent_multi_retry_recovery_with_flaky_planner(settings: Settings) -> None:
    mock_planner = FlakyMockPlanner()
    custom_agent = SQLAnalystAgent(settings=settings, planner=mock_planner)

    resp = await custom_agent.ask("Revenue by warehouse")
    assert resp.status == "completed"
    assert resp.retries_used == 2
    assert mock_planner.repair_calls == 2
    assert resp.execution is not None
    assert resp.execution.row_count == 5
    assert resp.chart is not None
    assert resp.chart.chart_type == "bar"
