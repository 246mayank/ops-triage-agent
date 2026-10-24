"""The agent: a bounded loop. Ask the model, run the tool it picks, append the result, and
repeat until it submits a diagnosis that passes validation or a budget runs out."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from triage.llm.base import LlmClient
from triage.schema import (
    CATEGORY_DEFINITIONS,
    SUBMIT_SPEC,
    Alert,
    Diagnosis,
    Message,
    ToolCall,
)
from triage.tools.registry import Registry, default_registry
from triage.trace import Trace, git_commit
from triage.validate import downgrade, validate

DEFAULT_PROMPT = "v1"


@dataclass
class Budget:
    max_steps: int = 12
    max_tool_calls: int = 16
    max_input_tokens: int = 60_000
    max_seconds: float = 120.0

    def exceeded(self, t: Trace) -> str | None:
        if t.tool_calls >= self.max_tool_calls:
            return "tool_calls"
        if t.input_tokens >= self.max_input_tokens:
            return "input_tokens"
        if (t.model_ms + t.tool_ms) / 1000 >= self.max_seconds:
            return "wall_clock"
        return None


@dataclass
class RunResult:
    diagnosis: Diagnosis
    trace: Trace
    end_reason: str
    trace_path: Path | None = None
    validation_errors: list[str] = field(default_factory=list)


def load_prompt(version: str = DEFAULT_PROMPT) -> str:
    text = resources.files("triage.prompts").joinpath(f"system_{version}.md").read_text(
        encoding="utf-8")
    cats = "\n".join(f"- {c.value}: {d}" for c, d in CATEGORY_DEFINITIONS.items())
    cats += ("\n(Use status no_fault when nothing is wrong, and insufficient_evidence when "
             "the cause cannot be established from the available data.)")
    return text.replace("{categories}", cats)


def render_alert(alert: Alert) -> str:
    return (f"ALERT {alert.alert_id} raised {alert.raised_at} by rule '{alert.rule}':\n"
            f"{alert.message}\n\nInvestigate with the tools and submit a diagnosis.")


def hash_call(call: ToolCall) -> str:
    return hashlib.sha1(json.dumps([call.name, call.args], sort_keys=True).encode()).hexdigest()


def investigate(alert: Alert, llm: LlmClient, tools: Registry, budget: Budget | None = None,
                prompt_version: str = DEFAULT_PROMPT, run_id: str | None = None,
                incident: str = "") -> RunResult:
    budget = budget or Budget()
    run_id = run_id or uuid.uuid4().hex[:12]
    trace = Trace(run_id, {
        "incident": incident, "alert_id": alert.alert_id, "alert_message": alert.message,
        "model": llm.model, "prompt_version": prompt_version, "commit": git_commit(),
        "budget": budget.__dict__})
    specs = tools.specs() + [SUBMIT_SPEC]
    messages = [Message(role="system", content=load_prompt(prompt_version)),
                Message(role="user", content=render_alert(alert))]
    seen: dict[str, str] = {}  # hash(tool, args) -> result id
    errors: list[str] = []

    def finish(diag: Diagnosis, reason: str, ungrounded: int = 0) -> RunResult:
        trace.ungrounded_quotes_final = ungrounded
        trace.footer = {"diagnosis": diag.model_dump(mode="json"), "end_reason": reason,
                        "validation_errors": errors}
        return RunResult(diag, trace, reason, validation_errors=errors)

    def submit(call: ToolCall, step: int) -> RunResult | None:
        """Validate a submission. Returns a result to end the run, or None to continue."""
        nonlocal errors
        diag, errs, ungrounded = validate(call.args, trace.results)
        if trace.submissions == 0:  # count fabrications before any repair feedback
            trace.quotes_caught += ungrounded
        trace.submissions += 1
        if not errs:
            return finish(diag, "submitted")
        errors = errs
        trace.log_note(step, "validation_failed", errs)
        if trace.repairs_used >= 1:  # one repair attempt only
            return finish(downgrade(diag, errs), "validation_failed")
        trace.repairs_used += 1
        messages.append(Message(
            role="tool", name=call.name, tool_call_id=call.id,
            content="submit_diagnosis rejected. Fix these problems and submit again:\n- "
                    + "\n- ".join(errs)))
        return None

    reason = "max_steps"
    for step in range(budget.max_steps):
        turn = llm.chat(messages, specs)
        trace.log_turn(step, turn)
        messages.append(Message(role="assistant", content=turn.text or "",
                                tool_calls=turn.tool_calls, provider_parts=turn.provider_parts))
        if not turn.tool_calls:  # text without a tool call
            messages.append(Message(role="user", content="Call a tool or submit_diagnosis."))
            trace.log_note(step, "nudge", "text-only turn")
            continue
        for call in turn.tool_calls:
            if call.name == "submit_diagnosis":
                done = submit(call, step)
                if done:
                    return done
                continue
            key = hash_call(call)
            if key in seen:  # loop detection
                messages.append(Message(role="tool", name=call.name, tool_call_id=call.id,
                                        content=f"Already ran this: see {seen[key]}"))
                trace.log_note(step, "repeat", f"{call.name} already ran as {seen[key]}")
                continue
            res = tools.run(call.name, call.args)
            rendered = res.render()
            seen[key] = res.id
            trace.log_result(step, res, rendered)
            messages.append(Message(role="tool", name=call.name, tool_call_id=call.id,
                                    content=rendered))
        exceeded = budget.exceeded(trace)
        if exceeded:
            reason = exceeded
            break
    return force_final(messages, llm, trace, step + 1, reason, submit, finish)


def force_final(messages, llm, trace, step, reason, submit, finish) -> RunResult:
    """One last turn where submit_diagnosis is the only tool."""
    trace.log_note(step, "budget_exhausted", reason)
    messages.append(Message(role="user",
                            content="Budget exhausted: submit what you can support."))
    turn = llm.chat(messages, [SUBMIT_SPEC], force_tool="submit_diagnosis")
    trace.log_turn(step, turn, forced=True)
    messages.append(Message(role="assistant", content=turn.text or "",
                            tool_calls=turn.tool_calls, provider_parts=turn.provider_parts))
    for call in turn.tool_calls:
        if call.name == "submit_diagnosis":
            trace.repairs_used = max(trace.repairs_used, 1)  # no repair turn left
            done = submit(call, step)
            if done:
                if done.end_reason == "submitted":
                    done.end_reason = f"forced:{reason}"
                    trace.footer["end_reason"] = done.end_reason
                return done
    diag = Diagnosis(status="insufficient_evidence", root_cause="Budget ran out before a "
                     "supported diagnosis was submitted.", confidence="low",
                     next_checks=[f"budget: {reason}"])
    return finish(diag, f"budget:{reason}")


def run_incident(incident_dir: Path, llm: LlmClient, budget: Budget | None = None,
                 prompt_version: str = DEFAULT_PROMPT, run_id: str | None = None,
                 trace_dir: Path | None = Path("runs")) -> RunResult:
    incident_dir = Path(incident_dir)
    alert = Alert.model_validate_json((incident_dir / "alert.json").read_text(encoding="utf-8"))
    result = investigate(alert, llm, default_registry(incident_dir), budget, prompt_version,
                         run_id, incident=incident_dir.name)
    if trace_dir is not None:
        result.trace_path = result.trace.write(Path(trace_dir))
    return result
