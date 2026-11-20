"""Multi-step SQL Analyst Agent orchestrator with self-correction and chart synthesis."""

from __future__ import annotations

import re
import time
from typing import Any

from sql_analyst.config import Settings
from sql_analyst.database import get_schema_context, get_schema_info
from sql_analyst.planner import BaseSQLPlanner, create_planner, detect_language
from sql_analyst.sandbox import (
    SQLExecutionError,
    SQLSecurityError,
    SQLTimeoutError,
    execute_readonly_sql,
    validate_sql,
)
from sql_analyst.schemas import (
    AnalystResponse,
    ChartDataset,
    ChartSpec,
    ChartType,
    KPIHighlight,
    LanguageCode,
    SQLExecutionResult,
    SQLPlan,
    TableSchema,
    ToolStep,
    ValidationResult,
)

TABLE_KEYWORDS: dict[str, set[str]] = {
    "warehouses": {"warehouse", "gudang", "hub", "surabaya", "jakarta", "bandung", "semarang", "makassar"},
    "customers": {"customer", "pelanggan", "pembeli", "segment", "segmen", "province", "provinsi", "retail", "smb", "enterprise"},
    "products": {"product", "produk", "barang", "sku", "category", "kategori", "margin", "profit", "laba", "keuntungan", "stock", "stok"},
    "orders": {"order", "orders", "pesanan", "revenue", "sales", "pendapatan", "penjualan", "omzet", "discount", "diskon", "payment", "pembayaran", "cancel", "batal", "return", "retur", "month", "bulan", "quarter", "kuartal", "status"},
    "order_items": {"product", "produk", "category", "kategori", "quantity", "unit", "terjual", "margin", "profit", "laba", "item", "terlaris"},
    "shipments": {"shipment", "shipments", "delivery", "pengiriman", "carrier", "kurir", "ekspedisi", "delay", "delayed", "terlambat", "shipping", "ongkir", "biaya kirim"},
}

CURRENCY_HINTS = ("revenue", "amount", "spent", "value", "cost", "price", "profit", "discount")
PERCENT_HINTS = ("pct", "rate", "margin", "share")


def _select_relevant_tables(question: str, all_tables: list[TableSchema]) -> list[str]:
    low = question.lower()
    available = [t.name for t in all_tables]
    selected: list[str] = []
    for tname in available:
        kws = TABLE_KEYWORDS.get(tname, set())
        if any(re.search(rf"\b{re.escape(kw)}", low) for kw in kws):
            selected.append(tname)
    if not selected:
        return available
    # Ensure relational bridges are included when joining products/customers/shipments
    if "order_items" in selected and "orders" not in selected:
        selected.append("orders")
    if "products" in selected and "orders" in selected and "order_items" not in selected:
        selected.append("order_items")
    return sorted(set(selected))


def _inject_realistic_sql_fault(sql: str) -> tuple[str, str]:
    """Mutate a single valid column in a SQL query to simulate a realistic LLM hallucination."""
    substitutions = [
        ("o.total_amount", "o.nonexistent_revenue_col"),
        ("s.is_delayed", "s.delayed_flag"),
        ("oi.line_total", "oi.revenue"),
        ("p.unit_price", "p.price"),
        ("w.name", "w.warehouse_name"),
        ("c.customer_name", "c.name"),
        ("o.order_date", "o.created_at"),
    ]
    for original, faulty in substitutions:
        if original in sql:
            return sql.replace(original, faulty, 1), f"Simulated draft column typo: '{original}' -> '{faulty}'"
    # Fallback: replace first SELECT column or FROM table
    m = re.search(r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE)
    if m:
        tbl = m.group(1)
        if tbl.endswith("s"):
            faulty_tbl = tbl[:-1]
            return (
                re.sub(rf"\bFROM\s+{re.escape(tbl)}\b", f"FROM {faulty_tbl}", sql, count=1, flags=re.IGNORECASE),
                f"Simulated draft table typo: '{tbl}' -> '{faulty_tbl}'",
            )
    return sql.replace("SELECT", "SELECT bad_hallucinated_col,", 1), "Simulated hallucinated column in SELECT"


