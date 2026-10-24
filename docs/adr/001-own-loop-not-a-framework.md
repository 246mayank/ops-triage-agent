# ADR 001: Own agent loop instead of an agent framework

**Context.** The agent needs hard budgets (turns, tool calls, tokens, time), loop detection,
a grounding validator with exactly one repair attempt, and a trace of every step. These are
the parts reviewers and on-call engineers ask about.

**Decision.** Write the loop directly (`src/triage/agent.py`, about 150 lines) against a
small `LlmClient` interface, with the model's automatic function calling turned off.

**Alternatives.** LangChain / LangGraph, or the SDK's automatic function calling. Both hide
when tools run and make budget and validation hooks indirect.

**Consequences.** Every line is explainable and testable with a scripted client. Cost: more
code to own, and no ecosystem of ready-made integrations. With a team and many more tools,
revisit.
