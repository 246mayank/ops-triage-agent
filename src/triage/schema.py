"""Data contracts: alerts, tool calls, messages and the diagnosis the agent must submit."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Category(StrEnum):
    silent_data_loss = "silent_data_loss"
    concurrency_race = "concurrency_race"
    stuck_job = "stuck_job"
    schema_drift = "schema_drift"
    auth_failure = "auth_failure"
    rate_limited = "rate_limited"
    timezone_boundary = "timezone_boundary"
    late_data = "late_data"
    empty_upstream = "empty_upstream"
    parse_error = "parse_error"
    resource_exhaustion = "resource_exhaustion"
    config_change = "config_change"


CATEGORY_DEFINITIONS = {
    Category.silent_data_loss: "rows were read but disappeared inside the pipeline without the "
                               "job failing",
    Category.concurrency_race: "two workers or processes acted on the same data at the same time",
    Category.stuck_job: "a run is stuck in a non-terminal state and nothing retries it",
    Category.schema_drift: "the upstream file's columns or types changed",
    Category.auth_failure: "credentials or tokens were rejected or expired",
    Category.rate_limited: "the upstream throttled requests (HTTP 429) and retries gave up",
    Category.timezone_boundary: "dates were bucketed in the wrong timezone, shifting rows "
                                "across days",
    Category.late_data: "data was fetched before the upstream finished revising it",
    Category.empty_upstream: "the upstream delivered an empty file and it was accepted",
    Category.parse_error: "a malformed record made parsing or validation reject data",
    Category.resource_exhaustion: "a host resource (disk, memory) ran out",
    Category.config_change: "a config deploy changed behaviour",
}

Status = Literal["diagnosed", "no_fault", "insufficient_evidence"]


class Alert(BaseModel):
    alert_id: str
    raised_at: str
    rule: str
    message: str


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class Message(BaseModel):
    """Provider-neutral chat message.

    role: system | user | assistant | tool. Assistant messages may carry tool_calls; tool
    messages answer one tool call (tool_call_id, name) with rendered text.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    provider_parts: list[dict] | None = None  # raw assistant parts, echoed back verbatim


class AssistantTurn(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    provider_parts: list[dict] | None = None


class Evidence(BaseModel):
    tool_call_id: str = Field(description="id of the tool result the quote comes from, e.g. t4")
    quote: str = Field(max_length=200, description="text copied exactly from that result")
    why: str = Field(description="one line: what this proves")


class Diagnosis(BaseModel):
    status: Status
    category: Category | None = None
    root_cause: str
    evidence: list[Evidence] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence: Literal["low", "medium", "high"] = "low"
    next_checks: list[str] = Field(default_factory=list)


SUBMIT_SPEC = {
    "name": "submit_diagnosis",
    "description": (
        "Submit your final diagnosis and end the investigation. Every evidence quote must be "
        "copied exactly from the data of the tool result you cite (max 200 characters). "
        "status=diagnosed requires a category and at least one evidence item; no_fault requires "
        "evidence that the data is healthy; insufficient_evidence should say what is missing "
        "in next_checks."),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                       "enum": ["diagnosed", "no_fault", "insufficient_evidence"]},
            "category": {"type": "string", "enum": [c.value for c in Category],
                         "description": "Required when status is diagnosed"},
            "root_cause": {"type": "string", "description": "1-3 sentences"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_call_id": {"type": "string"},
                        "quote": {"type": "string",
                                  "description": ("One line or short span copied verbatim, max "
                                                  "200 characters. For a table, quote one row "
                                                  "without the header line.")},
                        "why": {"type": "string"},
                    },
                    "required": ["tool_call_id", "quote", "why"],
                },
            },
            "suggested_fix": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_checks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "root_cause", "evidence", "suggested_fix", "confidence"],
    },
}