def _format_metric_value(col_name: str, val: Any) -> str:
    if val is None:
        return "N/A"
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        low = col_name.lower()
        if any(h in low for h in PERCENT_HINTS):
            return f"{float(val):.2f}%"
        if "days" in low:
            return f"{float(val):.2f} days"
        if any(h in low for h in CURRENCY_HINTS):
            return f"Rp {val:,.0f}"
        if isinstance(val, int) or float(val).is_integer():
            return f"{int(val):,}"
        return f"{float(val):,.2f}"
    return str(val)


def _humanize_col(col: str) -> str:
    return col.replace("_", " ").title().replace("Pct", "(%)").replace("Avg ", "Avg. ")


def _choose_primary_metric_col(columns: list[str], rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    numeric_cols: list[str] = []
    for col in columns:
        low = col.lower()
        if low.endswith("_id") or low == "id":
            continue
        sample_vals = [r.get(col) for r in rows if r.get(col) is not None]
        if sample_vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in sample_vals):
            numeric_cols.append(col)
    if not numeric_cols:
        return None

    priority = [
        "total_revenue",
        "gross_profit",
        "total_spent",
        "net_revenue",
        "delay_rate_pct",
        "profit_margin_pct",
        "cancellation_rate_pct",
        "return_rate_pct",
        "avg_delivery_days",
        "total_discount",
        "total_units_sold",
        "units_sold",
        "order_count",
        "total_orders",
        "stock_qty",
        "avg_shipping_cost",
        "total_amount",
    ]
    for p in priority:
        if p in numeric_cols:
            return p
    return numeric_cols[-1]


def build_chart_spec(
    question: str,
    plan_chart_type: ChartType,
    result: SQLExecutionResult,
) -> ChartSpec:
    """Build a Chart.js-ready ChartSpec automatically adapted to result shape."""
    if result.row_count <= 1 or not result.columns:
        return ChartSpec(
            chart_type="table",
            title=question[:80],
            labels=[],
            datasets=[],
        )

    # Identify label column (first non-numeric column)
    label_col: str | None = None
    for col in result.columns:
        sample = next((r.get(col) for r in result.rows if r.get(col) is not None), None)
        if isinstance(sample, str):
            label_col = col
            break
    if label_col is None:
        label_col = result.columns[0]

    metric_col = _choose_primary_metric_col(result.columns, result.rows)
    if metric_col is None:
        return ChartSpec(
            chart_type="table",
            title=question[:80],
            labels=[],
            datasets=[],
        )

    # Auto-refine chart type for clarity
    chart_type: ChartType = plan_chart_type
    if label_col.lower() in ("month", "quarter", "order_date", "date", "year"):
        chart_type = "line"
    elif (chart_type == "doughnut" and result.row_count > 8) or (
        chart_type == "table" and result.row_count >= 2
    ):
        chart_type = "bar"

    labels = [str(r.get(label_col, "")) for r in result.rows]
    values = [float(r.get(metric_col) or 0.0) for r in result.rows]

    datasets = [ChartDataset(label=_humanize_col(metric_col), data=values)]

    # For profit analysis bar charts, optionally pair revenue & gross_profit (same IDR unit!)
    if (
        chart_type == "bar"
        and metric_col == "gross_profit"
        and "total_revenue" in result.columns
    ):
        rev_values = [float(r.get("total_revenue") or 0.0) for r in result.rows]
        datasets = [
            ChartDataset(label="Total Revenue", data=rev_values),
            ChartDataset(label="Gross Profit", data=values),
        ]

    return ChartSpec(
        chart_type=chart_type,
        title=f"{_humanize_col(metric_col)} by {_humanize_col(label_col)}",
        x_label=_humanize_col(label_col),
        y_label=_humanize_col(metric_col),
        labels=labels,
        datasets=datasets,
    )


