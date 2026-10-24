from conftest import INCIDENTS

from eval.gate import check
from eval.score import Expected, RequiredEvidence, score
from sandbox.build import build_all, build_incident, digests
from triage.schema import Diagnosis

# ---- generator -------------------------------------------------------------------------


def test_same_seed_gives_identical_incident(tmp_path):
    src = INCIDENTS / "07_timezone_boundary"
    a = digests(build_incident(src, tmp_path / "a"))
    b = digests(build_incident(src, tmp_path / "b"))
    assert a == b and "world.db" in a


def test_committed_checksums_match_generator():
    ok, diff = build_all(check=True)
    assert ok, diff


# ---- scorer ----------------------------------------------------------------------------

EXP = Expected("01", "dev", "diagnosed", "silent_data_loss",
               [RequiredEvidence("a", "3200"), RequiredEvidence("b", "1690"),
                RequiredEvidence("c", "E_INSERT_SUPPRESSED")], 2)


def D(**kw):
    base = dict(status="diagnosed", category="silent_data_loss", root_cause="r",
                evidence=[{"tool_call_id": "t1", "quote": "rows_read=3200 rows_loaded=1690",
                           "why": "w"}])
    base.update(kw)
    return Diagnosis.model_validate(base)


def test_strict_correct():
    s = score(D(), EXP, 0)
    assert s.strict and s.hits == ["a", "b"]


def test_too_few_hits():
    s = score(D(evidence=[{"tool_call_id": "t1", "quote": "rows_read=3200", "why": "w"}]), EXP, 0)
    assert s.category_ok and not s.strict


def test_ungrounded_fails_strict():
    assert not score(D(), EXP, 1).strict


def test_wrong_category():
    s = score(D(category="resource_exhaustion"), EXP, 0)
    assert s.status_ok and not s.category_ok and not s.strict


def test_abstention_scoring():
    exp = Expected("14", "test", "insufficient_evidence", None, [], 0)
    assert score(D(status="insufficient_evidence", category=None, evidence=[]), exp, 0).strict
    assert not score(D(), exp, 0).status_ok


def test_none_diagnosis():
    assert not score(None, EXP, 0).category_ok


# ---- gate ------------------------------------------------------------------------------


def report(strict, ungrounded=0, abst_ok=True):
    recs = [{"system": "agent", "incident": "13", "expected_status": "no_fault",
             "score": {"category_ok": abst_ok}}]
    return {"meta": {"runs": 1}, "records": recs,
            "summary": {"agent": {"strict_attempts": strict, "ungrounded_final": ungrounded}}}


def test_gate_pass():
    assert check(report(8), {"agent_strict": 8})[0]


def test_gate_one_below_allowed_two_not():
    assert check(report(7), {"agent_strict": 8})[0]
    assert not check(report(6), {"agent_strict": 8})[0]


def test_gate_ungrounded_fails():
    assert not check(report(8, ungrounded=1), {"agent_strict": 8})[0]


def test_gate_abstention_fails():
    assert not check(report(8, abst_ok=False), {"agent_strict": 8})[0]
