"""read_config and config_history: file tools confined to the incident's config/ folder."""

from __future__ import annotations

import difflib
import re

from pydantic import BaseModel, Field

from triage.tools.logs import safe_path
from triage.tools.registry import Incident, Output, Tool, ToolError, trim


class ConfigPathArgs(BaseModel):
    path: str = Field(default="pipeline.yaml",
                      description="Config file name inside config/, e.g. pipeline.yaml")


def read_config(inc: Incident, a: ConfigPathArgs) -> Output:
    p = safe_path(inc, "config", a.path)
    if not p.is_file():
        files = ", ".join(f.name for f in inc.config.glob("*.yaml"))
        raise ToolError(f"no config file '{a.path}'; available: {files}")
    return trim(p.read_text(encoding="utf-8"))


READ_CONFIG = Tool(
    "read_config",
    "Read the current (deployed) config file, e.g. pipeline.yaml: schedule, vendor endpoint "
    "and token expiry, scheduler retry policy, loader settings, alert thresholds.",
    ConfigPathArgs, read_config)


_STAMP = re.compile(r"\.(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z\.yaml$")


def config_history(inc: Incident, a: ConfigPathArgs) -> Output:
    stem = a.path.removesuffix(".yaml")
    safe_path(inc, "config", a.path)  # same containment rule as read_config
    hist = safe_path(inc, "config", "history")
    versions = []
    for f in sorted(hist.glob(f"{stem}.*.yaml")):
        m = _STAMP.search(f.name)
        if m:
            when = f"{m[1]}T{m[2]}:{m[3]}:{m[4]}Z"
            versions.append((when, f.read_text(encoding="utf-8").splitlines()))
    if not versions:
        raise ToolError(f"no history for '{a.path}'")
    versions.sort()
    lines = [f"{len(versions)} versions, oldest first"]
    prev: list[str] | None = None
    for when, body in versions:
        if prev is None:
            lines.append(f"== version deployed {when} (oldest on record)")
        else:
            diff = [d for d in difflib.unified_diff(prev, body, lineterm="", n=0)
                    if not d.startswith(("---", "+++", "@@"))]
            lines.append(f"== version deployed {when}: {len(diff)} changed lines")
            lines += [f"   {d}" for d in diff]
        prev = body
    return trim("\n".join(lines))


CONFIG_HISTORY = Tool(
    "config_history",
    "Deploy history of a config file: every earlier version with its deploy timestamp and a "
    "diff against the version before it. Use when behaviour changed at a point in time.",
    ConfigPathArgs, config_history)
