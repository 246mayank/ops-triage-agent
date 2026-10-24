"""The two baselines the agent must beat. Both get one model turn and must answer with
submit_diagnosis; neither can call investigation tools.

B0 (alert only): the alert and the category list. It has nothing to cite.
B1 (context-stuffed): the alert plus a dump of everything that fits: table schemas, the
manifest, the last 20 job runs, the config and its history, every WARN and ERROR log line,
and the alert history. Each chunk is wrapped like a tool result with an id, so it can cite.
"""

from __future__ import annotations

import json
from pathlib import Path

from triage.agent import load_prompt, render_alert
from triage.schema import SUBMIT_SPEC, Alert, AssistantTurn, Message
from triage.tools.registry import default_registry

B1_MAX_CHARS = 60_000  # about 15k tokens: the whole dump fits for every incident in this set

B0_NOTE = ("You cannot call any investigation tools: answer from the alert alone with one "
           "submit_diagnosis call. You have no tool results, so leave evidence empty.")
B1_NOTE = ("You cannot call any investigation tools. Everything available about the pipeline "
           "is in the <tool_result> blocks below; cite their ids (c1, c2, ...) as "
           "tool_call_id. Answer with one submit_diagnosis call.")


def _system(prompt_version: str) -> str:
    return load_prompt(prompt_version)


def b0_messages(alert: Alert, prompt_version: str) -> tuple[list[Message], dict[str, str]]:
    msgs = [Message(role="system", content=_system(prompt_version)),
            Message(role="user", content=render_alert(alert) + "\n\n" + B0_NOTE)]
    return msgs, {}


def b1_chunks(incident_dir: Path) -> list[tuple[str, str, dict]]:
    """(tool name, data, args) for each chunk, generated with the same tools as the agent so
    the text matches what the agent would see."""
    reg = default_registry(incident_dir)
    calls = [("describe_table", {"table": t}) for t in
             ("jobs", "ingest_manifest", "orders", "customers", "alerts")]
    calls += [
        ("run_sql", {"query": "SELECT file_name, run_date, rows_read, rows_loaded, rows_rejected, "
                              "rows_deduped, checksum FROM ingest_manifest ORDER BY run_date"}),
        ("get_job_history", {"job_name": "order_sync", "limit": 20}),
        ("run_sql", {"query": "SELECT run_date, COUNT(*) AS orders FROM orders "
                              "GROUP BY run_date ORDER BY run_date"}),
        ("run_sql", {"query": "SELECT * FROM alerts ORDER BY raised_at"}),
        ("read_config", {"path": "pipeline.yaml"}),
        ("config_history", {"path": "pipeline.yaml"}),
    ]
    out = [(name, reg.run(name, args), args) for name, args in calls]
    # every WARN and ERROR line, all services, uncapped (the 40-line tool cap does not apply)
    for svc in ("scheduler", "downloader", "validator", "loader"):
        lines = []
        path = incident_dir / "logs" / f"{svc}.jsonl"
        for raw in path.read_text(encoding="utf-8").splitlines():
            d = json.loads(raw)
            if d["level"] in ("WARN", "ERROR"):
                lines.append(f"{d['ts']} {d['level']} {d['service']} "
                             f"[{d.get('job_id') or '-'}] {d['msg']}")
        out.append(("logs", _Raw("\n".join(lines) or "(no WARN or ERROR lines)"),
                    {"service": svc, "level": "WARN+ERROR"}))
    return [(name, r.data, args) for name, r, args in out]


class _Raw:
    def __init__(self, data: str):
        self.data = data


def b1_messages(alert: Alert, incident_dir: Path,
                prompt_version: str) -> tuple[list[Message], dict[str, str]]:
    results: dict[str, str] = {}
    blocks: list[str] = []
    size = 0
    for i, (name, data, args) in enumerate(b1_chunks(incident_dir), start=1):
        cid = f"c{i}"
        block = (f'<tool_result id="{cid}" tool="{name}" args=\'{json.dumps(args)}\'>\n'
                 f"{data}\n</tool_result>")
        if size + len(block) > B1_MAX_CHARS:
            break
        blocks.append(block)
        results[cid] = block
        size += len(block)
    content = render_alert(alert) + "\n\n" + B1_NOTE + "\n\n" + "\n\n".join(blocks)
    msgs = [Message(role="system", content=_system(prompt_version)),
            Message(role="user", content=content)]
    return msgs, results


def run_baseline(system: str, alert: Alert, incident_dir: Path, llm,
                 prompt_version: str) -> tuple[AssistantTurn, dict[str, str], list[Message]]:
    if system == "b0":
        msgs, results = b0_messages(alert, prompt_version)
    elif system == "b1":
        msgs, results = b1_messages(alert, incident_dir, prompt_version)
    else:
        raise ValueError(system)
    turn = llm.chat(msgs, [SUBMIT_SPEC], force_tool="submit_diagnosis")
    return turn, results, msgs
