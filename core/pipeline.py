"""
app_nl — pipeline.py
=======================
Orchestrator. This is the lean Phase-1 pipeline from the architecture
review:

  canned response (0 cost)
  -> regex guard (0 cost)
  -> cache (0 cost on hit)
  -> intake: guardrail + rewrite + candidate_tables + intent   [1 LLM call, cheap cascade]
  -> complexity classifier (deterministic, 0 LLM calls)
  -> model router (deterministic, 0 LLM calls)
  -> entity disambiguation (dict/live-value lookup, 0 LLM calls)
  -> rule retrieval (dict lookup, 0 LLM calls)
  -> pattern retrieval (keyword match, 0 LLM calls)
  -> SQL generation (cascade + fixer loop, on the routed model tier)
  -> SQL validation (unchanged AST checks)

No Query Planner, no embedding retrieval, no dedicated Table Selector —
all explicitly deferred per the review, with the triggers for adding them
documented in registry.py's module docstring.

PROMPT-NARROWING (added in this pass): three things previously undermined
the column-narrowing built into registry.py/agents.py, all fixed here:
  1. build_sql_agent was never called with `question=` — meaning it always
     took the full-schema fallback branch regardless of what registry.py
     could do. Now passed through, with rewrite.rewritten_question.
  2. `per_table_ctx` (the categorical values sent to the LLM) included
     every categorical column for the table unconditionally. Now filtered
     to columns the question actually seems to need — see
     _relevant_columns_by_table below.
  3. fuzzy_matches_by_col was passed to the LLM exactly as returned by
     find_all_fuzzy_matches, including weak/noisy matches (e.g. a 56%
     item_name hit on a query that never mentions an item). Scanning every
     fuzzy column is still correct (it's the only way to catch something
     like "Ritchie Street" resolving to restaurant_name with no literal
     column-name overlap) — but a weak hit on a column nothing else about
     the question touches is noise, not signal, and is now dropped before
     it reaches the prompt. See _filter_fuzzy_noise.

OBSERVABILITY — the whole function body runs inside ONE top-level Logfire
span ("nl2sql_pipeline"), with a child span per stage. Every LLM call
(intake cascade + SQL generation cascade) is additionally auto-instrumented
by logfire.instrument_pydantic_ai() (configured once in observability.py),
which is what makes those specific spans carry gen_ai.* attributes and flow
through to Langfuse. A single unhandled exception anywhere in the body is
caught at the bottom, logged with a full traceback via logfire.exception,
and turned into a graceful user-facing message instead of a raw Python
error string reaching the UI.
"""
import re
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import logfire

from config import MAX_SQL_ATTEMPTS_PER_MODEL, MAX_SQL_ATTEMPTS_TOTAL, INTAKE_MODEL_CASCADE
from core.models import Complexity, IntakeResult, RewriteResult, SQLResult
from llm.complexity import classify_complexity
from llm.model_router import build_sql_model_cascade
from retrieval.rule_retrieval import retrieve_rules, format_rules, format_patterns
from retrieval.registry import retrieve_patterns
import retrieval.registry as registry
from data.db import fetch_categorical_context, find_all_fuzzy_matches, find_ambiguous_matches
from llm import build_intake_agent, build_sql_agent, run_with_model_cascade
from execution.validator import validate_sql, ValidationError
from llm.guardrail import looks_like_injection, match_canned_response
import data.cache as cache_mod
from observability import (
    metric_total_requests, metric_successful_requests, metric_failed_requests,
    metric_cache_hits, metric_cache_misses, metric_validation_failures,
    metric_guardrail_blocks, metric_model_escalations,
    log_request as log_observability_row, get_current_trace_id,
)

# Fuzzy matches below this score are only kept if their column is otherwise
# relevant to the question (name/baseline match) — i.e. a weak hit on a
# column nothing else points to is dropped as noise. A strong hit (>= this
# cutoff) is kept regardless, since that's precisely the "Ritchie Street"
# case: the column wouldn't be flagged relevant by name matching at all,
# only by the fuzzy layer actually finding a confident real-value match.
FUZZY_NOISE_CUTOFF = 0.75


