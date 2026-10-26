"""
app_nl.py — main entrypoint
=============================
Run with:  streamlit run app_nl.py

Lean Phase-1 multi-table-ready NL2SQL assistant. See registry.py's module
docstring for how to add a new table, and the architecture review notes
throughout this codebase for what's deliberately deferred (Query Planner,
embedding retrieval, dedicated Table Selector, vector DB) and what triggers
adding each one.
"""
import asyncio
import time
import uuid
from datetime import datetime

import streamlit as st
import logfire

import config
import retrieval.registry as registry
import data.cache as cache_mod
from observability import configure_observability, log_execution, log_reasoning
from data.db import fetch_categorical_context, clear_cache as clear_db_cache, find_all_fuzzy_matches
from core.pipeline import run_pipeline
from core.models import Complexity
from execution.executor import execute_sql_with_recovery
from llm import build_sql_agent, build_sql_model_cascade, run_reasoning, humanize_dataframe
from retrieval.rule_retrieval import retrieve_rules, format_rules, format_patterns
from retrieval.registry import retrieve_patterns

configure_observability()  # must run before any pydantic-ai Agent is built

st.set_page_config(page_title="Sales Analytics Assistant (app_nl)", page_icon="📊", layout="wide")

# ── Auth gate (same stopgap pattern as your existing app) ────────────────
if config.APP_PASSWORD:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if not st.session_state.authenticated:
        st.title("🔒 Sign in")
        entered = st.text_input("Password", type="password")
        if st.button("Sign in"):
            if entered == config.APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()

st.title("📊 Sales Analytics Assistant — app_nl")
st.caption("Lean Phase-1 architecture: deterministic complexity/model routing, "
           "registry-driven rules & patterns, no embedding infra yet.")

if "current" not in st.session_state:
    st.session_state.current = None
if "current_df" not in st.session_state:
    st.session_state.current_df = None
if "current_reasoning" not in st.session_state:
    st.session_state.current_reasoning = None
if "history" not in st.session_state:
    st.session_state.history = []
if "conv_memory" not in st.session_state:
    st.session_state.conv_memory = None
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

with st.sidebar:
    st.subheader("Settings")
    tables = registry.known_tables()
    st.caption(f"**Registered tables ({len(tables)}):** {', '.join(tables)}")

    override_choice = st.selectbox(
        "Manual model override (optional — normally routing is automatic by complexity)",
        ["Auto (routed by complexity)"] + [
            "google:gemini-2.5-flash",
            "groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b", "groq:qwen/qwen3.6-27b",
        ],
        index=0,
    )
    manual_override = None if override_choice.startswith("Auto") else override_choice

    if st.button("🔄 Refresh live values from DB"):
        clear_db_cache()
        st.success("Cleared — will refetch on next query.")

    if st.button("🧹 Forget conversation context"):
        st.session_state.conv_memory = None
        st.success("Follow-up memory cleared.")

    with st.expander("Registry preview", expanded=False):
        for t in tables:
            st.markdown(f"**{t}**")
            try:
                ctx = fetch_categorical_context(t)
                for col, vals in ctx.items():
                    st.caption(f"{col}: {', '.join(vals[:10])}{' …' if len(vals) > 10 else ''}")
            except Exception as e:
                st.caption(f"(could not fetch live values: {e})")

    st.divider()
    st.subheader(f"🕘 Query history ({len(st.session_state.history)})")
    if not st.session_state.history:
        st.caption("Nothing run yet this session.")
    else:
        if st.button("🗑️ Clear history"):
            st.session_state.history = []
            st.rerun()
        for i, entry in enumerate(reversed(st.session_state.history)):
            with st.expander(f"{entry['ts']} — {entry['question'][:40]}"):
                st.code(entry["sql"], language="sql")
                st.caption(f"{entry['rows']} row(s) returned")

question = st.text_input(
    "Ask a question about sales, items, orders, customers…",
    placeholder="e.g. total sales for Royapettah last week",
)

generate = st.button("Generate SQL", type="primary")

if generate:
    if not question.strip():
        st.warning("Type a question first.")
    else:
        rate_limit_message = cache_mod.check_rate_limit(
            st.session_state.session_id, config.PER_MINUTE_LIMIT, config.PER_HOUR_LIMIT, config.PER_DAY_LIMIT
        )
        if rate_limit_message:
            st.warning(f"You're sending requests faster than expected ({rate_limit_message}) — please slow down.")
            st.stop()

        with st.spinner("Thinking…"):
            with logfire.span("request_trace", session_id=st.session_state.session_id) as request_span:
                try:
                    rewrite, sql_result, validation, new_history, meta = asyncio.run(
                        run_pipeline(question, manual_model_override=manual_override,
                                     message_history=st.session_state.conv_memory,
                                     session_id=st.session_state.session_id)
                    )
                    request_span.set_attribute("request_id", meta.get("request_id", ""))
                except Exception as e:
                    # pipeline.run_pipeline already catches and logs everything
                    # internally with a full traceback — this is a true
                    # last-resort net for something going wrong even outside
                    # that (e.g. asyncio itself), so the Streamlit session
                    # never just dies with an unhandled exception.
                    logfire.exception("app_nl_uncaught_exception", session_id=st.session_state.session_id, error=str(e))
                    st.error(f"Something went wrong processing that question: {e}")
                    st.caption("This has been logged. Try again, or rephrase the question.")
                    st.stop()

        st.session_state.conv_memory = new_history
        st.session_state.current = {
            "question": question, "rewrite": rewrite, "sql_result": sql_result,
            "validation": validation, "meta": meta,
        }
        st.session_state.current_df = None
        st.session_state.current_reasoning = None  # clear stale analysis from a previous question

