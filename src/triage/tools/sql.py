"""SQL tools. run_sql is read-only through four independent layers:

1. the connection is opened with mode=ro, so the engine refuses writes;
2. an authorizer allow-list permits only SELECT, READ, FUNCTION and RECURSIVE actions;
3. the text must be one statement starting with SELECT or WITH;
4. execution is bounded: at most MAX_ROWS rows and TIME_LIMIT seconds.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel, Field

from triage.tools.registry import Incident, Output, Tool, ToolError, trim

MAX_ROWS = 50
TIME_LIMIT = 2.0
TABLES = ["jobs", "ingest_manifest", "orders", "customers", "alerts"]

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE}


def _authorizer(action: int, *_: object) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def connect_ro(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise ToolError("database not found")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)  # layer 1
    conn.set_authorizer(_authorizer)  # layer 2
    return conn


def check_statement(query: str) -> str:
    """Layer 3: exactly one statement, and it must start with SELECT or WITH."""
    q = query.strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if ";" in q:
        raise ToolError("only one statement is allowed (remove the ';')")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise ToolError("only SELECT or WITH ... SELECT queries are allowed")
    return q


def execute(db: Path, query: str, params: tuple = (), max_rows: int = MAX_ROWS,
            time_limit: float = TIME_LIMIT) -> tuple[list[str], list[tuple], bool]:
    """Run a checked read-only query. Returns (columns, rows, truncated)."""
    conn = connect_ro(db)
    deadline = time.monotonic() + time_limit
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)  # layer 4
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchmany(max_rows + 1)
        cols = [c[0] for c in cur.description or []]
    except sqlite3.DatabaseError as e:
        if "interrupted" in str(e):
            raise ToolError(f"query exceeded {time_limit:.0f}s and was stopped; "
                            "add a WHERE clause or LIMIT") from None
        if "not authorized" in str(e) or "readonly" in str(e):
            raise ToolError("refused: this tool is read-only") from None
        raise ToolError(f"sql error: {e}") from None
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    return cols, rows[:max_rows], truncated


def _fmt(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_table(cols: list[str], rows: list[tuple]) -> str:
    lines = [" | ".join(cols)]
    lines += [" | ".join(_fmt(v) for v in r) for r in rows]
    return "\n".join(lines)


# ---- run_sql ---------------------------------------------------------------------------


class RunSqlArgs(BaseModel):
    query: str = Field(description="One SQLite SELECT (or WITH ... SELECT) statement",
                       max_length=2000)


def run_sql(inc: Incident, args: RunSqlArgs) -> Output:
    q = check_statement(args.query)
    cols, rows, more = execute(inc.db, q)
    if not rows:
        return Output(" | ".join(cols), note="query returned 0 rows")
    out = trim(format_table(cols, rows))
    if out.truncated or more:
        shown = len(out.data.splitlines()) - 1
        total = f"more than {MAX_ROWS}" if more else str(len(rows))
        out.truncated = True
        out.note = (f"truncated: showing {shown} of {total} rows; "
                    "add a WHERE clause, GROUP BY or LIMIT")
    return out


RUN_SQL = Tool(
    "run_sql",
    "Run one read-only SQLite SELECT against the pipeline database (tables: jobs, "
    "ingest_manifest, orders, customers, alerts). Returns at most 50 rows, so aggregate "
    "(COUNT, GROUP BY run_date) instead of listing raw orders. Times are UTC ISO-8601 text.",
    RunSqlArgs, run_sql)


# ---- describe_table --------------------------------------------------------------------


class DescribeTableArgs(BaseModel):
    table: str = Field(description="Table name: " + ", ".join(TABLES))


def describe_table(inc: Incident, args: DescribeTableArgs) -> Output:
    if args.table not in TABLES:
        raise ToolError(f"unknown table '{args.table}'; known tables: {', '.join(TABLES)}")
    conn = connect_ro(inc.db)
    try:
        # PRAGMA is denied by the authorizer, so read the schema from sqlite_master instead.
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                           (args.table,)).fetchone()[0]
    finally:
        conn.close()
    _, rows, _ = execute(inc.db, f"SELECT COUNT(*) FROM {args.table}")
    return Output(f"{sql.strip()}\nrow_count: {rows[0][0]}")


DESCRIBE_TABLE = Tool(
    "describe_table",
    "Show a table's columns, types and row count.",
    DescribeTableArgs, describe_table)


# ---- list_sources ----------------------------------------------------------------------


class NoArgs(BaseModel):
    pass


def list_sources(inc: Incident, _: NoArgs) -> Output:
    lines = ["tables:"]
    for t in TABLES:
        _, rows, _ = execute(inc.db, f"SELECT COUNT(*) FROM {t}")
        lines.append(f"  {t} ({rows[0][0]} rows)")
    lines.append("log services:")
    first, last = None, None
    for f in sorted(inc.logs.glob("*.jsonl")):
        text = f.read_text(encoding="utf-8").splitlines()
        lines.append(f"  {f.stem} ({len(text)} lines)")
        for ln in (text[:1] + text[-1:]) if text else []:
            stamp = ln.split('"ts": "', 1)[1][:20]
            first = min(first or stamp, stamp)
            last = max(last or stamp, stamp)
    lines.append("config files:")
    for f in sorted(inc.config.glob("*.yaml")):
        lines.append(f"  {f.name}")
    n_hist = len(list((inc.config / "history").glob("*.yaml")))
    lines.append(f"  history/ ({n_hist} earlier versions; use config_history)")
    lines.append(f"log time range: {first} .. {last}")
    return Output("\n".join(lines))


LIST_SOURCES = Tool(
    "list_sources",
    "List the tables, log services, config files and the time range of the data. "
    "Cheap orientation call.",
    NoArgs, list_sources)
