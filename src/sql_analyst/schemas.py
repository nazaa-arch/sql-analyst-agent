"""Structured Pydantic v2 data contracts for the SQL Analyst Agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ChartType = Literal["bar", "line", "doughnut", "table"]
LanguageCode = Literal["en", "id"]
StepName = Literal[
    "inspect_schema",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "self_correct",
    "synthesize_report",
]
StepStatus = Literal["ok", "warning", "error", "blocked"]
ResponseStatus = Literal["completed", "blocked", "error"]


class ColumnSchema(BaseModel):
    """Metadata for a single SQLite column."""

    name: str
    data_type: str
    nullable: bool = True
    primary_key: bool = False
    foreign_key: str | None = None


class TableSchema(BaseModel):
    """Schema, row count, DDL, and sample values for a database table."""

    name: str
    row_count: int
    columns: list[ColumnSchema]
    ddl: str
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class DatabaseSchemaResponse(BaseModel):
    """Full database schema snapshot exposed via API."""

    db_path: str
    table_count: int
    total_rows: int
    tables: list[TableSchema]


class SQLPlan(BaseModel):
    """Structured output produced by the SQL planner."""

    reasoning: str
    sql: str
    chart_type: ChartType = "table"
    selected_tables: list[str] = Field(default_factory=list)
    language: LanguageCode = "en"
    intent: str = "analytical"
    is_unsafe_request: bool = False
    unsafe_reason: str | None = None


class ValidationResult(BaseModel):
    """Outcome of the static SQL security sandbox validator."""

    is_valid: bool
    normalized_sql: str
    blocked_reasons: list[str] = Field(default_factory=list)
    statement_type: str = "UNKNOWN"
    tables_referenced: list[str] = Field(default_factory=list)


class ToolStep(BaseModel):
    """Single observable step in the agent execution trace."""

    step_number: int
    step_name: StepName
    status: StepStatus
    summary: str
    latency_ms: float
    details: dict[str, Any] = Field(default_factory=dict)


class SQLExecutionResult(BaseModel):
    """Rows and execution metadata returned by the read-only sandbox."""

    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: float = 0.0


class ChartDataset(BaseModel):
    """Single data series for Chart.js visualization."""

    label: str
    data: list[float]


class ChartSpec(BaseModel):
    """Chart.js-ready visualization specification."""

    chart_type: ChartType
    title: str
    x_label: str | None = None
    y_label: str | None = None
    labels: list[str] = Field(default_factory=list)
    datasets: list[ChartDataset] = Field(default_factory=list)


class KPIHighlight(BaseModel):
    """Executive KPI callout rendered in the report header."""

    label: str
    value: str


class AskRequest(BaseModel):
    """Incoming natural language analytics request."""

    question: str = Field(..., min_length=1, max_length=1200)
    provider: Literal["local", "ollama", "gemini", "openai"] | None = None
    max_retries: int | None = Field(default=None, ge=0, le=5)
    simulate_initial_error: bool = False

    @field_validator("question")
    @classmethod
    def _validate_question_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question must not be empty or whitespace-only.")
        return cleaned


class ValidateSQLRequest(BaseModel):
    """Incoming SQL string for dry-run security sandbox validation."""

    sql: str = Field(..., min_length=1, max_length=5000)

    @field_validator("sql")
    @classmethod
    def _validate_sql_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("SQL must not be empty or whitespace-only.")
        return cleaned


class AnalystResponse(BaseModel):
    """Complete structured output from the SQL Analyst Agent."""

    question: str
    language: LanguageCode
    provider: str
    status: ResponseStatus
    summary: str
    kpis: list[KPIHighlight] = Field(default_factory=list)
    sql: str
    reasoning: str
    validation: ValidationResult
    execution: SQLExecutionResult | None = None
    chart: ChartSpec | None = None
    retries_used: int = 0
    total_latency_ms: float = 0.0
    trace: list[ToolStep] = Field(default_factory=list)
    error_message: str | None = None
