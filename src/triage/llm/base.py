"""Provider-neutral LLM interface. The agent only ever talks to this."""

from __future__ import annotations

from typing import Protocol

from triage.schema import AssistantTurn, Message


class LlmClient(Protocol):
    model: str

    def chat(self, messages: list[Message], tools: list[dict],
             force_tool: str | None = None) -> AssistantTurn:
        """One model turn. force_tool, when set, makes that tool the only allowed reply."""
        ...
