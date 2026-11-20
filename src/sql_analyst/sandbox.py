"""Strict Read-Only SQL Security Sandbox (static validator + runtime SQLite enforcement)."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from sql_analyst.schemas import SQLExecutionResult, ValidationResult

FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "CREATE",
        "REPLACE",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "VACUUM",
        "REINDEX",
        "LOAD_EXTENSION",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "BEGIN",
        "EXEC",
        "EXECUTE",
        "MERGE",
        "UPSERT",
        "ANALYZE",
        "FTS3_TOKENIZER",
    }
)

_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "load_extension",
        "fts3_tokenizer",
    }
)

_ALLOWED_AUTHORIZER_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

ALLOWED_START_KEYWORDS: frozenset[str] = frozenset({"SELECT", "WITH"})

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)[\"`\]]?)",
    re.IGNORECASE,
)


class SQLSecurityError(Exception):
    """Raised when SQL violates the read-only security sandbox policy."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class SQLTimeoutError(Exception):
    """Raised when SQL execution exceeds the configured timeout budget."""


class SQLExecutionError(Exception):
    """Raised when SQLite fails to execute a validated read-only query."""


def _strip_comments_and_extract_code(sql: str) -> tuple[str, str, bool, bool]:
    """Strip SQL comments while preserving string literals.

    Returns:
        (normalized_sql, code_without_literals, has_unclosed_comment, has_unclosed_quote)
    """
    n = len(sql)
    i = 0
    out_chars: list[str] = []
    code_chars: list[str] = []
    unclosed_comment = False
    unclosed_quote = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # Single-line comment: -- ... \n
        if ch == "-" and nxt == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            out_chars.append(" ")
            code_chars.append(" ")
            continue

        # Multi-line block comment: /* ... */
        if ch == "/" and nxt == "*":
            i += 2
            closed = False
            while i < n - 1:
                if sql[i] == "*" and sql[i + 1] == "/":
                    i += 2
                    closed = True
                    break
                i += 1
            if not closed:
                unclosed_comment = True
                break
            out_chars.append(" ")
            code_chars.append(" ")
            continue

        # Single-quoted string literal ('...')
        if ch == "'":
            out_chars.append(ch)
            code_chars.append(" ")
            i += 1
            closed_str = False
            while i < n:
                out_chars.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out_chars.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    closed_str = True
                    break
                i += 1
            if not closed_str:
                unclosed_quote = True
                break
            continue

        # Double-quoted identifier ("...")
        if ch == '"':
            out_chars.append(ch)
            code_chars.append(ch)
            i += 1
            closed_ident = False
            while i < n:
                out_chars.append(sql[i])
                code_chars.append(sql[i])
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out_chars.append(sql[i + 1])
                        code_chars.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    closed_ident = True
                    break
                i += 1
            if not closed_ident:
                unclosed_quote = True
                break
            continue

        out_chars.append(ch)
        code_chars.append(ch)
        i += 1

    normalized = " ".join("".join(out_chars).split()).strip()
    code_only = "".join(code_chars)
    return normalized, code_only, unclosed_comment, unclosed_quote


