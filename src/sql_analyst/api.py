"""FastAPI REST API & SQL Analyst Studio Web UI."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sql_analyst import __version__
from sql_analyst.agent import SQLAnalystAgent
from sql_analyst.config import Settings, get_settings
from sql_analyst.database import ensure_database, get_schema_info
from sql_analyst.sandbox import validate_sql
from sql_analyst.schemas import (
    AnalystResponse,
    AskRequest,
    DatabaseSchemaResponse,
    ValidateSQLRequest,
    ValidationResult,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ensure_database(cfg.db_path)
        yield

    application = FastAPI(
        title="SQL Analyst Agent",
        description=(
            "Autonomous, tool-using Data & SQL Analyst Agent with a strict read-only "
            "SQLite security sandbox, multi-step self-correction loop, and Chart.js recommendations."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    agent = SQLAnalystAgent(cfg)
    application.state.settings = cfg
    application.state.agent = agent

    if STATIC_DIR.is_dir():
        application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error while processing API request: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})

    @application.get("/", include_in_schema=False)
    async def studio_ui() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Studio UI index.html not found.")
        return FileResponse(index_path)

    @application.get("/health")
    async def health() -> dict[str, Any]:
        schema = get_schema_info(cfg.db_path, sample_limit=1)
        return {
            "status": "ok",
            "version": __version__,
            "provider": cfg.provider,
            "model": cfg.model,
            "db_path": schema.db_path,
            "table_count": schema.table_count,
            "total_rows": schema.total_rows,
            "max_retries": cfg.max_retries,
            "max_rows": cfg.max_rows,
            "query_timeout_sec": cfg.query_timeout_sec,
        }

    @application.get("/v1/schema", response_model=DatabaseSchemaResponse)
    async def schema_endpoint() -> DatabaseSchemaResponse:
        return get_schema_info(cfg.db_path, sample_limit=3)

    @application.post("/v1/ask", response_model=AnalystResponse)
    async def ask_endpoint(req: AskRequest) -> AnalystResponse:
        return await agent.ask(
            question=req.question,
            provider_override=req.provider,
            max_retries_override=req.max_retries,
            simulate_initial_error=req.simulate_initial_error,
        )

    @application.post("/v1/validate-sql", response_model=ValidationResult)
    async def validate_sql_endpoint(req: ValidateSQLRequest) -> ValidationResult:
        return validate_sql(req.sql)

    @application.get("/v1/eval/latest")
    async def latest_eval_endpoint() -> dict[str, Any]:
        report_path = Path(cfg.eval_report_path)
        if not report_path.is_file():
            return {
                "available": False,
                "message": "No evaluation report found yet. Run `python eval/run_eval.py` to generate one.",
            }
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read evaluation report: %s", exc)
            raise HTTPException(
                status_code=500,
                detail="Evaluation report could not be parsed.",
            ) from None
        return {"available": True, "report": data}

    return application


app = create_app()
