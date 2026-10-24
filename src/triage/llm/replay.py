"""Record and replay. A cassette maps a hash of the request to the model's response, so
tests and CI never call a live model, and a re-score never spends quota."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from triage.schema import AssistantTurn, Message


def request_key(model: str, messages: list[Message], tools: list[dict],
                force_tool: str | None, salt: str = "") -> str:
    body = {
        "model": model, "salt": salt, "force_tool": force_tool,
        "tools": tools,
        # provider_parts can hold opaque signatures; the visible content defines the request
        "messages": [m.model_dump(mode="json", exclude={"provider_parts"}) for m in messages],
    }
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


class Cassette:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str) -> AssistantTurn | None:
        d = self.data.get(key)
        return AssistantTurn.model_validate(d) if d else None

    def put(self, key: str, turn: AssistantTurn) -> None:
        self.data[key] = turn.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True),
                             encoding="utf-8", newline="\n")


class RecordingClient:
    """Wraps a live client. Serves a recorded response when one exists, otherwise calls the
    live model and records the answer. `salt` separates repeated runs of the same incident."""

    def __init__(self, inner, cassette: Path, salt: str = ""):
        self.inner = inner
        self.model = inner.model
        self.cassette = Cassette(cassette)
        self.salt = salt
        self.live_calls = 0

    def chat(self, messages, tools, force_tool=None) -> AssistantTurn:
        key = request_key(self.model, messages, tools, force_tool, self.salt)
        hit = self.cassette.get(key)
        if hit is not None:
            return hit
        turn = self.inner.chat(messages, tools, force_tool)
        self.live_calls += 1
        self.cassette.put(key, turn)
        return turn


class ReplayClient:
    """Serves recorded responses only. A request with no recording is an error."""

    def __init__(self, cassette: Path, model: str, salt: str = ""):
        self.cassette = Cassette(cassette)
        self.model = model
        self.salt = salt

    def chat(self, messages, tools, force_tool=None) -> AssistantTurn:
        key = request_key(self.model, messages, tools, force_tool, self.salt)
        hit = self.cassette.get(key)
        if hit is None:
            raise KeyError(f"no recording for request {key} in {self.cassette.path}")
        return hit
