"""
app_nl — llm/__init__.py
==========================
Everything about talking to and routing between LLMs, grouped: the
pydantic-ai Agent builders (agents.py), the pre-LLM guardrail layer
(guardrail.py), deterministic complexity classification (complexity.py),
deterministic model-tier routing (model_router.py), and the Reasoning
Engine (reasoning.py) — added to interpret already-retrieved data, kept in
its own file since it's a distinct concern from generating SQL.

Re-exports the names other modules actually import, so call sites can do
`from llm import build_intake_agent, classify_complexity, ...` instead of
reaching into each submodule individually.
"""
from .agents import build_intake_agent, build_sql_agent, run_with_model_cascade
from .guardrail import looks_like_injection, match_canned_response
from .complexity import classify_complexity
from .model_router import build_sql_model_cascade
from .reasoning import build_reasoning_agent, run_reasoning, humanize_dataframe

__all__ = [
    "build_intake_agent", "build_sql_agent", "run_with_model_cascade",
    "looks_like_injection", "match_canned_response",
    "classify_complexity",
    "build_sql_model_cascade",
    "build_reasoning_agent", "run_reasoning", "humanize_dataframe",
]