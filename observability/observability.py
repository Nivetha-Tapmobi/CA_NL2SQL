"""
app_nl — observability.py
============================
Single place that wires up tracing. Two things happen here:

1. logfire.configure(...) + logfire.instrument_pydantic_ai() — this is what
   makes EVERY pydantic-ai Agent.run() call anywhere in the app (intake,
   SQL generation, repair) automatically emit a span with gen_ai.* OTEL
   attributes (model, prompt tokens, completion tokens, latency) with ZERO
   extra code at each call site. This is the "all LLM calls traced" half.

2. A second OTEL span exporter pointed at Langfuse's OTEL ingestion
   endpoint, attached to the SAME tracer provider Logfire configured. Since
   pydantic-ai's instrumentation already emits standard gen_ai.* spans,
   Langfuse picks up model/token/cost data natively — no separate
   Langfuse SDK calls needed anywhere else in the codebase.

Call configure_observability() ONCE, at app startup (app_nl.py does this
before anything else runs). Every logfire.span(...) call elsewhere in the
codebase (pipeline.py, executor.py, agents.py) becomes a proper child span
under whatever request-level span wraps it, so the whole app_nl.py trace —
guardrail -> cache -> intake -> routing -> retrieval -> SQL generation ->
validation -> execution -> response — is one connected trace from user
query to final output, viewable in both Logfire and Langfuse.

ADDED IN THIS PASS — local request-log store for the "AI Observability"
Streamlit page. Langfuse/Logfire remain the source of truth for full trace
detail (every span, every attribute, every LLM call's raw prompt/response);
this is deliberately NOT trying to replace that. What it adds is a fast,
dependency-free rollup: query history, token/cost estimates, cache hit
ratio, model routing distribution, latency, and success/failure rate,
without the Streamlit page making a network round-trip to Langfuse's API
on every render. pipeline.py calls log_request(summary) exactly once per
request (in a `finally`, so it fires regardless of which branch returned)
with whatever fields it has by that point.

TWO HONEST LIMITS, not silently worked around:
  - "First token time" is NOT captured anywhere here. Every Agent.run()
    call in this codebase is non-streaming (awaits the full response) —
    there is no first-token signal to record. Getting it means switching
    at least the SQL-generation call to run_stream(), which is a real
    change to pipeline.py's generation loop, not an observability-only
    addition. The Streamlit page shows this column as "not instrumented."
  - build_langfuse_trace_url()'s URL shape is BEST-EFFORT. The OTEL
    trace_id captured by get_current_trace_id() is exact and verifiable
    (32-hex-char, per the OTEL spec) — what's NOT verified is Langfuse's
    current UI route for deep-linking to a trace by that ID, which can
    depend on your Langfuse version and requires a project ID this file
    has no source for unless you set LANGFUSE_PROJECT_ID yourself. If the
    constructed link 404s, that's this function's URL template being
    wrong, not the trace_id being wrong — check Langfuse's docs for your
    version/instance and fix the template in one place.
"""
import os
import sqlite3
import time
from typing import Optional

import logfire
from opentelemetry import trace as otel_trace

from config import LOGFIRE_TOKEN, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

try:
    from config import LANGFUSE_PROJECT_ID
except ImportError:
    LANGFUSE_PROJECT_ID = None  # not every config.py will have this yet — see build_langfuse_trace_url

_configured = False


def configure_observability() -> None:
    global _configured
    if _configured:
        return

    logfire.configure(
        token=LOGFIRE_TOKEN,
        send_to_logfire=bool(LOGFIRE_TOKEN),  # safe no-op locally if no token set — logs still work, just not shipped
        service_name="app-nl-sales-analytics",
    )
    # This one line is what captures every LLM call (intake + SQL gen +
    # repair, across every model in every cascade) without instrumenting
    # each Agent individually.
    logfire.instrument_pydantic_ai()

    # Trace SQLAlchemy execution too, so the "Execution" stage of the
    # pipeline (the actual query hitting MySQL) shows up in the same trace
    # as the LLM calls that produced it — not required for the LLM-tracing
    # ask specifically, but it's what makes "whole app, query to output"
    # actually true rather than stopping at SQL generation.
    try:
        logfire.instrument_sqlalchemy()
    except Exception:
        pass  # optional — don't let instrumentation failures break the app

    if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
        import base64
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        auth = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{LANGFUSE_HOST}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        otel_trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(exporter))
        logfire.info("langfuse_tracing_enabled", host=LANGFUSE_HOST)
    else:
        logfire.info("langfuse_tracing_disabled_no_keys")

    _configured = True


