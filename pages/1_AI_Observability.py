"""
app_nl — pages/1_AI_Observability.py
=======================================
Streamlit auto-discovers files under pages/ next to app_nl.py as
additional navigation entries — no wiring needed in app_nl.py itself.

Reads from observability.py's local SQLite request-log store (see that
module's docstring for why this exists alongside Langfuse rather than
replacing it: Langfuse/Logfire remain the source of truth for full trace
detail — every span, every prompt, every attribute. This page is a fast
rollup over what pipeline.py already computes per request, with a link
out to the full trace for anything that needs deeper investigation.

WHAT'S HERE vs WHAT'S NOT (read before assuming a metric is missing by
accident):
  - First token time: NOT SHOWN. Every LLM call in this app is
    non-streaming (awaits the full response), so there's no first-token
    signal anywhere to plot. This isn't a page bug — it needs a change to
    pipeline.py's generation loop (switching to run_stream()) before it
    can exist at all. Shown as a static note, not a fabricated number.
  - Langfuse trace links: only render if LANGFUSE_PROJECT_ID is set in
    config AND a trace_id was actually captured for that row. See
    observability.build_langfuse_trace_url's docstring — the URL shape is
    best-effort, not verified against every Langfuse version.
  - Cost: labeled "estimated" everywhere it appears, because it is —
    MODEL_PRICING_PER_1K_TOKENS_USD in observability.py is a placeholder
    table, not synced to any provider's real pricing automatically.
"""
import streamlit as st
import pandas as pd

from observability import get_requests_df, build_langfuse_trace_url, configure_observability

configure_observability()  # idempotent — safe even if app_nl.py already called this in this process

st.set_page_config(page_title="AI Observability — app_nl", page_icon="📈", layout="wide")
st.title("📈 AI Observability")
st.caption(
    "Rollup over the local request log. For full trace detail — every span, every prompt, "
    "every attribute — use Langfuse/Logfire directly; this page is the fast summary view."
)

lookback_options = {"Last 100": 100, "Last 500": 500, "Last 1,000": 1000, "Last 5,000": 5000}
lookback_label = st.sidebar.selectbox("Lookback window", list(lookback_options.keys()), index=1)
df = get_requests_df(limit=lookback_options[lookback_label])

if df.empty:
    st.info("No requests logged yet — this fills in as the app is used.")
    st.stop()

df["timestamp_dt"] = pd.to_datetime(df["timestamp"], unit="s")
df = df.sort_values("timestamp_dt")

# Outcomes that represent the system correctly handling the request, even
# if it didn't produce SQL (a canned reply or a clarification ask is a
# SUCCESS, not a failure — only validation_failed/unhandled_exception/
# no_matching_table represent the system actually falling short).
_SUCCESS_OUTCOMES = {
    "validated", "cache_hit", "canned_response", "blocked_regex",
    "blocked_llm_guardrail", "clarification_needed", "ambiguous_entity",
}

total_requests = len(df)
success_count = df["outcome"].isin(_SUCCESS_OUTCOMES).sum()
success_rate = success_count / total_requests if total_requests else 0.0
cache_hit_rate = df["cache_hit"].astype(bool).mean() if total_requests else 0.0
total_cost = df["estimated_cost_usd"].sum(skipna=True)
avg_latency = df["elapsed_ms"].mean(skipna=True)
total_tokens = df["total_tokens"].sum(skipna=True)

# ── Top-line metrics ──────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Requests", f"{total_requests:,}")
c2.metric("Success rate", f"{success_rate:.0%}")
c3.metric("Cache hit ratio", f"{cache_hit_rate:.0%}")
c4.metric("Est. total cost", f"${total_cost:,.4f}" if pd.notna(total_cost) else "—")
c5.metric("Avg latency", f"{avg_latency:,.0f} ms" if pd.notna(avg_latency) else "—")

st.caption(
    f"Total tokens across window: {total_tokens:,.0f}" if pd.notna(total_tokens) else "Total tokens: —"
)
st.info(
    "⏱️ **First token time isn't tracked yet.** Every LLM call in this app currently waits for "
    "the full response before returning — there's no first-token signal to record until at least "
    "the SQL-generation call switches to streaming mode."
)

st.divider()

# ── Model routing distribution ───────────────────────────────────────
left, right = st.columns(2)
with left:
    st.subheader("Model routing distribution")
    model_counts = df["model_used"].fillna("(none — no LLM call)").value_counts()
    st.bar_chart(model_counts)

