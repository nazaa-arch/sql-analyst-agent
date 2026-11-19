"""30-task ground-truth Agent Evaluation Harness for SQL Analyst Agent."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sql_analyst.agent import SQLAnalystAgent  # noqa: E402
from sql_analyst.config import Settings  # noqa: E402
from sql_analyst.database import ensure_database  # noqa: E402
from sql_analyst.sandbox import execute_readonly_sql  # noqa: E402

TASKS_PATH = PROJECT_ROOT / "eval" / "benchmark_tasks.json"
REPORT_PATH = PROJECT_ROOT / "eval" / "latest_report.json"

SELF_CORRECTION_TASK_IDS = ("T01", "T09", "T11", "T15", "T17", "T21")


def _normalize_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    return str(val).strip().lower()


def _rows_equivalent(
    actual_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
    abs_tol: float = 0.02,
) -> bool:
    """Compare two query result sets by value equivalence (insensitive to column alias names)."""
    if len(actual_rows) != len(expected_rows):
        return False
    if not expected_rows:
        return True

    def _to_tuples(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [tuple(_normalize_value(v) for v in row.values()) for row in rows]

    act_tuples = _to_tuples(actual_rows)
    exp_tuples = _to_tuples(expected_rows)

    def _tuple_match(t1: tuple[Any, ...], t2: tuple[Any, ...]) -> bool:
        if len(t1) != len(t2):
            return False
        for a, b in zip(t1, t2, strict=True):
            if isinstance(a, float) and isinstance(b, float):
                if abs(a - b) > abs_tol:
                    return False
            elif a != b:
                return False
        return True

    # Order-preserving match first
    if all(_tuple_match(a, e) for a, e in zip(act_tuples, exp_tuples, strict=True)):
        return True

    # Multiset fallback for non-tie-broken ordering
    unmatched = list(exp_tuples)
    for act in act_tuples:
        found_idx = next(
            (idx for idx, exp in enumerate(unmatched) if _tuple_match(act, exp)),
            None,
        )
        if found_idx is None:
            return False
        unmatched.pop(found_idx)
    return True


async def run_evaluation(provider: str = "local") -> dict[str, Any]:
    """Run all 30 benchmark tasks + 6 self-correction recovery trials."""
    settings = Settings.from_env()
    settings.provider = provider  # type: ignore[assignment]
    ensure_database(settings.db_path)

    agent = SQLAnalystAgent(settings)
    tasks: list[dict[str, Any]] = json.loads(TASKS_PATH.read_text(encoding="utf-8"))

    analytical_tasks = [t for t in tasks if t["task_type"] == "analytical"]
    adversarial_tasks = [t for t in tasks if t["task_type"] == "adversarial"]

    analytical_results: list[dict[str, Any]] = []
    adversarial_results: list[dict[str, Any]] = []
    recovery_results: list[dict[str, Any]] = []
    latencies_ms: list[float] = []

    # 1. Analytical 24 tasks
    for task in analytical_tasks:
        gt_exec = execute_readonly_sql(
            settings.db_path,
            task["ground_truth_sql"],
            max_rows=settings.max_rows,
            timeout_sec=settings.query_timeout_sec,
        )
        resp = await agent.ask(task["question"])
        latencies_ms.append(resp.total_latency_ms)

        valid_sql = resp.status == "completed" and resp.validation.is_valid and resp.execution is not None
        matched = (
            valid_sql
            and resp.execution is not None
            and _rows_equivalent(resp.execution.rows, gt_exec.rows)
        )
        analytical_results.append(
            {
                "id": task["id"],
                "difficulty": task["difficulty"],
                "language": task["language"],
                "question": task["question"],
                "status": resp.status,
                "valid_sql": valid_sql,
                "execution_match": matched,
                "row_count": resp.execution.row_count if resp.execution else 0,
                "expected_row_count": gt_exec.row_count,
                "latency_ms": resp.total_latency_ms,
                "chart_type": resp.chart.chart_type if resp.chart else None,
            }
        )

    # 2. Adversarial 6 tasks (must be 100% blocked)
    for task in adversarial_tasks:
        resp = await agent.ask(task["question"])
        latencies_ms.append(resp.total_latency_ms)
        blocked = resp.status == "blocked" and resp.execution is None
        adversarial_results.append(
            {
                "id": task["id"],
                "language": task["language"],
                "question": task["question"],
                "status": resp.status,
                "blocked": blocked,
                "blocked_reasons": resp.validation.blocked_reasons,
                "latency_ms": resp.total_latency_ms,
            }
        )

    # 3. Self-Correction Recovery Suite (6 tasks with injected first-draft SQL fault)
    task_by_id = {t["id"]: t for t in analytical_tasks}
    for tid in SELF_CORRECTION_TASK_IDS:
        task = task_by_id[tid]
        gt_exec = execute_readonly_sql(
            settings.db_path,
            task["ground_truth_sql"],
            max_rows=settings.max_rows,
            timeout_sec=settings.query_timeout_sec,
        )
        resp = await agent.ask(task["question"], simulate_initial_error=True)
        has_repair_step = any(s.step_name == "self_correct" for s in resp.trace)
        recovered = (
            resp.status == "completed"
            and resp.retries_used >= 1
            and has_repair_step
            and resp.execution is not None
            and _rows_equivalent(resp.execution.rows, gt_exec.rows)
        )
        recovery_results.append(
            {
                "id": tid,
                "difficulty": task["difficulty"],
                "language": task["language"],
                "retries_used": resp.retries_used,
                "has_repair_step": has_repair_step,
                "recovered_and_matched": recovered,
                "latency_ms": resp.total_latency_ms,
            }
        )

    # Compute aggregate metrics
    n_ana = len(analytical_results)
    n_adv = len(adversarial_results)
    n_rec = len(recovery_results)

    exec_acc = 100.0 * sum(1 for r in analytical_results if r["execution_match"]) / max(n_ana, 1)
    valid_rate = 100.0 * sum(1 for r in analytical_results if r["valid_sql"]) / max(n_ana, 1)
    safety_rate = 100.0 * sum(1 for r in adversarial_results if r["blocked"]) / max(n_adv, 1)
    recovery_rate = (
        100.0 * sum(1 for r in recovery_results if r["recovered_and_matched"]) / max(n_rec, 1)
    )

    lat_arr = np.array(latencies_ms, dtype=float)
    p50 = round(float(np.percentile(lat_arr, 50)), 2)
    p95 = round(float(np.percentile(lat_arr, 95)), 2)
    mean_lat = round(float(np.mean(lat_arr)), 2)

    by_difficulty: dict[str, dict[str, Any]] = {}
    for diff in ("easy", "medium", "hard"):
        subset = [r for r in analytical_results if r["difficulty"] == diff]
        passed = sum(1 for r in subset if r["execution_match"])
        by_difficulty[diff] = {
            "total": len(subset),
            "passed": passed,
            "accuracy_pct": round(100.0 * passed / max(len(subset), 1), 2),
        }

    by_language: dict[str, dict[str, Any]] = {}
    for lang in ("en", "id"):
        subset = [r for r in analytical_results if r["language"] == lang]
        passed = sum(1 for r in subset if r["execution_match"])
        by_language[lang] = {
            "total": len(subset),
            "passed": passed,
            "accuracy_pct": round(100.0 * passed / max(len(subset), 1), 2),
        }

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "provider": provider,
        "total_benchmark_tasks": len(tasks),
        "analytical_tasks": n_ana,
        "adversarial_tasks": n_adv,
        "self_correction_trials": n_rec,
        "metrics": {
            "execution_accuracy_pct": round(exec_acc, 2),
            "valid_sql_rate_pct": round(valid_rate, 2),
            "self_correction_recovery_rate_pct": round(recovery_rate, 2),
            "safety_block_rate_pct": round(safety_rate, 2),
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "latency_mean_ms": mean_lat,
        },
        "breakdown_by_difficulty": by_difficulty,
        "breakdown_by_language": by_language,
        "analytical_results": analytical_results,
        "adversarial_results": adversarial_results,
        "self_correction_results": recovery_results,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_markdown_summary(report: dict[str, Any]) -> None:
    m = report["metrics"]
    print("\n## SQL Analyst Agent — Benchmark Evaluation Report\n")
    print(f"- **Provider**: `{report['provider']}`")
    print(f"- **Tasks Evaluated**: {report['total_benchmark_tasks']} (24 analytical + 6 adversarial) + {report['self_correction_trials']} self-correction recovery trials")
    print(f"- **Report Saved**: `{REPORT_PATH}`\n")
    print("| Metric | Measured Value |")
    print("|---|---:|")
    print(f"| Execution Accuracy (24 tasks) | **{m['execution_accuracy_pct']:.2f}%** |")
    print(f"| Valid SQL Rate (24 tasks) | **{m['valid_sql_rate_pct']:.2f}%** |")
    print(f"| Self-Correction Recovery Rate ({report['self_correction_trials']} trials) | **{m['self_correction_recovery_rate_pct']:.2f}%** |")
    print(f"| Adversarial Safety Block Rate (6 tasks) | **{m['safety_block_rate_pct']:.2f}%** |")
    print(f"| Latency p50 | **{m['latency_p50_ms']:.2f} ms** |")
    print(f"| Latency p95 | **{m['latency_p95_ms']:.2f} ms** |")
    print(f"| Latency Mean | **{m['latency_mean_ms']:.2f} ms** |\n")

    print("### Breakdown by Difficulty\n")
    print("| Difficulty | Passed / Total | Accuracy |")
    print("|---|---:|---:|")
    for diff, stats in report["breakdown_by_difficulty"].items():
        print(f"| {diff.title()} | {stats['passed']} / {stats['total']} | {stats['accuracy_pct']:.2f}% |")

    print("\n### Breakdown by Language\n")
    print("| Language | Passed / Total | Accuracy |")
    print("|---|---:|---:|")
    for lang, stats in report["breakdown_by_language"].items():
        label = "English (EN)" if lang == "en" else "Indonesian (ID)"
        print(f"| {label} | {stats['passed']} / {stats['total']} | {stats['accuracy_pct']:.2f}% |")


def main() -> None:
    report = asyncio.run(run_evaluation(provider="local"))
    print_markdown_summary(report)


if __name__ == "__main__":
    main()
