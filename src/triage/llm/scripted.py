"""ScriptedClient: returns pre-written turns, for unit tests of the loop."""

from __future__ import annotations

from collections.abc import Callable

from triage.schema import AssistantTurn, Message, ToolCall, Usage


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"s-{name}", name=name, args=args)


def turn(*calls: ToolCall, text: str | None = None, tokens: int = 100) -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=list(calls),
                         usage=Usage(input_tokens=tokens, output_tokens=20, latency_ms=1))


class ScriptedClient:
    model = "scripted"

    def __init__(self, turns: list[AssistantTurn | Callable[[list[Message]], AssistantTurn]]):
        self.turns = list(turns)
        self.requests: list[tuple[list[Message], list[dict], str | None]] = []

    def chat(self, messages: list[Message], tools: list[dict],
             force_tool: str | None = None) -> AssistantTurn:
        self.requests.append((list(messages), tools, force_tool))
        if not self.turns:
            return turn(text="(script exhausted)")
        nxt = self.turns.pop(0)
        return nxt(messages) if callable(nxt) else nxt
