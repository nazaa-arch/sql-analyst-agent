FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_PROVIDER=local \
    AGENT_DB_PATH=/app/data/analytics.db \
    AGENT_EVAL_REPORT_PATH=/app/eval/latest_report.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY eval ./eval

RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

RUN python eval/run_eval.py --strict

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "sql_analyst.api:app", "--host", "0.0.0.0", "--port", "8000"]
