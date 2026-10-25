"""Gemini through the google-genai SDK, with automatic function calling off: our loop runs
the tools, so it can log, limit and validate every call."""

from __future__ import annotations

import os
import re
import time

from google import genai
from google.genai import errors, types

from triage.schema import AssistantTurn, Message, ToolCall, Usage

DEFAULT_MODEL = os.environ.get("TRIAGE_MODEL") or "gemini-3.5-flash-lite"  # empty var = default


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines) so the key never has to be in a shell profile."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")  # .env wins over stale env


class GeminiClient:
    def __init__(self, model: str = DEFAULT_MODEL, temperature: float = 0.0,
                 max_retries: int = 6):
        load_dotenv()
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (environment or .env)")
        self.client = genai.Client(api_key=key)
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self._n = 0

    # ---- conversion ----------------------------------------------------------------------
    @staticmethod
    def _contents(messages: list[Message]) -> tuple[str | None, list[types.Content]]:
        system = None
        out: list[types.Content] = []
        for m in messages:
            if m.role == "system":
                system = m.content
            elif m.role == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=m.content)]))
            elif m.role == "assistant":
                if m.provider_parts:
                    parts = [types.Part.model_validate(p) for p in m.provider_parts]
                else:
                    parts = [types.Part(text=m.content)] if m.content else []
                    parts += [types.Part(function_call=types.FunctionCall(name=c.name,
                                                                          args=c.args))
                              for c in m.tool_calls]
                out.append(types.Content(role="model", parts=parts))
            elif m.role == "tool":
                part = types.Part(function_response=types.FunctionResponse(
                    name=m.name, response={"output": m.content}))
                # parallel calls: all responses for one model turn go in one user Content
                if out and out[-1].role == "user" and out[-1].parts \
                        and out[-1].parts[0].function_response is not None:
                    out[-1].parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
        return system, out

    def chat(self, messages: list[Message], tools: list[dict],
             force_tool: str | None = None) -> AssistantTurn:
        system, contents = self._contents(messages)
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                           parameters_json_schema=t["parameters"])
                 for t in tools]
        mode = types.FunctionCallingConfigMode.ANY if force_tool else \
            types.FunctionCallingConfigMode.AUTO
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode=mode, allowed_function_names=[force_tool] if force_tool else None)),
        )
        resp, ms = self._call(contents, config)
        cand = resp.candidates[0] if resp.candidates else None
        parts = list(cand.content.parts) if cand and cand.content and cand.content.parts else []
        texts, calls = [], []
        for p in parts:
            if p.function_call is not None:
                self._n += 1
                calls.append(ToolCall(id=p.function_call.id or f"c{self._n}",
                                      name=p.function_call.name,
                                      args=dict(p.function_call.args or {})))
            elif p.text and not p.thought:
                texts.append(p.text)
        um = resp.usage_metadata
        usage = Usage(
            input_tokens=(um.prompt_token_count or 0) if um else 0,
            output_tokens=((um.candidates_token_count or 0) + (um.thoughts_token_count or 0))
            if um else 0,
            latency_ms=ms)
        return AssistantTurn(
            text="\n".join(texts) or None, tool_calls=calls, usage=usage,
            provider_parts=[p.model_dump(mode="json", exclude_none=True) for p in parts])

    def _call(self, contents, config):
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config)
                return resp, int((time.perf_counter() - start) * 1000)
            except errors.APIError as e:
                retryable = e.code in (429, 500, 502, 503, 504)
                if not retryable or attempt == self.max_retries:
                    raise
                m = re.search(r"retry in ([\d.]+)s|retryDelay['\"]?: ?['\"](\d+)s", str(e))
                delay = float(m.group(1) or m.group(2)) if m else min(60, 2 ** (attempt + 1))
                time.sleep(delay + 1)
        raise RuntimeError("unreachable")
