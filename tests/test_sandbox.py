"""Exhaustive security & runtime tests for the read-only SQL sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from sql_analyst.sandbox import (
    SQLExecutionError,
    SQLSecurityError,
    SQLTimeoutError,
    execute_readonly_sql,
    validate_sql,
)


@pytest.mark.parametrize(
    "dangerous_sql",
    [
        "DROP TABLE orders",
        "DELETE FROM orders WHERE order_id = 1",
        "UPDATE orders SET status = 'completed'",
        "INSERT INTO warehouses VALUES (9, 'WH-X', 'X', 'X', 'X', 'X', 100)",
        "ALTER TABLE orders ADD COLUMN hack TEXT",
        "CREATE TABLE pwn (id INT)",
        "REPLACE INTO warehouses (warehouse_id) VALUES (1)",
        "ATTACH DATABASE '/tmp/pwn.db' AS pwn",
        "DETACH DATABASE main",
        "PRAGMA table_info(orders)",
        "VACUUM",
        "REINDEX",
        "SELECT load_extension('evil.so')",
        "SELECT * FROM orders; DROP TABLE customers",
        "SELECT 1; DELETE FROM order_items;",
        "WITH cte AS (SELECT 1) DELETE FROM orders",
        "WITH cte AS (SELECT 1) UPDATE orders SET status = 'completed'",
        "WITH cte AS (SELECT 1) INSERT INTO warehouses (warehouse_id) VALUES (99)",
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM pragma_table_info('orders')",
        "ANALYZE orders",
        "SELECT 1\x00; DROP TABLE orders",
        "SELECT 'unterminated; DROP TABLE orders",
        'SELECT "unterminated FROM orders',
        "/* unclosed comment SELECT 1",
        "   ",
        "-- only a comment",
    ],
)
def test_static_validator_blocks_unsafe_sql(dangerous_sql: str) -> None:
    result = validate_sql(dangerous_sql)
    assert result.is_valid is False
    assert len(result.blocked_reasons) >= 1


@pytest.mark.parametrize(
    "safe_sql",
    [
        "SELECT COUNT(*) AS n FROM orders;",
        "SELECT status, COUNT(*) FROM orders GROUP BY status",
        "WITH rev AS (SELECT warehouse_id, SUM(total_amount) AS rev FROM orders GROUP BY warehouse_id) SELECT * FROM rev",
        "-- Monthly count\nSELECT strftime('%Y-%m', order_date) AS m, COUNT(*) FROM orders /* safe */ GROUP BY m",
        "SELECT 'DROP TABLE; DELETE FROM orders' AS harmless_literal, COUNT(*) FROM warehouses",
        "\ufeffSELECT COUNT(*) AS bom_prefixed FROM warehouses;",
    ],
)
def test_static_validator_allows_safe_readonly_sql(safe_sql: str) -> None:
    result = validate_sql(safe_sql)
    assert result.is_valid is True
    assert result.blocked_reasons == []


def test_runtime_sandbox_enforces_security_and_row_cap(seeded_db: Path) -> None:
    with pytest.raises(SQLSecurityError):
        execute_readonly_sql(seeded_db, "DELETE FROM orders")

    res = execute_readonly_sql(seeded_db, "SELECT * FROM orders", max_rows=15)
    assert res.row_count == 15
    assert res.truncated is True
    assert "order_id" in res.columns


def test_runtime_sandbox_handles_special_chars_in_db_path(tmp_path: Path) -> None:
    from sql_analyst.database import seed_database

    special_dir = tmp_path / "analytics #1"
    special_dir.mkdir()
    db_file = seed_database(special_dir / "my store.db", force=True, seed=42)
    res = execute_readonly_sql(db_file, "SELECT COUNT(*) AS n FROM warehouses")
    assert res.rows == [{"n": 5}]


def test_runtime_sandbox_enforces_timeout(seeded_db: Path) -> None:
    heavy_cte = (
        "WITH RECURSIVE cnt(x) AS ("
        "SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 5000000"
        ") SELECT SUM(x) FROM cnt"
    )
    with pytest.raises(SQLTimeoutError):
        execute_readonly_sql(seeded_db, heavy_cte, max_rows=10, timeout_sec=0.005)


def test_runtime_sandbox_raises_execution_error_on_bad_column(seeded_db: Path) -> None:
    with pytest.raises(SQLExecutionError):
        execute_readonly_sql(seeded_db, "SELECT nonexistent_col FROM orders")