def _filter_fuzzy_noise(
    fuzzy_matches_by_col: Dict[str, List[Tuple[str, float]]],
    tables: List[str],
    question: str,
) -> Dict[str, List[Tuple[str, float]]]:
    """Drops weak fuzzy hits on columns the question doesn't otherwise
    reference. Scanning every fuzzy_match_column upstream (in
    find_all_fuzzy_matches) is still correct and necessary — this only
    trims what actually reaches the LLM afterward."""
    if not fuzzy_matches_by_col:
        return {}
    name_relevant = set()
    for t in tables:
        name_relevant |= registry.relevant_columns_for_question(t, question)

    filtered: Dict[str, List[Tuple[str, float]]] = {}
    for col, matches in fuzzy_matches_by_col.items():
        strong = [m for m in matches if m[1] >= FUZZY_NOISE_CUTOFF]
        if strong:
            filtered[col] = strong
        elif col in name_relevant:
            filtered[col] = matches  # keep weak matches only when the column is otherwise clearly relevant
        # else: dropped entirely — weak match, no other signal this column matters
    return filtered


def _relevant_columns_by_table(
    tables: List[str], question: str, fuzzy_matches_by_col: Dict[str, List[Tuple[str, float]]],
) -> Dict[str, set]:
    """Same union agents.py's build_sql_agent uses for schema narrowing
    (name/baseline match + genuine fuzzy hits) — computed once here so
    categorical-value narrowing (per_table_ctx below) stays consistent with
    what actually ends up in the schema block, rather than using a
    different, looser rule for one and not the other."""
    result = {}
    for t in tables:
        selected = registry.relevant_columns_for_question(t, question)
        if fuzzy_matches_by_col:
            selected |= (set(fuzzy_matches_by_col.keys()) & registry.known_columns(t))
        result[t] = selected
    return result


# Deterministic date-range integrity check — a backstop for an observed
# intake failure: "7th July to 18th July 2026" (a 12-day range) rewritten
# as "July 7 and July 18, 2026" (two isolated days). The prompt now says
# not to do this (see agents.py's JOB B), but that's an instruction, not a
# guarantee — this check doesn't trust it, and asks for clarification
# rather than silently running SQL against whichever interpretation the
# rewrite happened to produce.
_MONTH_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
             r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE_TOKEN = rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_RE}(?:\s+\d{{4}})?"
_DATE_RANGE_RE = re.compile(rf"({_DATE_TOKEN})\s*(?:to|through|till|until)\s*({_DATE_TOKEN})", re.IGNORECASE)


def _find_date_range_phrase(text: str) -> Optional[str]:
    m = _DATE_RANGE_RE.search(text)
    return m.group(0) if m else None


def _extract_total_tokens(run_result, request_id: str, stage: str) -> int:
    """pydantic-ai's usage().total_tokens can legitimately come back as None
    even on a successful call — some providers only report request_tokens/
    response_tokens separately and don't populate a combined total. Treating
    that the same as 'capture failed' is why the observability dashboard
    was showing null tokens for real, successful calls. Falls back to
    summing the parts; only returns 0 (and logs, rather than staying
    silent) if usage() itself is unavailable or malformed."""
    try:
        usage = run_result.usage()
        if not usage:
            return 0
        total = getattr(usage, "total_tokens", None)
        if total:
            return total
        request_tokens = getattr(usage, "request_tokens", None) or 0
        response_tokens = getattr(usage, "response_tokens", None) or 0
        if request_tokens or response_tokens:
            return request_tokens + response_tokens
        # usage object exists but has nothing usable on it — worth knowing
        # about, not worth swallowing silently as if it were 0 real tokens.
        logfire.warn(
            "token_usage_unavailable", request_id=request_id, stage=stage,
            usage_repr=repr(usage),
        )
        return 0
    except Exception as e:
        logfire.warn("token_usage_extraction_failed", request_id=request_id, stage=stage, error=str(e))
        return 0


def _blocked_result(message: str) -> RewriteResult:
    return RewriteResult(rewritten_question="", is_answerable=False, clarification_needed=message)


