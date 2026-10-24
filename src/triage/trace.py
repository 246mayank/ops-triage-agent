"""Per-run JSONL trace (header, one line per step, footer) and markdown rendering."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from triage.schema import AssistantTurn
from triage.tools.registry import ToolResult


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "uncommitted"
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class Trace:
    run_id: str
    header: dict[str, Any]
    events: list[dict] = field(default_factory=list)
    footer: dict[str, Any] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)  # result id -> text the model saw
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_ms: int = 0
    tool_ms: int = 0
    repairs_used: int = 0
    submissions: int = 0
    quotes_caught: int = 0  # ungrounded quotes found before any repair
    ungrounded_quotes_final: int = 0

    def log_turn(self, step: int, turn: AssistantTurn, forced: bool = False) -> None:
        self.turns += 1
        self.input_tokens += turn.usage.input_tokens
        self.output_tokens += turn.usage.output_tokens
        self.model_ms += turn.usage.latency_ms
        self.events.append({
            "type": "turn", "step": step, "forced": forced, "text": turn.text,
            "tool_calls": [c.model_dump() for c in turn.tool_calls],
            "input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens,
            "latency_ms": turn.usage.latency_ms})

    def log_result(self, step: int, res: ToolResult, rendered: str) -> None:
        self.tool_calls += 1
        self.tool_ms += res.ms
        self.results[res.id] = rendered
        self.events.append({
            "type": "tool", "step": step, "id": res.id, "tool": res.tool, "args": res.args,
            "ok": res.ok, "truncated": res.truncated, "note": res.note, "chars": len(res.data),
            "ms": res.ms, "data": res.data})

    def log_note(self, step: int, kind: str, detail: Any) -> None:
        self.events.append({"type": kind, "step": step, "detail": detail})

    def cost(self) -> dict:
        return {"turns": self.turns, "tool_calls": self.tool_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round((self.model_ms + self.tool_ms) / 1000, 2)}

    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.run_id}.jsonl"
        lines = [{"type": "header", "run_id": self.run_id, **self.header}, *self.events,
                 {"type": "footer", **self.footer, "totals": self.cost(),
                  "repairs_used": self.repairs_used, "quotes_caught": self.quotes_caught,
                  "ungrounded_quotes_final": self.ungrounded_quotes_final}]
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                        encoding="utf-8", newline="\n")
        return path


def render_markdown(path: Path, max_data: int = 600) -> str:
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]
    h, f = rows[0], rows[-1]
    out = [f"# Trace {h['run_id']}", "",
           f"- incident: `{h.get('incident')}`  alert: `{h.get('alert_id')}`",
           f"- model: `{h.get('model')}`  prompt: `{h.get('prompt_version')}`  "
           f"commit: `{h.get('commit')}`", "",
           f"> **Alert:** {h.get('alert_message')}", ""]
    for e in rows[1:-1]:
        if e["type"] == "turn":
            tag = " (forced final turn)" if e.get("forced") else ""
            out.append(f"### Step {e['step'] + 1}{tag}")
            if e.get("text"):
                out.append(f"_model:_ {e['text'].strip()}")
            for c in e["tool_calls"]:
                args = json.dumps(c["args"], ensure_ascii=False)
                out.append(f"- **call** `{c['name']}` `{args}`")
        elif e["type"] == "tool":
            status = "ok" if e["ok"] else "error"
            data = e["data"] if len(e["data"]) <= max_data else e["data"][:max_data] + " …"
            note = f" — {e['note']}" if e.get("note") else ""
            out += [f"- **{e['id']}** `{e['tool']}` → {status}{note}", "", "```", data, "```"]
        else:
            out.append(f"- _{e['type']}_: {e['detail']}")
        out.append("")
    d = f.get("diagnosis") or {}
    out += ["## Diagnosis", "",
            f"- **status:** {d.get('status')}  **category:** {d.get('category')}  "
            f"**confidence:** {d.get('confidence')}",
            f"- **root cause:** {d.get('root_cause')}"]
    for ev in d.get("evidence", []):
        out.append(f"  - `{ev['tool_call_id']}`: \"{ev['quote']}\" — {ev['why']}")
    out += [f"- **fix:** {d.get('suggested_fix')}", "",
            f"Validation: repairs={f.get('repairs_used')}, quotes caught={f.get('quotes_caught')}, "
            f"ungrounded at end={f.get('ungrounded_quotes_final')}, end reason="
            f"`{f.get('end_reason')}`. Totals: {f.get('totals')}"]
    return "\n".join(out) + "\n"
