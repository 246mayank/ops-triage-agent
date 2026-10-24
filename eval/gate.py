"""The regression gate. A report fails when, against the accepted baseline:
- agent strict-correct drops by more than 1 incident,
- any quote in a final diagnosis is ungrounded, or
- any abstention incident (expected no_fault / insufficient_evidence) is wrong."""

from __future__ import annotations

ABSTAIN = {"no_fault", "insufficient_evidence"}


def check(report: dict, baseline: dict) -> tuple[bool, list[str]]:
    agent = report["summary"]["agent"]
    lines, ok = [], True
    floor = baseline["agent_strict"] - 1
    got = agent["strict_attempts"] / max(1, report["meta"]["runs"])
    line = f"strict correct: {got:g} (baseline {baseline['agent_strict']}, floor {floor})"
    if got < floor:
        ok = False
        line += "  FAIL"
    lines.append(line)
    if agent["ungrounded_final"]:
        ok = False
        lines.append(f"ungrounded quotes accepted: {agent['ungrounded_final']}  FAIL")
    else:
        lines.append("ungrounded quotes accepted: 0")
    bad = [r["incident"] for r in report["records"] if r["system"] == "agent"
           and r["expected_status"] in ABSTAIN and not r["score"]["category_ok"]]
    if bad:
        ok = False
        lines.append(f"abstention wrong on: {', '.join(sorted(set(bad)))}  FAIL")
    else:
        lines.append("abstention incidents: all correct")
    lines.append("GATE PASSED" if ok else "GATE FAILED")
    return ok, lines


def summary_markdown(report: dict, ok: bool, lines: list[str]) -> str:
    return "\n".join(["## Eval gate: " + ("passed" if ok else "FAILED"), "",
                      *[f"- {ln}" for ln in lines[:-1]], ""])
