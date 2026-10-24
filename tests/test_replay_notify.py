import httpx
import pytest
from test_loop import ALERT, GOOD

from triage import notify
from triage.agent import investigate
from triage.llm.replay import RecordingClient, ReplayClient
from triage.llm.scripted import ScriptedClient, call, turn
from triage.schema import Alert, Diagnosis
from triage.tools.registry import default_registry

# ---- record / replay -------------------------------------------------------------------


def script():
    return ScriptedClient([turn(call("reconcile_counts", run_date="2026-10-21")),
                           turn(call("submit_diagnosis", **GOOD))])


def no_ms(events):
    return [{k: v for k, v in e.items() if k != "ms"} for e in events]


def test_recorded_run_replays_identically(inc01, tmp_path):
    cas = tmp_path / "c.json"
    a = investigate(ALERT, RecordingClient(script(), cas, salt="r1"), default_registry(inc01),
                    run_id="x")
    b = investigate(ALERT, ReplayClient(cas, model="scripted", salt="r1"),
                    default_registry(inc01), run_id="x")
    assert a.diagnosis == b.diagnosis and no_ms(a.trace.events) == no_ms(b.trace.events)


def test_replay_miss_raises(inc01, tmp_path):
    with pytest.raises(KeyError):
        investigate(ALERT, ReplayClient(tmp_path / "none.json", "scripted"),
                    default_registry(inc01))


def test_salt_separates_runs(inc01, tmp_path):
    cas = tmp_path / "c.json"
    investigate(ALERT, RecordingClient(script(), cas, salt="r1"), default_registry(inc01))
    with pytest.raises(KeyError):
        investigate(ALERT, ReplayClient(cas, "scripted", salt="r2"), default_registry(inc01))


# ---- webhook ---------------------------------------------------------------------------

A = Alert(alert_id="a-1", raised_at="t", rule="r", message="m")
DIAG = Diagnosis(status="no_fault", root_cause="fine", evidence=[], confidence="high")


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_same_alert_posts_once(tmp_path):
    calls = []
    c = client(lambda req: calls.append(req) or httpx.Response(200))
    db = tmp_path / "s.db"
    assert notify.post(A, DIAG, "r1", "http://hook", c, db) == "posted"
    assert notify.post(A, DIAG, "r2", "http://hook", c, db) == "duplicate"
    assert len(calls) == 1 and notify.existing_run("a-1", db) == "r1"


def test_retry_then_success(tmp_path):
    codes = iter([503, 200])
    c = client(lambda req: httpx.Response(next(codes)))
    assert notify.post(A, DIAG, "r1", "http://hook", c, tmp_path / "s.db", backoff=0) == "posted"


def test_retries_exhausted_logged_as_failed(tmp_path):
    def boom(req):
        raise httpx.ConnectError("down")

    res = notify.post(A, DIAG, "r1", "http://hook", client(boom), tmp_path / "s.db", backoff=0)
    assert res == "failed"


def test_claim_is_atomic(tmp_path):
    db = tmp_path / "s.db"
    assert notify.claim("x", "r1", db) and not notify.claim("x", "r2", db)


def test_no_webhook(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGE_WEBHOOK_URL", raising=False)
    assert notify.post(A, DIAG, "r1", None, None, tmp_path / "s.db") == "no_webhook"