# ── Metric counters — same instinct as your existing app: cheap counters
# exposed via Logfire (and Langfuse, since it's the same OTEL pipeline). ──
metric_total_requests = logfire.metric_counter("total_requests")
metric_successful_requests = logfire.metric_counter("successful_requests")
metric_failed_requests = logfire.metric_counter("failed_requests")
metric_cache_hits = logfire.metric_counter("cache_hits")
metric_cache_misses = logfire.metric_counter("cache_misses")
metric_validation_failures = logfire.metric_counter("validation_failures")
metric_guardrail_blocks = logfire.metric_counter("guardrail_blocks")
metric_model_escalations = logfire.metric_counter("model_escalations")


# ═══════════════════════════════════════════════════════════════════════
# LOCAL REQUEST-LOG STORE — backs the "AI Observability" Streamlit page.
# See module docstring for what this is and isn't.
# ═══════════════════════════════════════════════════════════════════════

OBS_DB_PATH = os.environ.get("APP_NL_OBSERVABILITY_DB", "app_nl_observability.sqlite3")

# PLACEHOLDER RATES, per 1K tokens, USD — approximate and NOT kept in sync
# with any provider's actual current pricing automatically. Good enough for
# spotting relative cost trends between models/routing decisions (which is
# the actual point of this page — "did routing save money" is a relative
# question); do not treat this as billing-accurate. Update these against
# your providers' current pricing pages before trusting absolute numbers.
MODEL_PRICING_PER_1K_TOKENS_USD = {
    "google:gemini-2.5-flash": 0.0003,
    "google:gemini-2.0-flash": 0.0002,
    "groq:openai/gpt-oss-120b": 0.0002,
    "groq:openai/gpt-oss-20b": 0.0001,
    "groq:qwen/qwen3.6-27b": 0.0001,
}

_obs_conn: Optional[sqlite3.Connection] = None


def _obs_connection() -> sqlite3.Connection:
    global _obs_conn
    if _obs_conn is None:
        conn = sqlite3.connect(OBS_DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS request_log (
                request_id TEXT PRIMARY KEY,
                timestamp REAL,
                session_id TEXT,
                question TEXT,
                rewritten_question TEXT,
                outcome TEXT,
                model_used TEXT,
                complexity TEXT,
                candidate_tables TEXT,
                cache_hit INTEGER,
                attempts INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL,
                elapsed_ms REAL,
                trace_id TEXT
            )"""
        )
        # Execution-stage columns, added after the fact — a query can pass
        # SQL generation/validation and still fail when actually run against
        # MySQL (real DB error, cost guardrail, a repair attempt), and that
        # outcome lives in app_nl.py's "Run this query" handler, entirely
        # outside run_pipeline(). ALTER TABLE ADD COLUMN, one at a time,
        # tolerant of "duplicate column" so this self-migrates an existing
        # database file from before these columns existed, instead of
        # requiring anyone to delete/recreate it.
        for col_def in (
            "execution_ran INTEGER", "execution_success INTEGER", "execution_rows INTEGER",
            "execution_ms REAL", "repair_attempts INTEGER", "cost_warning TEXT",
            "reasoning_ran INTEGER", "reasoning_success INTEGER", "reasoning_tokens INTEGER",
            "reasoning_confidence REAL", "reasoning_ms REAL",
        ):
            try:
                conn.execute(f"ALTER TABLE request_log ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists — fine, this runs on every connection open
        conn.commit()
        _obs_conn = conn
    return _obs_conn


def _estimate_cost(model_used: Optional[str], total_tokens: Optional[int]) -> Optional[float]:
    if not model_used or not total_tokens:
        return None
    rate = MODEL_PRICING_PER_1K_TOKENS_USD.get(model_used)
    if rate is None:
        return None  # unrecognized model — show tokens without a fabricated cost rather than guessing a rate
    return round((total_tokens / 1000) * rate, 6)


def log_request(summary: dict) -> None:
    """Best-effort, single row per request. Called from pipeline.py's
    `finally` block, so it fires exactly once regardless of which branch
    returned. NEVER raises into the caller — a broken observability write
    must not break a real user-facing request; failures are logged to
    Logfire (ironic but intentional: the primary tracing path doesn't
    depend on this local store at all) and otherwise swallowed."""
    try:
        conn = _obs_connection()
        estimated_cost = _estimate_cost(summary.get("model_used"), summary.get("total_tokens"))
        conn.execute(
            """INSERT OR REPLACE INTO request_log
               (request_id, timestamp, session_id, question, rewritten_question, outcome,
                model_used, complexity, candidate_tables, cache_hit, attempts, total_tokens,
                estimated_cost_usd, elapsed_ms, trace_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary.get("request_id"), summary.get("timestamp", time.time()),
                summary.get("session_id", ""), summary.get("question", ""),
                summary.get("rewritten_question", ""), summary.get("outcome", "unknown"),
                summary.get("model_used"), summary.get("complexity"),
                summary.get("candidate_tables", ""), int(bool(summary.get("cache_hit"))),
                summary.get("attempts", 0), summary.get("total_tokens"),
                estimated_cost, summary.get("elapsed_ms"), summary.get("trace_id"),
            ),
        )
        conn.commit()
    except Exception as e:
        logfire.warn("observability_log_write_failed", error=str(e))