def synthesize_executive_report(
    question: str,
    language: LanguageCode,
    result: SQLExecutionResult,
    chart: ChartSpec,
) -> tuple[str, list[KPIHighlight]]:
    """Generate bilingual executive summary and KPI cards grounded in actual query results."""
    del question
    kpis: list[KPIHighlight] = []

    if result.row_count == 0:
        msg = (
            "Query berhasil dijalankan namun tidak menemukan data yang cocok dengan filter."
            if language == "id"
            else "The query executed cleanly in read-only mode but returned 0 matching rows."
        )
        return msg, [KPIHighlight(label="Rows Returned", value="0")]

    # Single-row aggregate result (KPI summary)
    if result.row_count == 1:
        row = result.rows[0]
        parts: list[str] = []
        for col in result.columns[:5]:
            formatted = _format_metric_value(col, row.get(col))
            label = _humanize_col(col)
            kpis.append(KPIHighlight(label=label, value=formatted))
            parts.append(f"{label}: {formatted}")
        if language == "id":
            summary = f"Ringkasan analitik berhasil dihitung — {', '.join(parts)}."
        else:
            summary = f"Analytical summary computed — {', '.join(parts)}."
        return summary, kpis[:4]

    # Multi-row result
    label_col = next(
        (c for c in result.columns if isinstance(result.rows[0].get(c), str)),
        result.columns[0],
    )
    metric_col = _choose_primary_metric_col(result.columns, result.rows)

    kpis.append(KPIHighlight(label="Rows Returned", value=f"{result.row_count:,}"))
    if metric_col:
        first_row = result.rows[0]
        top_label = str(first_row.get(label_col, ""))
        top_val = first_row.get(metric_col)
        numeric_vals = [
            float(r[metric_col])
            for r in result.rows
            if isinstance(r.get(metric_col), (int, float))
        ]
        kpis.append(
            KPIHighlight(
                label=f"Top {_humanize_col(label_col)}",
                value=top_label,
            )
        )
        kpis.append(
            KPIHighlight(
                label=f"Lead {_humanize_col(metric_col)}",
                value=_format_metric_value(metric_col, top_val),
            )
        )
        if numeric_vals:
            is_rate = any(h in metric_col.lower() for h in PERCENT_HINTS) or "avg_" in metric_col.lower()
            agg_val = (sum(numeric_vals) / len(numeric_vals)) if is_rate else sum(numeric_vals)
            agg_label = f"Avg. {_humanize_col(metric_col)}" if is_rate else f"Sum {_humanize_col(metric_col)}"
            kpis.append(
                KPIHighlight(
                    label=agg_label,
                    value=_format_metric_value(metric_col, agg_val),
                )
            )

        if chart.chart_type == "line":
            first_period = str(result.rows[0].get(label_col, ""))
            last_period = str(result.rows[-1].get(label_col, ""))
            peak_row = max(result.rows, key=lambda r: float(r.get(metric_col) or 0.0))
            peak_period = str(peak_row.get(label_col, ""))
            peak_fmt = _format_metric_value(metric_col, peak_row.get(metric_col))
            if language == "id":
                summary = (
                    f"Ditemukan {result.row_count} periode ({first_period} s.d. {last_period}). "
                    f"Puncak {_humanize_col(metric_col)} terjadi pada {peak_period} sebesar {peak_fmt}."
                )
            else:
                summary = (
                    f"Analyzed {result.row_count} periods ({first_period} to {last_period}). "
                    f"Peak {_humanize_col(metric_col)} occurred in {peak_period} at {peak_fmt}."
                )
        else:
            top_fmt = _format_metric_value(metric_col, top_val)
            if language == "id":
                summary = (
                    f"Analisis menghasilkan {result.row_count} baris data. "
                    f"Peringkat teratas adalah {top_label} dengan {_humanize_col(metric_col)} sebesar {top_fmt}."
                )
            else:
                summary = (
                    f"Query returned {result.row_count} rows. "
                    f"The leading {_humanize_col(label_col).lower()} is {top_label} with {_humanize_col(metric_col)} of {top_fmt}."
                )
    else:
        summary = (
            f"Query read-only berhasil mengembalikan {result.row_count} baris data."
            if language == "id"
            else f"Read-only query executed successfully and returned {result.row_count} rows."
        )

    return summary, kpis[:4]


