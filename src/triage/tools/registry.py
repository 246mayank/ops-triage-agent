"""Tool registry: names, JSON schemas, the result envelope, output trimming and timing.

Every tool returns the same envelope (ok, data, truncated, note). Errors come back as
ok=False with a short message instead of raising, so the model can correct itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

MAX_CHARS = 2000


class ToolError(Exception):
    """Raised inside a tool for an expected, model-correctable problem."""


@dataclass
class Output:
    data: str
    truncated: bool = False
    note: str = ""


@dataclass
class ToolResult:
    id: str
    tool: str
    args: dict
    ok: bool
    data: str
    truncated: bool
    note: str
    ms: int

    def render(self) -> str:
        """The exact text the model sees. The grounding check matches quotes against this."""
        head = f"ok: {str(self.ok).lower()}\ntruncated: {str(self.truncated).lower()}"
        if self.note:
            head += f"\nnote: {self.note}"
        return (f'<tool_result id="{self.id}" tool="{self.tool}">\n{head}\ndata:\n'
                f"{self.data}\n</tool_result>")


@dataclass
class Incident:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "world.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def config(self) -> Path:
        return self.root / "config"


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Incident, Any], Output]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": json_schema(self.args_model)}


def json_schema(model: type[BaseModel]) -> dict:
    """A small, provider-friendly JSON schema (no anyOf/$defs) from a flat pydantic model."""
    props: dict[str, dict] = {}
    required: list[str] = []
    for name, f in model.model_fields.items():
        ann = f.annotation
        typ = "integer" if ann in (int, int | None) else "string"
        prop: dict[str, Any] = {"type": typ}
        if f.description:
            prop["description"] = f.description
        props[name] = prop
        if f.is_required():
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def trim(text: str, limit: int = MAX_CHARS, unit: str = "lines", hint: str = "") -> Output:
    """Cut text at a line boundary under `limit` chars and say how much was dropped."""
    if len(text) <= limit:
        return Output(text)
    lines = text.splitlines()
    kept: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            break
        kept.append(ln)
        size += len(ln) + 1
    more = len(lines) - len(kept)
    note = f"truncated: {more} more {unit}"
    if hint:
        note += f", {hint}"
    return Output("\n".join(kept), truncated=True, note=note)


@dataclass
class Registry:
    incident: Incident
    tools: dict[str, Tool]
    results: dict[str, ToolResult] = field(default_factory=dict)
    _n: int = 0

    def specs(self, only: set[str] | None = None) -> list[dict]:
        return [t.spec() for n, t in self.tools.items() if only is None or n in only]

    def run(self, name: str, args: dict) -> ToolResult:
        self._n += 1
        rid = f"t{self._n}"
        start = time.perf_counter()
        tool = self.tools.get(name)
        try:
            if tool is None:
                raise ToolError(f"unknown tool '{name}'; available: {', '.join(self.tools)}")
            try:
                parsed = tool.args_model.model_validate(args or {})
            except ValidationError as e:
                msgs = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'args'}: {x['msg']}"
                                 for x in e.errors())
                raise ToolError(f"invalid arguments: {msgs}") from None
            out = tool.fn(self.incident, parsed)
            if len(out.data) > MAX_CHARS:  # backstop: no tool may exceed the cap
                cut = trim(out.data)
                out = Output(cut.data, True, "; ".join(x for x in (out.note, cut.note) if x))
            ok = True
        except ToolError as e:
            out, ok = Output("", note=str(e)), False
        except Exception as e:  # noqa: BLE001 - a tool bug must not end the run
            out, ok = Output("", note=f"tool error: {type(e).__name__}: {e}"[:300]), False
        ms = int((time.perf_counter() - start) * 1000)
        res = ToolResult(rid, name, args or {}, ok, out.data, out.truncated, out.note, ms)
        self.results[rid] = res
        return res


def default_registry(incident_dir: Path) -> Registry:
    from triage.tools import config, jobs, logs, reconcile, sql

    tools = [sql.LIST_SOURCES, sql.DESCRIBE_TABLE, sql.RUN_SQL, logs.SEARCH_LOGS,
             jobs.GET_JOB_HISTORY, config.READ_CONFIG, config.CONFIG_HISTORY,
             reconcile.RECONCILE_COUNTS]
    return Registry(Incident(Path(incident_dir)), {t.name: t for t in tools})
