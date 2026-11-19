"""Tests for deterministic database seeding and schema introspection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sql_analyst.database import get_schema_context, get_schema_info, seed_database


def test_seeded_table_counts_and_integrity(seeded_db: Path) -> None:
    info = get_schema_info(seeded_db)
    counts = {t.name: t.row_count for t in info.tables}

    assert info.table_count == 6
    assert counts["warehouses"] == 5
    assert counts["customers"] == 120
    assert counts["products"] == 40
    assert counts["orders"] == 600
    assert 1200 <= counts["order_items"] <= 1650
    assert 450 <= counts["shipments"] <= 550

    with sqlite3.connect(seeded_db) as conn:
        fk_errors = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_errors == []


def test_seeding_is_deterministic(tmp_path: Path) -> None:
    db1 = seed_database(tmp_path / "a.db", force=True, seed=42)
    db2 = seed_database(tmp_path / "b.db", force=True, seed=42)

    with sqlite3.connect(db1) as c1, sqlite3.connect(db2) as c2:
        r1 = c1.execute("SELECT ROUND(SUM(total_amount), 2), COUNT(*) FROM orders").fetchone()
        r2 = c2.execute("SELECT ROUND(SUM(total_amount), 2), COUNT(*) FROM orders").fetchone()
        assert r1 == r2


def test_schema_context_includes_ddl_fks_and_samples(seeded_db: Path) -> None:
    ctx = get_schema_context(seeded_db, selected_tables=["orders", "warehouses"])
    assert "CREATE TABLE orders" in ctx
    assert "CREATE TABLE warehouses" in ctx
    assert "Foreign Keys:" in ctx
    assert "Sample Row:" in ctx
    assert "CREATE TABLE shipments" not in ctx