with right:
    st.subheader("Complexity distribution")
    complexity_counts = df["complexity"].fillna("(n/a)").value_counts()
    st.bar_chart(complexity_counts)

# ── Latency and cost over time ───────────────────────────────────────
left, right = st.columns(2)
with left:
    st.subheader("Latency over time (ms)")
    latency_series = df.set_index("timestamp_dt")["elapsed_ms"]
    st.line_chart(latency_series)

with right:
    st.subheader("Estimated cost over time (USD)")
    cost_series = df.set_index("timestamp_dt")["estimated_cost_usd"].fillna(0).cumsum()
    st.line_chart(cost_series)
    st.caption("Cumulative — see module note above on why this is an estimate, not billing-accurate.")

# ── Success / failure breakdown ──────────────────────────────────────
st.subheader("Outcome breakdown")
outcome_counts = df["outcome"].value_counts()
st.bar_chart(outcome_counts)

failure_outcomes = {"validation_failed", "no_matching_table", "unhandled_exception"}
failures = df[df["outcome"].isin(failure_outcomes)]
if not failures.empty:
    with st.expander(f"⚠️ {len(failures)} failed request(s) in this window", expanded=False):
        st.dataframe(
            failures[["timestamp_dt", "question", "rewritten_question", "outcome", "model_used"]],
            use_container_width=True,
        )

# ── Execution outcomes — closes the gap between "SQL generation succeeded"
# and "the query actually ran." Only populated for rows where the user
# clicked "Run this query" in app_nl.py; validated-but-never-run SQL
# correctly shows as NaN here, not as a failure.
executed = df[df["execution_ran"] == 1] if "execution_ran" in df.columns else df.iloc[0:0]
if not executed.empty:
    st.subheader("Execution outcomes (queries actually run against the database)")
    exec_success_rate = executed["execution_success"].astype(bool).mean()
    avg_exec_ms = executed["execution_ms"].mean(skipna=True)
    repaired_count = (executed["repair_attempts"].fillna(0) > 0).sum()
    e1, e2, e3 = st.columns(3)
    e1.metric("Execution success rate", f"{exec_success_rate:.0%}")
    e2.metric("Avg execution time", f"{avg_exec_ms:,.0f} ms" if pd.notna(avg_exec_ms) else "—")
    e3.metric("Auto-repaired queries", f"{repaired_count}")

    exec_failures = executed[executed["execution_success"] == 0]
    if not exec_failures.empty:
        with st.expander(
            f"⚠️ {len(exec_failures)} query/queries passed validation but failed to actually run",
            expanded=False,
        ):
            st.dataframe(
                exec_failures[["timestamp_dt", "question", "model_used"]],
                use_container_width=True,
            )
else:
    st.caption("No queries have been run against the database yet in this window — "
               "generation and validation outcomes are shown above regardless.")

st.divider()

# ── Query history table, with Langfuse trace links where available ───
st.subheader("Query history")
display_df = df[[
    "timestamp_dt", "question", "outcome", "model_used", "complexity",
    "cache_hit", "attempts", "total_tokens", "estimated_cost_usd", "elapsed_ms",
]].rename(columns={
    "timestamp_dt": "Time", "question": "Question", "outcome": "Outcome",
    "model_used": "Model", "complexity": "Complexity", "cache_hit": "Cache hit",
    "attempts": "Attempts", "total_tokens": "Tokens",
    "estimated_cost_usd": "Est. cost (USD)", "elapsed_ms": "Latency (ms)",
})

trace_links = df["trace_id"].apply(build_langfuse_trace_url)
if trace_links.notna().any():
    display_df["Langfuse trace"] = trace_links
    st.dataframe(
        display_df.sort_values("Time", ascending=False),
        use_container_width=True,
        column_config={"Langfuse trace": st.column_config.LinkColumn("Langfuse trace", display_text="Open ↗")},
    )
else:
    st.dataframe(display_df.sort_values("Time", ascending=False), use_container_width=True)
    st.caption(
        "No Langfuse trace links available — set LANGFUSE_PROJECT_ID in config to enable them "
        "(and verify the URL shape in observability.build_langfuse_trace_url against your Langfuse "
        "instance/version first; it's best-effort, not confirmed against every version)."
    )