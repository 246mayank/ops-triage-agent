from triage.validate import downgrade, validate

RESULTS = {"t1": '<tool_result id="t1" tool="reconcile_counts">\ndata:\n'
                 "file=orders_2026-10-21.csv rows_read=3200 rows_loaded=1690 gap=1500\n"
                 "</tool_result>"}


def diag(**kw):
    d = {"status": "diagnosed", "category": "silent_data_loss", "root_cause": "x",
         "evidence": [{"tool_call_id": "t1", "quote": "rows_read=3200 rows_loaded=1690",
                       "why": "gap"}],
         "suggested_fix": "y", "confidence": "high"}
    d.update(kw)
    return d


def ev(quote, tid="t1"):
    return [{"tool_call_id": tid, "quote": quote, "why": "w"}]


def test_valid():
    d, errs, ung = validate(diag(), RESULTS)
    assert d and errs == [] and ung == 0


def test_unknown_tool_call_id():
    _, errs, ung = validate(diag(evidence=ev("x", "t9")), RESULTS)
    assert ung == 1 and "unknown tool_call_id 't9'" in errs[0]


def test_quote_not_in_result():
    _, errs, ung = validate(diag(evidence=ev("rows_loaded=1700")), RESULTS)
    assert ung == 1 and "quote not found in t1" in errs[0]


def test_case_and_whitespace_differences_pass():
    _, errs, _ = validate(diag(evidence=ev("  ROWS_READ=3200   rows_loaded=1690 ")), RESULTS)
    assert errs == []


def test_category_missing_when_diagnosed():
    _, errs, _ = validate(diag(category=None), RESULTS)
    assert any("category is required" in e for e in errs)


def test_category_must_be_empty_when_abstaining():
    _, errs, _ = validate(diag(status="insufficient_evidence", evidence=[]), RESULTS)
    assert any("category must be empty" in e for e in errs)


def test_no_fault_needs_evidence():
    _, errs, _ = validate(diag(status="no_fault", category=None, evidence=[]), RESULTS)
    assert any("no_fault needs evidence" in e for e in errs)


def test_schema_error_reported():
    d, errs, _ = validate({"status": "maybe"}, RESULTS)
    assert d is None and errs and all(e.startswith("schema:") for e in errs)


def test_quote_over_200_chars_rejected():
    _, errs, _ = validate(diag(evidence=ev("x" * 201)), RESULTS)
    assert errs and errs[0].startswith("schema:")


def test_downgrade_drops_evidence():
    d, errs, _ = validate(diag(evidence=ev("nope")), RESULTS)
    g = downgrade(d, errs)
    assert g.status == "insufficient_evidence" and g.evidence == [] and g.category is None
    assert "validation_failed" in g.root_cause