def _readonly_authorizer(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    """SQLite C-level authorizer callback restricting execution strictly to read-only operations."""
    del arg1, db_name, trigger_name
    if action_code == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in _FORBIDDEN_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action_code in _ALLOWED_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def validate_sql(sql: str) -> ValidationResult:
    """Validate SQL statically against strict read-only sandbox rules."""
    if not sql or not sql.strip():
        return ValidationResult(
            is_valid=False,
            normalized_sql="",
            blocked_reasons=["Empty SQL statement is not allowed."],
            statement_type="EMPTY",
        )

    if "\x00" in sql:
        return ValidationResult(
            is_valid=False,
            normalized_sql="",
            blocked_reasons=["Null bytes are not allowed in SQL statements."],
            statement_type="INVALID",
        )

    cleaned_input = sql.lstrip("\ufeff")
    normalized, code_only, unclosed_comment, unclosed_quote = _strip_comments_and_extract_code(
        cleaned_input
    )
    reasons: list[str] = []

    if unclosed_comment:
        reasons.append("Unclosed block comment detected.")

    if unclosed_quote:
        reasons.append("Unclosed string literal or quoted identifier detected.")

    if not normalized:
        reasons.append("SQL statement is empty after stripping comments.")
        return ValidationResult(
            is_valid=False,
            normalized_sql="",
            blocked_reasons=reasons,
            statement_type="EMPTY",
        )

    # Stacked / multi-statement check outside string literals
    code_stripped = code_only.strip()
    if ";" in code_stripped:
        # Allow only a single trailing semicolon
        without_trailing = code_stripped[:-1] if code_stripped.endswith(";") else code_stripped
        if ";" in without_trailing:
            reasons.append("Stacked / multi-statement SQL queries are strictly forbidden.")

    # Normalize trailing semicolon away for execution cleanliness
    exec_sql = normalized[:-1].strip() if normalized.endswith(";") else normalized

    # Extract all identifier/keyword tokens from code outside string literals
    tokens = [m.group(0).upper() for m in _WORD_RE.finditer(code_only)]
    statement_type = tokens[0] if tokens else "UNKNOWN"

    if not tokens:
        reasons.append("No executable SQL keywords found.")
    elif statement_type not in ALLOWED_START_KEYWORDS:
        reasons.append(
            f"Only read-only SELECT or WITH ... SELECT statements are permitted (got '{statement_type}')."
        )
    elif statement_type == "WITH" and "SELECT" not in tokens:
        reasons.append("WITH CTE query must contain a SELECT statement.")

    # Scan for forbidden DDL/DML/admin keywords and PRAGMA_* table-valued functions
    found_forbidden = sorted(
        {tok for tok in tokens if tok in FORBIDDEN_KEYWORDS or tok.startswith("PRAGMA_")}
    )
    if found_forbidden:
        reasons.append(
            f"Forbidden SQL keyword(s) detected: {', '.join(found_forbidden)}."
        )

    # Extract referenced table names for auditability
    tables_referenced = sorted(
        {m.group(1).lower() for m in _TABLE_REF_RE.finditer(code_only)}
    )

    return ValidationResult(
        is_valid=len(reasons) == 0,
        normalized_sql=exec_sql,
        blocked_reasons=reasons,
        statement_type=statement_type,
        tables_referenced=tables_referenced,
    )


def execute_readonly_sql(
    db_path: str | Path,
    sql: str,
    max_rows: int = 200,
    timeout_sec: float = 5.0,
) -> SQLExecutionResult:
    """Execute SQL inside a strictly read-only SQLite connection with row cap & timeout."""
    validation = validate_sql(sql)
    if not validation.is_valid:
        raise SQLSecurityError(validation.blocked_reasons)

    target = Path(db_path).resolve()
    if not target.exists():
        raise SQLExecutionError(f"Database file not found: {target.name}")

    start_ts = time.perf_counter()
    timed_out = False

    def _progress_handler() -> int:
        nonlocal timed_out
        if (time.perf_counter() - start_ts) > timeout_sec:
            timed_out = True
            return 1
        return 0

    conn = sqlite3.connect(
        f"{target.as_uri()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        # Runtime defense-in-depth: query_only pragma + C-level authorizer + progress timeout
        conn.execute("PRAGMA query_only = ON;")
        conn.set_authorizer(_readonly_authorizer)
        conn.set_progress_handler(_progress_handler, 500)

        cursor = conn.execute(validation.normalized_sql)
        columns = [desc[0] for desc in (cursor.description or [])]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        limited_rows = fetched[:max_rows]
        rows: list[dict[str, Any]] = [dict(r) for r in limited_rows]
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

        return SQLExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            execution_time_ms=elapsed_ms,
        )
    except sqlite3.OperationalError as exc:
        if timed_out or "interrupted" in str(exc).lower():
            raise SQLTimeoutError(
                f"Query exceeded read-only execution timeout of {timeout_sec:.2f}s."
            ) from exc
        raise SQLExecutionError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        if "not authorized" in str(exc).lower():
            raise SQLSecurityError([f"SQLite authorizer denied operation: {exc}"]) from exc
        raise SQLExecutionError(str(exc)) from exc
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.close()
