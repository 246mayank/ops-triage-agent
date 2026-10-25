"""Grounding check: every evidence quote must appear in the tool result it cites."""

from __future__ import annotations

import re

from pydantic import ValidationError

from triage.schema import Diagnosis

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def validate(args: dict, results: dict[str, str]) -> tuple[Diagnosis | None, list[str], int]:
    """Check a submit_diagnosis payload against this run's tool results.

    results maps tool-call id -> the exact text the model saw for that result.
    Returns (diagnosis or None, errors, number of ungrounded quotes).
    """
    if args.get("status") in ("no_fault", "insufficient_evidence") and args.get("category"):
        # A category here is meaningless, not ungrounded (models often echo the status): drop it.
        args = {**args, "category": None}
    try:
        diag = Diagnosis.model_validate(args)
    except ValidationError as e:
        errs = [f"schema: {'.'.join(map(str, x['loc'])) or 'diagnosis'}: {x['msg']}"
                for x in e.errors()]
        return None, errs, 0

    errors: list[str] = []
    if diag.status == "diagnosed" and diag.category is None:
        errors.append("category is required when status is diagnosed")
    if diag.status == "diagnosed" and not diag.evidence:
        errors.append("status diagnosed needs at least one evidence item")
    if diag.status == "no_fault" and not diag.evidence:
        errors.append("status no_fault needs evidence that the data is healthy")

    ungrounded = 0
    for i, ev in enumerate(diag.evidence, start=1):
        src = results.get(ev.tool_call_id)
        if src is None:
            errors.append(f"evidence {i}: unknown tool_call_id '{ev.tool_call_id}' "
                          f"(valid ids: {', '.join(results) or 'none'})")
            ungrounded += 1
            continue
        q = normalise(ev.quote)
        if not q:
            errors.append(f"evidence {i}: quote is empty")
            ungrounded += 1
        elif q not in normalise(src):
            errors.append(f"evidence {i}: quote not found in {ev.tool_call_id}; copy one "
                          "contiguous span exactly from that result (for a table, one row "
                          "without the header)")
            ungrounded += 1
    return diag, errors, ungrounded


def downgrade(diag: Diagnosis | None, errors: list[str]) -> Diagnosis:
    """After a failed repair: keep only what is grounded, and say so."""
    return Diagnosis(
        status="insufficient_evidence",
        category=None,
        root_cause=(diag.root_cause if diag else "No valid diagnosis was submitted.")
        + " [downgraded: validation_failed]",
        evidence=[],
        suggested_fix=diag.suggested_fix if diag else "",
        confidence="low",
        next_checks=[f"validator: {e}" for e in errors][:5],
    )