def log_execution(
    request_id: Optional[str],
    execution_success: bool,
    execution_rows: Optional[int] = None,
    execution_ms: Optional[float] = None,
    repair_attempts: Optional[int] = None,
    cost_warning: Optional[str] = None,
) -> None:
    """Call from app_nl.py's 'Run this query' handler, both on success and
    on failure — this is what closes the gap between 'SQL generation
    succeeded' and 'the query actually ran'. Updates the existing row by
    request_id (an UPDATE, not an INSERT) — if that row doesn't exist for
    some reason (e.g. this request_id was never logged by the generation
    stage), this silently affects 0 rows rather than crashing; execution
    outcome with no matching generation row isn't worth failing over."""
    if not request_id:
        return
    try:
        conn = _obs_connection()
        conn.execute(
            """UPDATE request_log SET execution_ran = 1, execution_success = ?,
               execution_rows = ?, execution_ms = ?, repair_attempts = ?, cost_warning = ?
               WHERE request_id = ?""",
            (int(execution_success), execution_rows, execution_ms, repair_attempts,
             cost_warning, request_id),
        )
        conn.commit()
    except Exception as e:
        logfire.warn("observability_log_execution_failed", error=str(e))


def log_reasoning(
    request_id: Optional[str],
    reasoning_success: bool,
    reasoning_tokens: Optional[int] = None,
    reasoning_confidence: Optional[float] = None,
    reasoning_ms: Optional[float] = None,
) -> None:
    """Same UPDATE-by-request_id pattern as log_execution — called from
    app_nl.py after the Reasoning Engine runs (success or failure), so the
    dashboard can show reasoning outcomes joined onto the same request row
    as generation and execution, not as a separate disconnected event."""
    if not request_id:
        return
    try:
        conn = _obs_connection()
        conn.execute(
            """UPDATE request_log SET reasoning_ran = 1, reasoning_success = ?,
               reasoning_tokens = ?, reasoning_confidence = ?, reasoning_ms = ?
               WHERE request_id = ?""",
            (int(reasoning_success), reasoning_tokens, reasoning_confidence, reasoning_ms, request_id),
        )
        conn.commit()
    except Exception as e:
        logfire.warn("observability_log_reasoning_failed", error=str(e))


def get_requests_df(limit: int = 5000):
    """Returns a pandas DataFrame of the most recent `limit` requests,
    newest first — the Streamlit observability page's single data source.
    Imports pandas locally so importing observability.py elsewhere in the
    app (agents.py, pipeline.py) doesn't require pandas as a hard dep just
    for tracing setup."""
    import pandas as pd
    conn = _obs_connection()
    return pd.read_sql_query(
        "SELECT * FROM request_log ORDER BY timestamp DESC LIMIT ?", conn, params=(limit,)
    )


def get_current_trace_id() -> Optional[str]:
    """Current OTEL trace ID as a 32-char hex string, or None if there's no
    active span context. Exact and spec-compliant — the uncertainty in this
    file is entirely in build_langfuse_trace_url's URL shape, not here."""
    span = otel_trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if not ctx or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def build_langfuse_trace_url(trace_id: Optional[str]) -> Optional[str]:
    """BEST-EFFORT — see module docstring. Returns None (not a guessed
    link) if LANGFUSE_PROJECT_ID isn't configured, since a project-id-less
    URL is more likely to be wrong than useful. Verify this template
    against your actual Langfuse instance before relying on it."""
    if not trace_id or not LANGFUSE_PROJECT_ID:
        return None
    return f"{LANGFUSE_HOST}/project/{LANGFUSE_PROJECT_ID}/traces/{trace_id}"