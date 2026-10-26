"""
app_nl — executor.py
=======================
Cost guardrails (EXPLAIN row-scan estimate, missing-WHERE / excessive-date-
range heuristics), row-cap safety limit, and the post-validation execution
repair loop. Same design as your existing app, table-agnostic.
"""
import re
import time
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
import sqlglot
import logfire
from sqlglot import expressions as exp
from sqlalchemy import text

from config import (
    EXECUTION_ROW_CAP, QUERY_ROW_SCAN_LIMIT, MAX_DATE_RANGE_DAYS,
    QUERY_MAX_EXECUTION_TIME_MS, MAX_SQL_REPAIR_ATTEMPTS,
)
from data.db import get_engine
from .validator import validate_sql, ValidationError

_INTERVAL_UNIT_DAYS = {"DAY": 1, "WEEK": 7, "MONTH": 30, "YEAR": 365, "HOUR": 1 / 24}


def _estimate_date_range_days(parsed: exp.Select) -> Optional[float]:
    total_days = 0.0
    found_any = False
    for interval in parsed.find_all(exp.Interval):
        try:
            amount = float(interval.this.this)
            unit = str(interval.args.get("unit") or "DAY").upper()
            for key, mult in _INTERVAL_UNIT_DAYS.items():
                if key in unit:
                    total_days += amount * mult
                    found_any = True
                    break
        except (TypeError, ValueError, AttributeError):
            continue
    date_literals = []
    for lit in parsed.find_all(exp.Literal):
        if lit.is_string:
            s = str(lit.this)
            if re.match(r"^\d{4}-\d{2}-\d{2}", s):
                try:
                    date_literals.append(datetime.strptime(s[:10], "%Y-%m-%d"))
                except ValueError:
                    pass
    if len(date_literals) >= 2:
        span_days = (max(date_literals) - min(date_literals)).days
        total_days = max(total_days, span_days)
        found_any = True
    return total_days if found_any else None


def _has_missing_filter(parsed: exp.Select) -> bool:
    if parsed.args.get("where") is not None:
        return False
    has_aggregate = bool(list(parsed.find_all((exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count))))
    has_group_by = bool(parsed.args.get("group"))
    return not has_aggregate and not has_group_by


def check_query_cost(sql: str, conn) -> Tuple[Optional[str], Optional[int]]:
    warnings: List[str] = []
    total_estimated_rows: Optional[int] = None
    try:
        rows = conn.execute(text(f"EXPLAIN {sql}")).fetchall()
        total_estimated_rows = 0
        for row in rows:
            row_dict = dict(row._mapping) if hasattr(row, "_mapping") else {}
            estimated = row_dict.get("rows")
            if estimated:
                total_estimated_rows += int(estimated)
        if total_estimated_rows > QUERY_ROW_SCAN_LIMIT:
            warnings.append(f"estimates scanning ~{total_estimated_rows:,} rows (limit {QUERY_ROW_SCAN_LIMIT:,})")
    except Exception:
        pass
    try:
        parsed = sqlglot.parse_one(sql, read="mysql")
        if isinstance(parsed, exp.Select):
            if _has_missing_filter(parsed):
                warnings.append("has no WHERE clause on row-level data — may return the entire table")
            date_range = _estimate_date_range_days(parsed)
            if date_range and date_range > MAX_DATE_RANGE_DAYS:
                warnings.append(f"spans an estimated ~{date_range:.0f} days (limit {MAX_DATE_RANGE_DAYS})")
    except Exception:
        pass
    if warnings:
        return "This query " + "; and it ".join(warnings) + ". It may be slow.", total_estimated_rows
    return None, total_estimated_rows


def add_safety_limit(sql: str, cap: int = EXECUTION_ROW_CAP) -> str:
    try:
        parsed = sqlglot.parse_one(sql, read="mysql")
        if isinstance(parsed, exp.Select) and parsed.args.get("limit"):
            return sql
    except Exception:
        pass
    return f"{sql}\nLIMIT {cap}"


async def execute_sql_with_recovery(initial_sql: str, rewritten_question: str, tables, rules_text, patterns_text,
                                     categorical_ctx, fuzzy_matches_by_col, model: str, sql_agent_builder,
                                     request_id: Optional[str] = None):
    """Returns (df, final_sql, cost_warning, explain_plan_rows, execution_time_ms, repair_log)."""
    eng = get_engine()
    current_sql = initial_sql
    repair_log: List[dict] = []
    last_error = None

    with logfire.span("sql_execution", request_id=request_id or "", initial_sql=initial_sql) as exec_span:
        for repair_attempt in range(0, MAX_SQL_REPAIR_ATTEMPTS + 1):
            exec_start = time.perf_counter()
            try:
                with eng.connect() as conn:
                    conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={QUERY_MAX_EXECUTION_TIME_MS}"))
                    cost_warning, explain_plan_rows = check_query_cost(current_sql, conn)
                    df = pd.read_sql(text(add_safety_limit(current_sql)), conn)
                execution_time_ms = round((time.perf_counter() - exec_start) * 1000, 1)
                exec_span.set_attribute("execution_time_ms", execution_time_ms)
                exec_span.set_attribute("row_count", len(df))
                exec_span.set_attribute("repair_attempts", len(repair_log))
                exec_span.set_attribute("cost_warning", cost_warning or "")
                exec_span.set_attribute("final_sql", current_sql)
                return df, current_sql, cost_warning, explain_plan_rows, execution_time_ms, repair_log
            except Exception as e:
                last_error = e
                logfire.warn("sql_execution_error", request_id=request_id or "", repair_attempt=repair_attempt,
                             sql=current_sql, error=str(e))
                if repair_attempt >= MAX_SQL_REPAIR_ATTEMPTS:
                    exec_span.set_attribute("succeeded", False)
                    raise
                with logfire.span(
                    "sql_repair_attempt", request_id=request_id or "", repair_attempt=repair_attempt + 1,
                    original_sql=current_sql, error_message=str(e),
                ) as repair_span:
                    repair_prompt = (
                        f"{rewritten_question}\n\nYour previous SQL failed when actually run against the "
                        f"database:\n{e}\n\nThe SQL was:\n{current_sql}\n\nFix the query so it runs "
                        f"successfully. Single SELECT only."
                    )
                    sql_agent = sql_agent_builder(model, tables, rules_text, patterns_text, categorical_ctx,
                                                   fuzzy_matches_by_col)
                    try:
                        sql_run = await sql_agent.run(repair_prompt)
                        repaired_sql = validate_sql(sql_run.output.sql)
                        repair_span.set_attribute("repaired_sql", repaired_sql)
                    except Exception as repair_e:
                        repair_span.set_attribute("repair_failed", True)
                        raise last_error from repair_e
                repair_log.append({
                    "repair_attempt": repair_attempt + 1,
                    "original_sql": current_sql,
                    "error_message": str(e),
                    "repaired_sql": repaired_sql,
                })
                current_sql = repaired_sql

        raise last_error