current = st.session_state.current
if current:
    rewrite = current["rewrite"]
    sql_result = current["sql_result"]
    validation = current["validation"]
    meta = current.get("meta", {})

    badge_bits = []
    if meta.get("source") == "cache":
        badge_bits.append("⚡ served from cache")
    if meta.get("model_used"):
        badge_bits.append(f"model: `{meta['model_used']}`")
    if meta.get("complexity"):
        badge_bits.append(f"complexity: **{meta['complexity']}**")
    if meta.get("candidate_tables"):
        badge_bits.append(f"table(s): {', '.join(meta['candidate_tables'])}")
    if meta.get("attempts"):
        badge_bits.append(f"{meta['attempts']} attempt(s)")
    if badge_bits:
        st.caption(" · ".join(badge_bits))

    if meta.get("rules_used") or meta.get("patterns_used"):
        with st.expander("Routing detail — rules & patterns retrieved", expanded=False):
            st.write("**Business rules retrieved:**", meta.get("rules_used") or "(none)")
            st.write("**SQL patterns retrieved:**", meta.get("patterns_used") or "(none)")

    if rewrite.rewritten_question and rewrite.rewritten_question.strip().lower() != current["question"].strip().lower():
        st.caption(f"Interpreted as: *{rewrite.rewritten_question}*")

    # ── Intent classification + which agent/tool ran — testing aid.
    # Shown for every outcome (not just a successful SQL result), since
    # verifying the classification is correct matters just as much when a
    # question got declined or asked for clarification. There is no real
    # Reasoning Engine yet (see core/pipeline.py's needs_reasoning note) —
    # the label says so plainly rather than implying reasoning happened.
    if meta.get("answer_type"):
        intent_label = meta["answer_type"]
        intent_emoji = {"DATA_QUERY": "📊", "ANALYTICAL_REASONING": "🧠", "HYBRID": "📊🧠"}.get(intent_label, "❔")
        if meta.get("needs_reasoning"):
            agent_label = "Query Generator + Reasoning _(reasoning engine not built yet — data only)_"
        else:
            agent_label = "Query Generator"
        st.markdown(f"{intent_emoji} **Intent:** `{intent_label}`  ·  **Running:** {agent_label}")

    if meta.get("source") == "canned":
        st.info(rewrite.clarification_needed)
    elif not rewrite.is_answerable or rewrite.clarification_needed:
        st.warning(rewrite.clarification_needed or "I can't confidently answer this — could you rephrase?")
    else:
        validated_sql, validation_error = validation
        if sql_result is None:
            # Was: st.error(f"Couldn't generate SQL: {validation_error}") — this
            # directly surfaced whatever raw text a failed model attempt produced
            # (validation_error here is built from `model failed to produce valid
            # output: {e}`, where e can be an arbitrary exception — including a
            # plain-English model refusal that failed JSON parsing, showing up as
            # a raw pydantic error dump). Friendly message first; raw detail is
            # still available, just not the primary thing thrown at the user.
            st.error(
                "I couldn't work out a confident answer to that just now — this is usually "
                "temporary. Try rephrasing the question, or try again in a moment."
            )
            with st.expander("Technical details (for debugging)", expanded=False):
                st.code(validation_error or "(no error detail captured)")
            if meta.get("request_id"):
                st.caption(f"Reference: request_id `{meta['request_id']}`")
        else:
            st.subheader("Generated SQL")
            st.code(sql_result.sql, language="sql")
            if sql_result.assumptions:
                st.info("**Assumptions made:**\n" + "\n".join(f"- {a}" for a in sql_result.assumptions))
            st.caption(sql_result.explanation)

            if validation_error:
                st.error(f"⚠️ Validator rejected this query: {validation_error}")
            else:
                st.success("✅ Passed validation — read-only single SELECT.")

                if st.button("▶️ Run this query"):
                    tables = meta.get("candidate_tables", [])
                    exec_model = meta.get("model_used")
                    try:
                        exec_ctx = {}
                        for t in tables:
                            exec_ctx.update(fetch_categorical_context(t))
                        fuzzy_matches_by_col = {}
                        for t in tables:
                            for col, matches in find_all_fuzzy_matches(rewrite.rewritten_question, t).items():
                                fuzzy_matches_by_col.setdefault(col, []).extend(matches)
                        rules = retrieve_rules(rewrite.rewritten_question, tables)
                        patterns = retrieve_patterns(rewrite.rewritten_question, tables)

                        df, final_sql, cost_warning, explain_rows, exec_ms, repair_log = asyncio.run(
                            execute_sql_with_recovery(
                                validated_sql, rewrite.rewritten_question, tables,
                                format_rules(rules), format_patterns(patterns),
                                exec_ctx, fuzzy_matches_by_col, exec_model, build_sql_agent,
                                request_id=meta.get("request_id"),
                            )
                        )
                        # Translate coded columns (e.g. MONTH(date) -> "January")
                        # into human-readable form once, here — both the
                        # displayed table below and the Reasoning Engine read
                        # this same humanized frame, so they can't disagree
                        # with each other about what a value means.
                        df = humanize_dataframe(df)
                        if repair_log:
                            st.info(f"⚙️ Auto-corrected after a database error ({len(repair_log)} repair attempt(s)).")
                        if cost_warning:
                            st.warning(f"⚠️ {cost_warning}")

                        log_execution(
                            request_id=meta.get("request_id"), execution_success=True,
                            execution_rows=len(df), execution_ms=exec_ms,
                            repair_attempts=len(repair_log), cost_warning=cost_warning,
                        )

                        # Reasoning Engine — runs here specifically because
                        # this is the first point actual data exists. Still
                        # gated behind the same "Run this query" click as
                        # everything else; this doesn't add a second
                        # execution path or bypass the safety gate, it just
                        # continues the same click's flow one step further.
                        reasoning_result = None
                        if meta.get("needs_reasoning"):
                            with st.spinner("Reasoning over the results…"):
                                reasoning_cascade = build_sql_model_cascade(Complexity.COMPLEX)
                                _reasoning_start = time.perf_counter()
                                try:
                                    reasoning_result, reasoning_run = asyncio.run(
                                        run_reasoning(
                                            reasoning_cascade[0], rewrite.rewritten_question, final_sql, df,
                                            assumptions=sql_result.assumptions if sql_result else None,
                                        )
                                    )
                                    reasoning_ms = round((time.perf_counter() - _reasoning_start) * 1000, 1)
                                    reasoning_tokens = None
                                    try:
                                        usage = reasoning_run.usage()
                                        reasoning_tokens = getattr(usage, "total_tokens", None) or (
                                            (getattr(usage, "request_tokens", None) or 0)
                                            + (getattr(usage, "response_tokens", None) or 0)
                                        ) or None
                                    except Exception:
                                        pass
                                    log_reasoning(
                                        request_id=meta.get("request_id"), reasoning_success=True,
                                        reasoning_tokens=reasoning_tokens,
                                        reasoning_confidence=reasoning_result.confidence,
                                        reasoning_ms=reasoning_ms,
                                    )
                                except Exception as e:
                                    logfire.exception(
                                        "reasoning_engine_failed", request_id=meta.get("request_id"), error=str(e)
                                    )
                                    log_reasoning(request_id=meta.get("request_id"), reasoning_success=False)
                                    st.warning(
                                        "I retrieved the data, but couldn't complete the deeper analysis this "
                                        "time — the data is shown below regardless."
                                    )
                                    reasoning_result = None

                        st.session_state.current_df = df
                        st.session_state.current_reasoning = reasoning_result
                        st.session_state.current_df_label = current["question"]
                        st.session_state.history.append({
                            "question": current["question"], "sql": final_sql, "df": df,
                            "rows": len(df), "ts": datetime.now().strftime("%H:%M:%S"),
                        })
                        if len(st.session_state.history) > config.MAX_HISTORY_ITEMS:
                            st.session_state.history = st.session_state.history[-config.MAX_HISTORY_ITEMS:]
                        st.rerun()
                    except Exception as e:
                        logfire.exception("query_execution_uncaught", request_id=meta.get("request_id"), error=str(e))
                        log_execution(request_id=meta.get("request_id"), execution_success=False)
                        st.error(f"Couldn't run that just now, even after an automatic retry: {e}")
                        st.session_state.current_df = None

if st.session_state.current_df is not None:
    st.subheader(f"Result — {st.session_state.get('current_df_label', '')}")
    st.dataframe(st.session_state.current_df, use_container_width=True)
    st.caption(f"{len(st.session_state.current_df)} row(s) returned.")

    reasoning_result = st.session_state.get("current_reasoning")
    if reasoning_result:
        st.divider()
        st.subheader("🧠 Analysis")
        st.write(reasoning_result.insight)
        if reasoning_result.key_findings:
            st.markdown("**Key findings:**")
            for finding in reasoning_result.key_findings:
                st.markdown(f"- {finding}")
        if reasoning_result.caveats:
            with st.expander("Caveats / limitations", expanded=False):
                for caveat in reasoning_result.caveats:
                    st.markdown(f"- {caveat}")
        st.caption(f"Analysis confidence: {reasoning_result.confidence:.0%}")