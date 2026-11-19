FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    AGENT_PROVIDER=local \
    AGENT_DB_PATH=/app/data/analytics.db \
    AGENT_EVAL_REPORT_PATH=/app/eval/latest_report.json

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY eval ./eval

RUN mkdir -p /app/data && python eval/run_eval.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "sql_analyst.api:app", "--host", "0.0.0.0", "--port", "8000"]
