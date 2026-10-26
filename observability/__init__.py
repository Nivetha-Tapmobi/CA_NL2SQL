"""
app_nl — observability/__init__.py
=====================================
Re-exports everything from observability.py so every existing
`from observability import X` call site across the codebase (pipeline.py,
app_nl.py, pages/1_AI_Observability.py) keeps working with ZERO edits —
the package name matches the old flat module's name, which is what makes
this re-export free instead of requiring a call-site change like every
other moved module needed.
"""
from .observability import (
    configure_observability,
    log_request,
    log_execution,
    log_reasoning,
    get_requests_df,
    get_current_trace_id,
    build_langfuse_trace_url,
    metric_total_requests,
    metric_successful_requests,
    metric_failed_requests,
    metric_cache_hits,
    metric_cache_misses,
    metric_validation_failures,
    metric_guardrail_blocks,
    metric_model_escalations,
)

__all__ = [
    "configure_observability", "log_request", "log_execution", "log_reasoning",
    "get_requests_df", "get_current_trace_id", "build_langfuse_trace_url",
    "metric_total_requests", "metric_successful_requests", "metric_failed_requests",
    "metric_cache_hits", "metric_cache_misses", "metric_validation_failures",
    "metric_guardrail_blocks", "metric_model_escalations",
]