async def run_pipeline(question: str, manual_model_override: Optional[str] = None,
                        message_history=None, session_id: Optional[str] = None):
    """Returns (rewrite, sql_result, (validated_sql, validation_error), new_history, meta).

    meta includes: source, model_used, complexity, candidate_tables, attempts,
    rules_used, patterns_used, request_id — enough for the UI to show
    routing decisions transparently.
    """
    request_id = str(uuid.uuid4())
    metric_total_requests.add(1, {"request_id": request_id})

    # Observability summary — one row written to the local request-log
    # store (see observability.py) regardless of which branch below
    # returns, via the `finally` at the bottom of this function. Updated
    # incrementally alongside the existing pipeline_span.set_attribute(...)
    # calls rather than requiring every return statement to change.
    _obs_start = time.perf_counter()
    _obs_total_tokens = 0
    obs_summary = {
        "request_id": request_id, "session_id": session_id or "",
        "timestamp": time.time(), "question": question,
        "rewritten_question": "", "outcome": "unknown", "model_used": None,
        "complexity": None, "candidate_tables": "", "cache_hit": False,
        "attempts": 0, "trace_id": get_current_trace_id(),
    }

    with logfire.span(
        "nl2sql_pipeline",
        request_id=request_id,
        session_id=session_id or "",
        timestamp=datetime.now().isoformat(),
        raw_user_question=question,
    ) as pipeline_span:
        try:
            # ── Layer 0: canned responses ──
            canned_reply = match_canned_response(question)
            if canned_reply:
                pipeline_span.set_attribute("outcome", "canned_response")
                obs_summary["outcome"] = "canned_response"
                metric_successful_requests.add(1, {"request_id": request_id})
                return (_blocked_result(canned_reply), None, None, message_history,
                        {"source": "canned", "model_used": None, "attempts": 0, "request_id": request_id})

            # ── Layer 1: free regex guard ──
            if looks_like_injection(question):
                pipeline_span.set_attribute("outcome", "blocked_regex")
                obs_summary["outcome"] = "blocked_regex"
                pipeline_span.set_attribute("attack_detected", True)
                metric_guardrail_blocks.add(1, {"request_id": request_id, "layer": "regex"})
                msg = ("I'm a sales analytics assistant, so I can only help with sales, revenue, "
                       "orders, and store performance questions. If you rephrase this as one of "
                       "those, I'd be glad to help.")
                metric_successful_requests.add(1, {"request_id": request_id})  # correct behavior, not a failure
                return (_blocked_result(msg), None, None, message_history,
                        {"source": "blocked_regex", "model_used": None, "attempts": 0, "request_id": request_id})

            # ── Layer 2: intake (rewrite + candidate_tables + intent) ──
            with logfire.span("intake", request_id=request_id) as intake_span:
                intake_run, intake_model_used, intake_attempts = await run_with_model_cascade(
                    build_intake_agent, question, INTAKE_MODEL_CASCADE, message_history=message_history,
                    stage_name="intake",
                )
                intake: IntakeResult = intake_run.output
                new_history = intake_run.all_messages()
                intake_span.set_attribute("model_used", intake_model_used)
                intake_span.set_attribute("cascade_attempts", intake_attempts)
                intake_span.set_attribute("is_suspicious", intake.is_suspicious)
                intake_span.set_attribute("rewritten_question", intake.rewritten_question)
                intake_span.set_attribute("candidate_tables", intake.candidate_tables)
                intake_span.set_attribute("is_answerable", intake.is_answerable)
                intake_span.set_attribute("answer_type", intake.answer_type)
                intake_span.set_attribute("needs_reasoning", intake.needs_reasoning)

                obs_summary["model_used"] = intake_model_used
                obs_summary["rewritten_question"] = intake.rewritten_question
                _obs_total_tokens += _extract_total_tokens(intake_run, request_id, "intake")

            if intake.is_suspicious:
                pipeline_span.set_attribute("outcome", "blocked_llm_guardrail")
                obs_summary["outcome"] = "blocked_llm_guardrail"
                pipeline_span.set_attribute("attack_type", intake.attack_type or "other")
                metric_guardrail_blocks.add(1, {"request_id": request_id, "layer": "llm"})
                msg = ("I'm a sales analytics assistant, so I can only help with sales, revenue, "
                       "orders, and store performance questions. If you rephrase this as one of "
                       "those, I'd be glad to help.")
                metric_successful_requests.add(1, {"request_id": request_id})
                return (_blocked_result(msg), None, None, new_history,
                        {"source": "blocked_llm_guardrail", "model_used": intake_model_used,
                         "attempts": intake_attempts, "request_id": request_id})

            rewrite = RewriteResult(
                rewritten_question=intake.rewritten_question,
                is_answerable=intake.is_answerable,
                clarification_needed=intake.clarification_needed,
            )

            # Date-range integrity check — see _find_date_range_phrase above.
            # Only runs when intake didn't already flag something; doesn't
            # override an existing clarification_needed with a different one.
            if rewrite.is_answerable and not rewrite.clarification_needed:
                range_phrase = _find_date_range_phrase(question)
                if range_phrase and not _DATE_RANGE_RE.search(rewrite.rewritten_question):
                    rewrite.is_answerable = False
                    rewrite.clarification_needed = (
                        f"Just to confirm — by \"{range_phrase}\" do you mean the whole period "
                        f"from start to end, or those two specific individual days only?"
                    )
                    pipeline_span.set_attribute("date_range_integrity_flagged", True)
                    pipeline_span.set_attribute("date_range_phrase", range_phrase)
                    pipeline_span.set_attribute("date_range_rewrite", rewrite.rewritten_question)

            if not rewrite.is_answerable or rewrite.clarification_needed or not intake.candidate_tables:
                if rewrite.is_answerable and not intake.candidate_tables:
                    rewrite.clarification_needed = rewrite.clarification_needed or (
                        "I couldn't tell which part of the data this relates to — could you be a "
                        "bit more specific?"
                    )
                    rewrite.is_answerable = False
                pipeline_span.set_attribute("outcome", "clarification_needed")
                obs_summary["outcome"] = "clarification_needed"
                obs_summary["rewritten_question"] = rewrite.rewritten_question
                metric_successful_requests.add(1, {"request_id": request_id})
                return (rewrite, None, None, new_history,
                        {"source": "fresh", "model_used": intake_model_used, "attempts": intake_attempts,
                         "answer_type": intake.answer_type, "needs_reasoning": intake.needs_reasoning,
                         "request_id": request_id})

            tables = [t for t in intake.candidate_tables if t in registry.known_tables()]
            if not tables:
                rewrite.is_answerable = False
                rewrite.clarification_needed = (
                    "I couldn't match this to any data I have access to — could you rephrase it?"
                )
                pipeline_span.set_attribute("outcome", "no_matching_table")
                obs_summary["outcome"] = "no_matching_table"
                metric_successful_requests.add(1, {"request_id": request_id})
                return (rewrite, None, None, new_history,
                        {"source": "fresh", "model_used": intake_model_used, "attempts": intake_attempts,
                         "answer_type": intake.answer_type, "needs_reasoning": intake.needs_reasoning,
                         "request_id": request_id})

            # ── Entity disambiguation ──
            with logfire.span("entity_disambiguation", request_id=request_id) as disambig_span:
                ambiguous_found = False
                for t in tables:
                    ambiguous = find_ambiguous_matches(rewrite.rewritten_question, t)
                    for col, phrase_matches in ambiguous.items():
                        phrase, matches = next(iter(phrase_matches.items()))
                        disambig_span.set_attribute("ambiguous_column", col)
                        disambig_span.set_attribute("ambiguous_phrase", phrase)
                        disambig_span.set_attribute("ambiguous_matches", matches)
                        ambiguous_found = True
                        rewrite.is_answerable = False
                        rewrite.clarification_needed = (
                            f"There are a few matches for \"{phrase}\" — {', '.join(matches)}. Which one "
                            f"did you mean, or would you like the combined total across all of them?"
                        )
                        pipeline_span.set_attribute("outcome", "ambiguous_entity")
                        obs_summary["outcome"] = "ambiguous_entity"
                        metric_successful_requests.add(1, {"request_id": request_id})
                        return (rewrite, None, None, new_history,
                                {"source": "fresh", "model_used": intake_model_used, "attempts": intake_attempts,
                                 "answer_type": intake.answer_type, "needs_reasoning": intake.needs_reasoning,
                                 "request_id": request_id})
                disambig_span.set_attribute("ambiguous_found", ambiguous_found)

            # ── Stage 2/3: complexity + model routing (deterministic) ──
            complexity = classify_complexity(intake)
            sql_cascade = build_sql_model_cascade(complexity, manual_override=manual_model_override)
            pipeline_span.set_attribute("complexity", complexity.value)
            pipeline_span.set_attribute("sql_model_cascade", sql_cascade)
            obs_summary["complexity"] = complexity.value
            obs_summary["candidate_tables"] = ",".join(tables)

            # ── Cache check ──
            # Deliberately UNNARROWED — the cache-key fingerprint stays keyed
            # on every categorical column for the table, not just the ones
            # this question needs. That's intentional, not an oversight: if
            # this fingerprint were narrowed to only referenced columns,
            # a cached answer could keep being served after a categorical
            # value change on a column the fingerprint no longer tracks. The
            # prompt narrowing below (per_table_ctx) is a separate concern
            # from cache-invalidation safety and is fine to be stricter than this.
            categorical_ctx: Dict[str, List[str]] = {}
            for t in tables:
                categorical_ctx.update({f"{t}.{k}": v for k, v in fetch_categorical_context(t).items()})

            with logfire.span("cache_lookup", request_id=request_id) as cache_span:
                cached = cache_mod.cache_get(rewrite.rewritten_question, sql_cascade[0], categorical_ctx)
                cache_span.set_attribute("cache_hit", bool(cached))

            if cached:
                metric_cache_hits.add(1, {"request_id": request_id})
                sql_result = SQLResult(**cached["sql_result"])
                validated_sql = cached["validated_sql"]
                pipeline_span.set_attribute("outcome", "cache_hit")
                obs_summary["outcome"] = "cache_hit"
                obs_summary["cache_hit"] = True
                obs_summary["model_used"] = sql_cascade[0]
                metric_successful_requests.add(1, {"request_id": request_id})
                return (rewrite, sql_result, (validated_sql, None), new_history,
                        {"source": "cache", "model_used": sql_cascade[0], "attempts": 0,
                         "complexity": complexity.value, "candidate_tables": tables,
                         "answer_type": intake.answer_type, "needs_reasoning": intake.needs_reasoning,
                         "request_id": request_id})
            metric_cache_misses.add(1, {"request_id": request_id})

            # ── Rule + pattern retrieval (dict/keyword lookup, 0 LLM calls) ──
            with logfire.span("rule_pattern_retrieval", request_id=request_id) as retrieval_span:
                rules = retrieve_rules(rewrite.rewritten_question, tables)
                rules_text = format_rules(rules)
                patterns = retrieve_patterns(rewrite.rewritten_question, tables)
                patterns_text = format_patterns(patterns)
                retrieval_span.set_attribute("rules_retrieved", [r.rule_name for r in rules])
                retrieval_span.set_attribute("patterns_retrieved", [p.name for p in patterns])

            # fuzzy matching across every fuzzy_match_column for the selected
            # table(s) — scanning ALL of them is still correct here (it's the
            # only way to catch e.g. "Ritchie Street" resolving to
            # restaurant_name with zero literal column-name overlap). What's
            # NEW is _filter_fuzzy_noise immediately after: weak hits on
            # columns nothing else about the question touches (e.g. a 56%
            # item_name match on a query that never mentions an item) are
            # dropped before they reach the prompt, instead of being sent
            # through unfiltered exactly as the matcher returned them.
            with logfire.span("fuzzy_matching", request_id=request_id) as fuzzy_span:
                fuzzy_matches_by_col: Dict[str, list] = {}
                for t in tables:
                    for col, matches in find_all_fuzzy_matches(rewrite.rewritten_question, t).items():
                        fuzzy_matches_by_col.setdefault(col, []).extend(matches)
                fuzzy_span.set_attribute("columns_matched_raw", list(fuzzy_matches_by_col.keys()))

                fuzzy_matches_by_col = _filter_fuzzy_noise(fuzzy_matches_by_col, tables, rewrite.rewritten_question)
                fuzzy_span.set_attribute("columns_matched_after_noise_filter", list(fuzzy_matches_by_col.keys()))

            # relevant_columns computed ONCE here — reused for both the
            # categorical-context narrowing below and (implicitly, via the
            # same registry function) the schema narrowing inside
            # build_sql_agent, so the two stay consistent with each other.
            relevant_by_table = _relevant_columns_by_table(tables, rewrite.rewritten_question, fuzzy_matches_by_col)

            # per_table_ctx is what actually reaches the LLM prompt (as
            # opposed to categorical_ctx above, which is cache-key-only).
            # Previously this included every categorical column for the
            # table unconditionally — now filtered to the columns this
            # question actually seems to need, which is why "status" alone
            # shows up instead of status/order_type/payment_type/category_name
            # every time.
            per_table_ctx: Dict[str, List[str]] = {}
            for t in tables:
                full_ctx = fetch_categorical_context(t)
                per_table_ctx.update({k: v for k, v in full_ctx.items() if k in relevant_by_table[t]})

            # ── SQL generation: fixer loop nested inside the routed model cascade ──
            validated_sql = None
            validation_error = None
            sql_result = None
            prompt_text = rewrite.rewritten_question
            attempt = 0
            final_model_used = sql_cascade[0]

            with logfire.span("sql_generation", request_id=request_id) as gen_span:
                for cascade_pos, current_model in enumerate(sql_cascade):
                    if attempt >= MAX_SQL_ATTEMPTS_TOTAL:
                        break
                    final_model_used = current_model
                    tries_on_this_model = 0

                    while tries_on_this_model < MAX_SQL_ATTEMPTS_PER_MODEL and attempt < MAX_SQL_ATTEMPTS_TOTAL:
                        attempt += 1
                        tries_on_this_model += 1
                        with logfire.span(
                            f"sql_generation_attempt_{attempt}", request_id=request_id, attempt=attempt,
                            model=current_model, cascade_position=cascade_pos + 1,
                        ) as attempt_span:
                            # question=rewrite.rewritten_question is the actual
                            # fix — without it, build_sql_agent silently took
                            # the full-schema branch regardless of everything
                            # else in this function (see agents.py's loud
                            # sql_prompt_schema_not_narrowed warning, which
                            # would have been firing on every request before
                            # this line was added).
                            sql_agent = build_sql_agent(
                                current_model, tables, rules_text, patterns_text, per_table_ctx,
                                fuzzy_matches_by_col, question=rewrite.rewritten_question,
                            )
                            try:
                                sql_run = await sql_agent.run(prompt_text)
                                sql_result = sql_run.output
                                attempt_span.set_attribute("generated_sql", sql_result.sql)
                                _obs_total_tokens += _extract_total_tokens(sql_run, request_id, "sql_generation")
                            except Exception as e:
                                validation_error = f"model failed to produce valid output: {e}"
                                attempt_span.set_attribute("technical_failure", True)
                                attempt_span.set_attribute("error", str(e))
                                logfire.warn("sql_generation_technical_failure", request_id=request_id,
                                             attempt=attempt, model=current_model, error=str(e))
                                sql_result = None
                                prompt_text = rewrite.rewritten_question
                                break

                            try:
                                validated_sql = validate_sql(sql_result.sql)
                                validation_error = None
                                attempt_span.set_attribute("validation_passed", True)
                            except ValidationError as e:
                                validation_error = str(e)
                                attempt_span.set_attribute("validation_passed", False)
                                attempt_span.set_attribute("validation_error", validation_error)
                                metric_validation_failures.add(1, {"request_id": request_id, "attempt": attempt})
                                if tries_on_this_model < MAX_SQL_ATTEMPTS_PER_MODEL and attempt < MAX_SQL_ATTEMPTS_TOTAL:
                                    prompt_text = (
                                        f"{rewrite.rewritten_question}\n\nYour previous SQL failed validation:\n"
                                        f"{validation_error}\nFix the query and try again. Single SELECT only."
                                    )

                        if validated_sql:
                            break

                    if validated_sql:
                        break
                    if cascade_pos < len(sql_cascade) - 1:
                        metric_model_escalations.add(1, {
                            "request_id": request_id, "from_model": current_model,
                            "to_model": sql_cascade[cascade_pos + 1],
                        })
                    prompt_text = rewrite.rewritten_question  # fresh model, fresh prompt

                gen_span.set_attribute("total_attempts", attempt)
                gen_span.set_attribute("final_model_used", final_model_used)
                gen_span.set_attribute("succeeded", bool(validated_sql))

            outcome = "validated" if validated_sql else "validation_failed"
            pipeline_span.set_attribute("outcome", outcome)
            pipeline_span.set_attribute("sql_attempts", attempt)
            obs_summary["outcome"] = outcome
            obs_summary["model_used"] = final_model_used
            obs_summary["attempts"] = attempt
            obs_summary["rewritten_question"] = rewrite.rewritten_question

            # No Reasoning Engine exists yet — see core/models.py's
            # IntakeResult.needs_reasoning docstring. Rather than silently
            # returning only the data for an ANALYTICAL_REASONING/HYBRID
            # question (making it look like reasoning happened when it
            # didn't), or refusing the question outright (which JOB D no
            # longer does for these), say plainly that only the underlying
            # data is being shown. This is surfaced via `assumptions`,
            # which app_nl.py already renders to the user for every result.
            if validated_sql and sql_result and intake.needs_reasoning:
                sql_result.assumptions.append(
                    "This shows the relevant underlying data only — deeper trend/pattern/"
                    "root-cause analysis isn't available yet in this assistant."
                )

            meta = {
                "source": "fresh", "model_used": final_model_used, "attempts": attempt,
                "complexity": complexity.value, "candidate_tables": tables,
                "rules_used": [r.rule_name for r in rules], "patterns_used": [p.name for p in patterns],
                "answer_type": intake.answer_type, "needs_reasoning": intake.needs_reasoning,
                "request_id": request_id,
            }

            if validated_sql and sql_result:
                cache_mod.cache_set(
                    rewrite.rewritten_question, sql_cascade[0], categorical_ctx,
                    {"sql_result": sql_result.model_dump(), "validated_sql": validated_sql},
                )
                metric_successful_requests.add(1, {"request_id": request_id})
            else:
                metric_failed_requests.add(1, {"request_id": request_id, "reason": "validation_failed"})

            return rewrite, sql_result, (validated_sql, validation_error), new_history, meta

        except Exception as e:
            # Last-resort catch-all: ANY unhandled exception anywhere in the
            # pipeline (including inside a provider SDK, not just our own
            # code) is logged here with a FULL traceback via logfire.exception
            # — tied to this request_id, so it's actually diagnosable in
            # Logfire instead of only showing a one-line message in the UI —
            # and turned into a graceful response instead of crashing the
            # Streamlit session.
            logfire.exception("pipeline_unhandled_exception", request_id=request_id, error=str(e))
            metric_failed_requests.add(1, {"request_id": request_id, "reason": "unhandled_exception"})
            obs_summary["outcome"] = "unhandled_exception"
            fallback = RewriteResult(
                rewritten_question=question,
                is_answerable=False,
                clarification_needed=(
                    "I hit an unexpected error just now — this is usually transient. Please try again "
                    "in a moment. (If it keeps happening, check the Logfire trace for request_id "
                    f"{request_id}.)"
                ),
            )
            return fallback, None, None, message_history, {
                "source": "error", "model_used": None, "attempts": 0, "request_id": request_id,
                "error": str(e), "error_type": type(e).__name__,
            }

        finally:
            # Fires exactly once regardless of which branch above returned
            # (including the except block) — see observability.py's
            # log_request for what happens to this row and why it's a
            # local SQLite store rather than a Langfuse API call.
            obs_summary["elapsed_ms"] = round((time.perf_counter() - _obs_start) * 1000, 1)
            obs_summary["total_tokens"] = _obs_total_tokens or None
            log_observability_row(obs_summary)