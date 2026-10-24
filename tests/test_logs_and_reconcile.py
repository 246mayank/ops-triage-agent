def lines(res):
    return res.data.splitlines() if res.data else []


def test_level_filter(reg01):
    res = reg01.run("search_logs", {"service": "loader", "level": "WARN"})
    assert res.ok and lines(res)
    assert all(" WARN " in ln or " ERROR " in ln for ln in lines(res))


def test_job_and_time_filter(reg01):
    res = reg01.run("search_logs", {"service": "all", "job_id": "os-20261021",
                                    "since": "2026-10-22T02:01:00Z",
                                    "until": "2026-10-22T02:02:00Z"})
    assert res.ok and lines(res)
    for ln in lines(res):
        assert "[os-20261021]" in ln
        assert "2026-10-22T02:01:00Z" <= ln[:20] <= "2026-10-22T02:02:00Z"


def test_newest_first(reg01):
    ts = [ln[:20] for ln in lines(reg01.run("search_logs", {"service": "downloader"}))]
    assert ts == sorted(ts, reverse=True)


def test_line_limit(reg01):
    res = reg01.run("search_logs", {"service": "all", "limit": 500})
    assert len(lines(res)) <= 40 and res.truncated and "more lines" in res.note


def test_overlong_pattern_refused(reg01):
    assert not reg01.run("search_logs", {"service": "loader", "pattern": "a" * 201}).ok


def test_invalid_regex_is_envelope(reg01):
    res = reg01.run("search_logs", {"service": "loader", "pattern": "("})
    assert not res.ok and "invalid regex" in res.note


def test_pattern_case_insensitive(reg01):
    res = reg01.run("search_logs", {"service": "loader", "pattern": "e_insert_suppressed"})
    assert len(lines(res)) == 3


def test_reconcile_healthy_day_gap_zero(reg01):
    res = reg01.run("reconcile_counts", {"run_date": "2026-10-20"})
    assert res.ok and " gap=0 " in res.data and "reconciles" in res.note


def test_reconcile_lossy_day(reg01):
    res = reg01.run("reconcile_counts", {"file_name": "orders_2026-10-21.csv"})
    assert "rows_read=3200 rows_loaded=1690" in res.data and "gap=1500" in res.data


def test_reconcile_missing_manifest(reg01):
    res = reg01.run("reconcile_counts", {"run_date": "2026-09-01"})
    assert res.ok and "manifest=none" in res.data


def test_reconcile_needs_an_argument(reg01):
    assert not reg01.run("reconcile_counts", {}).ok