class SQLAnalystAgent:
    """Autonomous multi-step SQL Analyst Agent with strict read-only sandbox and self-correction."""

    def __init__(
        self,
        settings: Settings,
        planner: BaseSQLPlanner | None = None,
    ) -> None:
        self.settings = settings
        self._planner = planner

    async def ask(
        self,
        question: str,
        provider_override: str | None = None,
        max_retries_override: int | None = None,
        simulate_initial_error: bool = False,
    ) -> AnalystResponse:
        """Execute the full 6-stage agentic pipeline and return a structured AnalystResponse."""
        overall_start = time.perf_counter()
        trace: list[ToolStep] = []
        step_num = 0
        max_retries = (
            self.settings.max_retries
            if max_retries_override is None
            else max_retries_override
        )
        planner = (
            self._planner
            if (self._planner is not None and provider_override is None)
            else create_planner(self.settings, provider_override=provider_override)
        )
        provider_label = planner.provider_name
        detected_lang: LanguageCode = detect_language(question)

        def _record(
            step_name: Any,
            status: Any,
            summary: str,
            start_ts: float,
            details: dict[str, Any] | None = None,
        ) -> None:
            nonlocal step_num
            step_num += 1
            trace.append(
                ToolStep(
                    step_number=step_num,
                    step_name=step_name,
                    status=status,
                    summary=summary,
                    latency_ms=round((time.perf_counter() - start_ts) * 1000.0, 2),
                    details=details or {},
                )
            )

        # ── Step 1: inspect_schema ────────────────────────────────────────────
        t0 = time.perf_counter()
        schema_info = get_schema_info(self.settings.db_path, sample_limit=2)
        selected_tables = _select_relevant_tables(question, schema_info.tables)
        schema_context = get_schema_context(
            self.settings.db_path,
            selected_tables=selected_tables,
            schema_info=schema_info,
        )
        _record(
            "inspect_schema",
            "ok",
            f"Inspected {schema_info.table_count} tables; focused on {', '.join(selected_tables)}.",
            t0,
            {"selected_tables": selected_tables, "total_rows": schema_info.total_rows},
        )

        # ── Step 2: generate_sql ──────────────────────────────────────────────
        t1 = time.perf_counter()
        try:
            plan: SQLPlan = await planner.plan(question, schema_context, schema_info.tables)
            detected_lang = plan.language
        except Exception as exc:
            _record(
                "generate_sql",
                "error",
                f"Planner failed: {exc}",
                t1,
                {"provider": provider_label},
            )
            return AnalystResponse(
                question=question,
                language=detected_lang,
                provider=provider_label,
                status="error",
                summary=f"SQL planner error ({provider_label}): {exc}",
                sql="",
                reasoning="Planner invocation failed before SQL generation.",
                validation=ValidationResult(
                    is_valid=False,
                    normalized_sql="",
                    blocked_reasons=[str(exc)],
                    statement_type="ERROR",
                ),
                retries_used=0,
                total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
                trace=trace,
                error_message=str(exc),
            )

        current_sql = plan.sql
        sim_note: str | None = None
        if simulate_initial_error and not plan.is_unsafe_request and current_sql:
            current_sql, sim_note = _inject_realistic_sql_fault(current_sql)

        _record(
            "generate_sql",
            "warning" if sim_note else "ok",
            sim_note or f"Generated {plan.intent} SQL plan (suggested chart: {plan.chart_type}).",
            t1,
            {
                "intent": plan.intent,
                "chart_type": plan.chart_type,
                "reasoning": plan.reasoning,
            },
        )

        # ── Step 3: validate_sql ──────────────────────────────────────────────
        t2 = time.perf_counter()
        validation = validate_sql(current_sql)
        if not validation.is_valid or plan.is_unsafe_request:
            reasons = list(validation.blocked_reasons)
            if plan.is_unsafe_request and plan.unsafe_reason and plan.unsafe_reason not in reasons:
                reasons.insert(0, plan.unsafe_reason)
            if not reasons:
                reasons.append("Query blocked by strict read-only security policy.")
            validation = ValidationResult(
                is_valid=False,
                normalized_sql=validation.normalized_sql,
                blocked_reasons=reasons,
                statement_type=validation.statement_type,
                tables_referenced=validation.tables_referenced,
            )
            _record(
                "validate_sql",
                "blocked",
                f"Blocked unsafe query: {'; '.join(reasons)}",
                t2,
                {"blocked_reasons": reasons, "statement_type": validation.statement_type},
            )
            blocked_summary = (
                f"Permintaan diblokir oleh Read-Only SQL Security Sandbox: {'; '.join(reasons)}"
                if detected_lang == "id"
                else f"Request blocked by Read-Only SQL Security Sandbox: {'; '.join(reasons)}"
            )
            return AnalystResponse(
                question=question,
                language=detected_lang,
                provider=provider_label,
                status="blocked",
                summary=blocked_summary,
                kpis=[KPIHighlight(label="Security Action", value="BLOCKED")],
                sql=current_sql,
                reasoning=plan.reasoning,
                validation=validation,
                execution=None,
                chart=None,
                retries_used=0,
                total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
                trace=trace,
                error_message="; ".join(reasons),
            )

        _record(
            "validate_sql",
            "ok",
            f"Passed read-only sandbox checks ({validation.statement_type}; tables: {', '.join(validation.tables_referenced) or 'none'}).",
            t2,
            {
                "statement_type": validation.statement_type,
                "tables_referenced": validation.tables_referenced,
            },
        )

        # ── Step 4 & 5: execute_sql + self_correct loop ───────────────────────
        retries_used = 0
        execution_result: SQLExecutionResult | None = None
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            t_exec = time.perf_counter()
            try:
                execution_result = execute_readonly_sql(
                    self.settings.db_path,
                    validation.normalized_sql,
                    max_rows=self.settings.max_rows,
                    timeout_sec=self.settings.query_timeout_sec,
                )
                _record(
                    "execute_sql",
                    "ok",
                    f"Executed read-only query in {execution_result.execution_time_ms:.2f} ms ({execution_result.row_count} rows).",
                    t_exec,
                    {
                        "attempt": attempt + 1,
                        "row_count": execution_result.row_count,
                        "truncated": execution_result.truncated,
                    },
                )
                last_error = None
                break
            except SQLSecurityError as exc:
                _record(
                    "validate_sql",
                    "blocked",
                    f"Runtime sandbox blocked query: {exc}",
                    t_exec,
                    {"blocked_reasons": exc.reasons},
                )
                return AnalystResponse(
                    question=question,
                    language=detected_lang,
                    provider=provider_label,
                    status="blocked",
                    summary=f"Blocked by read-only sandbox: {exc}",
                    sql=current_sql,
                    reasoning=plan.reasoning,
                    validation=ValidationResult(
                        is_valid=False,
                        normalized_sql=validation.normalized_sql,
                        blocked_reasons=exc.reasons,
                        statement_type=validation.statement_type,
                    ),
                    retries_used=retries_used,
                    total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
                    trace=trace,
                    error_message=str(exc),
                )
            except SQLTimeoutError as exc:
                last_error = str(exc)
                _record("execute_sql", "error", last_error, t_exec, {"attempt": attempt + 1})
                break
            except SQLExecutionError as exc:
                last_error = str(exc)
                _record(
                    "execute_sql",
                    "error",
                    f"Attempt {attempt + 1} failed with SQLite error: {last_error}",
                    t_exec,
                    {"attempt": attempt + 1, "sqlite_error": last_error},
                )
                if attempt >= max_retries:
                    break

                # Self-correction step
                retries_used += 1
                t_fix = time.perf_counter()
                try:
                    repaired_plan = await planner.repair(
                        question=question,
                        failed_sql=current_sql,
                        error_message=last_error,
                        schema_context=schema_context,
                        tables=schema_info.tables,
                    )
                    current_sql = repaired_plan.sql
                    plan.reasoning = repaired_plan.reasoning
                    if repaired_plan.chart_type:
                        plan.chart_type = repaired_plan.chart_type
                    _record(
                        "self_correct",
                        "ok",
                        f"Retry #{retries_used}: repaired SQL from SQLite error feedback.",
                        t_fix,
                        {"retry": retries_used, "repair_reasoning": repaired_plan.reasoning},
                    )
                except Exception as repair_exc:
                    last_error = f"Self-correction failed: {repair_exc}"
                    _record("self_correct", "error", last_error, t_fix, {"retry": retries_used})
                    break

                # Mandatory re-validation after every repair!
                t_reval = time.perf_counter()
                validation = validate_sql(current_sql)
                if not validation.is_valid:
                    _record(
                        "validate_sql",
                        "blocked",
                        f"Repaired SQL rejected by sandbox: {'; '.join(validation.blocked_reasons)}",
                        t_reval,
                        {"blocked_reasons": validation.blocked_reasons},
                    )
                    return AnalystResponse(
                        question=question,
                        language=detected_lang,
                        provider=provider_label,
                        status="blocked",
                        summary=f"Repaired query blocked by security sandbox: {'; '.join(validation.blocked_reasons)}",
                        sql=current_sql,
                        reasoning=plan.reasoning,
                        validation=validation,
                        retries_used=retries_used,
                        total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
                        trace=trace,
                        error_message="; ".join(validation.blocked_reasons),
                    )
                _record(
                    "validate_sql",
                    "ok",
                    "Repaired SQL passed read-only sandbox validation.",
                    t_reval,
                    {"tables_referenced": validation.tables_referenced},
                )

        if execution_result is None:
            err_summary = (
                f"Gagal mengeksekusi query setelah {retries_used} percobaan perbaikan: {last_error}"
                if detected_lang == "id"
                else f"SQL execution failed after {retries_used} self-correction attempts: {last_error}"
            )
            return AnalystResponse(
                question=question,
                language=detected_lang,
                provider=provider_label,
                status="error",
                summary=err_summary,
                sql=current_sql,
                reasoning=plan.reasoning,
                validation=validation,
                execution=None,
                chart=None,
                retries_used=retries_used,
                total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
                trace=trace,
                error_message=last_error,
            )

        # ── Step 6: synthesize_report ─────────────────────────────────────────
        t_syn = time.perf_counter()
        chart_spec = build_chart_spec(question, plan.chart_type, execution_result)
        summary, kpis = synthesize_executive_report(
            question, detected_lang, execution_result, chart_spec
        )
        _record(
            "synthesize_report",
            "ok",
            f"Synthesized {detected_lang.upper()} executive summary, {len(kpis)} KPIs, and '{chart_spec.chart_type}' chart spec.",
            t_syn,
            {"chart_type": chart_spec.chart_type, "kpi_count": len(kpis)},
        )

        return AnalystResponse(
            question=question,
            language=detected_lang,
            provider=provider_label,
            status="completed",
            summary=summary,
            kpis=kpis,
            sql=validation.normalized_sql,
            reasoning=plan.reasoning,
            validation=validation,
            execution=execution_result,
            chart=chart_spec,
            retries_used=retries_used,
            total_latency_ms=round((time.perf_counter() - overall_start) * 1000.0, 2),
            trace=trace,
            error_message=None,
        )
