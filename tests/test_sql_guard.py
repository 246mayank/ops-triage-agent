import sqlite3

import pytest

from triage.tools import sql

REFUSED = [
    "INSERT INTO orders (order_id, run_date) VALUES ('x', '2026-01-01')",
    "UPDATE orders SET amount = 0",
    "DELETE FROM orders",
    "DROP TABLE orders",
    "ATTACH DATABASE 'other.db' AS other",
    "PRAGMA table_info(orders)",
    "SELECT 1; DROP TABLE orders",
    "WITH x AS (SELECT 1) DELETE FROM orders",
    "WITH x AS (SELECT 1) INSERT INTO customers SELECT 'c', 'r', 't' FROM x",
    "SELECT * FROM pragma_table_info('orders')",
]


@pytest.mark.parametrize("query", REFUSED)
def test_refused(reg01, query):
    res = reg01.run("run_sql", {"query": query})
    assert not res.ok, query
    assert res.data == ""


def test_with_delete_passes_layer3_but_is_stopped_by_engine(inc01):
    # layer 3 alone lets this through; layers 1 and 2 must stop it
    q = sql.check_statement("WITH x AS (SELECT 1) DELETE FROM orders")
    with pytest.raises(sql.ToolError, match="read-only"):
        sql.execute(inc01 / "world.db", q)


def test_readonly_connection_refuses_write_without_authorizer(inc01):
    conn = sqlite3.connect((inc01 / "world.db").resolve().as_uri() + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM orders")
    conn.close()


def test_slow_recursive_cte_is_stopped(reg01):
    res = reg01.run("run_sql", {"query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                                         "SELECT x + 1 FROM c) SELECT max(x) FROM c"})
    assert not res.ok and "stopped" in res.note


def test_plain_select_works(reg01):
    res = reg01.run("run_sql", {"query": "SELECT COUNT(*) AS n FROM jobs"})
    assert res.ok and res.data.splitlines() == ["n", "7"]


def test_trailing_semicolon_allowed(reg01):
    assert reg01.run("run_sql", {"query": "SELECT 1 AS one;"}).ok


def test_row_cap_and_truncation_note(reg01):
    res = reg01.run("run_sql", {"query": "SELECT order_id FROM orders LIMIT 60"})
    assert res.ok and res.truncated
    assert len(res.data.splitlines()) == 1 + 50
    assert "more than 50" in res.note


def test_output_char_cap(reg01):
    res = reg01.run("run_sql", {"query": "SELECT * FROM orders LIMIT 50"})
    assert res.truncated and len(res.data) <= 2000


def test_sql_error_is_envelope_not_exception(reg01):
    res = reg01.run("run_sql", {"query": "SELECT * FROM order"})
    assert not res.ok and "sql error" in res.note


def test_describe_table(reg01):
    assert not reg01.run("describe_table", {"table": "sqlite_master"}).ok
    res = reg01.run("describe_table", {"table": "orders"})
    assert res.ok and "row_count:" in res.data


def test_unknown_tool_and_bad_args(reg01):
    assert "unknown tool" in reg01.run("drop_everything", {}).note
    assert "invalid arguments" in reg01.run("run_sql", {}).note
