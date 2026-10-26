"""
app_nl — validator.py
=======================
sqlglot-based AST validator, generalized to be registry-driven so adding a
new table never requires touching validator logic — it reads column/grain
metadata from registry.py instead of hardcoded constants.

This is your existing production validator's logic (forbidden node types,
column-existence check, invoice-dedup business rule, metric-logic business
rule) — UNCHANGED IN SPIRIT, just parameterized by table.
"""
import re
from typing import List, Optional, Tuple

import sqlglot
from sqlglot import expressions as exp

import retrieval.registry as registry
from retrieval.registry import METRIC_REGISTRY


class ValidationError(Exception):
    pass


FORBIDDEN_NODE_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter,
    exp.Create, exp.Merge, exp.Command,
)


def validate_columns_exist(parsed: exp.Select, tables_in_query: set) -> None:
    known_cols = set()
    for t in tables_in_query:
        known_cols |= registry.known_columns(t)
    known_aliases = {a.alias_or_name.lower() for a in parsed.find_all(exp.Alias) if a.alias_or_name}
    referenced = {col.name.lower() for col in parsed.find_all(exp.Column) if col.name}
    unknown = referenced - known_cols - known_aliases
    if unknown:
        raise ValidationError(
            f"Query references column(s) that don't exist on {sorted(tables_in_query)}: {sorted(unknown)}"
        )

# NEW
def _select_dedupes_by_keys(select: exp.Select, join_keys: set) -> bool:
    """True if this SELECT itself guarantees at most one row per join key —
    either via SELECT DISTINCT <join_key, ...> or GROUP BY <join_key, ...>.
    A CTE's inner SELECT (e.g. `GROUP BY invoice_no`) is just as valid a
    dedup mechanism as a derived-table `SELECT DISTINCT invoice_no, ...`."""
    if not isinstance(select, exp.Select):
        return False

    if select.args.get("distinct"):
        selected_cols = set()
        for e in select.expressions:
            selected_cols.add(e.alias_or_name.lower())
            for col in e.find_all(exp.Column):
                selected_cols.add(col.name.lower())
        if selected_cols & join_keys:
            return True

    group = select.args.get("group")
    if group:
        grouped_cols = set()
        for e in group.expressions:
            grouped_cols.add(e.alias_or_name.lower() if hasattr(e, "alias_or_name") else None)
            for col in e.find_all(exp.Column):
                grouped_cols.add(col.name.lower())
        if grouped_cols & join_keys:
            return True

    return False

def _subquery_has_dedup(subquery: exp.Subquery, join_keys: set) -> bool:
    inner = subquery.this
    if not isinstance(inner, exp.Select):
        return False
    if not inner.args.get("distinct"):
        return False
    selected_cols = set()
    for e in inner.expressions:
        selected_cols.add(e.alias_or_name.lower())
        for col in e.find_all(exp.Column):
            selected_cols.add(col.name.lower())
    return bool(selected_cols & join_keys)


# NEW
def validate_grain_dedup_rule(parsed: exp.Select, tables_in_query: set) -> None:
    """Generalization of your validate_invoice_dedup_rule — for each table
    in the query, any SUM/AVG/MIN/MAX on a column the registry marks as
    'invoice-grain aggregatable' must read from a properly deduplicated
    select (a derived-table SELECT DISTINCT, or a CTE/subquery GROUP BY)
    keyed on the table's join key."""
    from_expr = parsed.args.get("from_") or parsed.args.get("from")
    subqueries = list(from_expr.find_all(exp.Subquery)) if from_expr else []
    for j in parsed.find_all(exp.Join):
        subqueries.extend(j.find_all(exp.Subquery))

    cte_selects = [
        cte.this for cte in parsed.find_all(exp.CTE)
        if isinstance(cte.this, exp.Select)
    ]
    dedup_candidate_selects = [sq.this for sq in subqueries if isinstance(sq.this, exp.Select)] + cte_selects

    for table in tables_in_query:
        agg_cols = registry.invoice_level_agg_columns(table)
        if not agg_cols:
            continue
        join_keys = {k.lower() for k in registry.TABLE_REGISTRY.get(table, {}).get("join_keys", [])}
        if not join_keys:
            continue
        if any(_select_dedupes_by_keys(sel, join_keys) for sel in dedup_candidate_selects):
            continue

        violations = []
        for agg in parsed.find_all((exp.Sum, exp.Avg, exp.Min, exp.Max)):
            enclosing = agg.find_ancestor(exp.Select)
            if enclosing is not None and _select_dedupes_by_keys(enclosing, join_keys):
                # this aggregation IS the dedup mechanism (e.g. MAX(total)
                # GROUP BY invoice_no inside a CTE) — not a violation.
                continue
            target = agg.this
            if isinstance(target, exp.Column) and target.name.lower() in agg_cols:
                violations.append(f"{agg.key.upper()}({target.name})")
        if violations:
            raise ValidationError(
                f"Business rule violation on {table} — row-level aggregation without dedup: "
                f"{', '.join(violations)} read(s) directly (or from a select that doesn't actually "
                f"deduplicate by {sorted(join_keys)}), which repeats across every detail row of the same "
                f"record. Wrap in a SELECT DISTINCT {', '.join(sorted(join_keys))}, <col> subquery, or a "
                f"GROUP BY {', '.join(sorted(join_keys))} CTE/subquery, first."
            )


def validate_text_match_pattern(parsed: exp.Select, tables_in_query: set) -> None:
    """Enforces LOWER(col) LIKE LOWER('%value%') on every column the registry
    marks as fuzzy_match_columns (restaurant_name, item_name, category_name,
    area, etc) — this is the code-level backstop for the fuzzy_text_matching
    business rule, same relationship as validate_grain_dedup_rule has to the
    invoice-dedup rule. Two things are rejected:
      1. Exact '=' comparison against a string literal on a fuzzy-match column.
      2. A LIKE comparison on a fuzzy-match column that isn't wrapped in
         LOWER() on BOTH sides (catches `restaurant_name LIKE '%Royapettah%'`
         and `LOWER(restaurant_name) LIKE '%Royapettah%'` — half-wrapped is
         still a bug, since a casing mismatch on the literal side still misses
         real rows).
    """
    violations: List[str] = []

    for table in tables_in_query:
        fuzzy_cols = {c.lower() for c in registry.fuzzy_match_columns(table)}
        if not fuzzy_cols:
            continue

        for eq in parsed.find_all(exp.EQ):
            left, right = eq.left, eq.right
            for col_side, lit_side in ((left, right), (right, left)):
                if (
                    isinstance(col_side, exp.Column)
                    and col_side.name.lower() in fuzzy_cols
                    and isinstance(lit_side, exp.Literal)
                    and lit_side.is_string
                ):
                    violations.append(
                        f"{col_side.name} = '{lit_side.this}' uses exact match on a fuzzy-match "
                        f"column — use LOWER({col_side.name}) LIKE LOWER('%{lit_side.this}%') instead"
                    )

        for like in parsed.find_all(exp.Like):
            col_expr, pattern_expr = like.this, like.expression
            cols_here = list(col_expr.find_all(exp.Column))
            if not cols_here or cols_here[0].name.lower() not in fuzzy_cols:
                continue
            col_name = cols_here[0].name
            left_wrapped = isinstance(col_expr, exp.Lower)
            right_wrapped = isinstance(pattern_expr, exp.Lower)
            if not (left_wrapped and right_wrapped):
                violations.append(
                    f"LIKE comparison on {col_name} must wrap BOTH sides in LOWER() — "
                    f"e.g. LOWER({col_name}) LIKE LOWER('%value%'), not a partially- or un-wrapped version"
                )

    if violations:
        raise ValidationError("Text match pattern violation: " + "; ".join(violations))


def validate_metric_logic(parsed: exp.Select) -> None:
    """Generalization of your validate_metric_logic — reads never_use_columns/
    detection_pattern from the metric registry instead of a hardcoded dict."""
    violations = []
    for metric_name, meta in METRIC_REGISTRY.items():
        if not meta.never_use_columns or not meta.detection_pattern:
            continue
        pattern_keyword = meta.detection_pattern.lower().split()[0]

        for cmp_node in parsed.find_all((exp.EQ, exp.Like)):
            left, right = cmp_node.left, cmp_node.right
            for col_side, lit_side in ((left, right), (right, left)):
                if not (isinstance(col_side, exp.Column) and isinstance(lit_side, exp.Literal) and lit_side.is_string):
                    continue
                if col_side.name.lower() not in {c.lower() for c in meta.never_use_columns}:
                    continue
                literal_text = str(lit_side.this).lower()
                if pattern_keyword in literal_text or metric_name.replace("_", " ").split()[0] in literal_text:
                    violations.append(
                        f"'{metric_name}' metric detected via {col_side.name} = '{lit_side.this}', but that "
                        f"column must never be used for this — use {meta.detection_column} LIKE "
                        f"'%{meta.detection_pattern}%' instead"
                    )
    if violations:
        raise ValidationError("Business metric logic violation: " + "; ".join(violations))


def validate_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()

    if ";" in sql:
        raise ValidationError("Multiple statements are not allowed.")

    try:
        parsed = sqlglot.parse_one(sql, read="mysql")
    except Exception as e:
        raise ValidationError(f"Could not parse generated SQL: {e}")

    if not isinstance(parsed, exp.Select):
        raise ValidationError("Only SELECT statements are allowed.")

    for bad_type in FORBIDDEN_NODE_TYPES:
        if list(parsed.find_all(bad_type)):
            raise ValidationError(
                f"Query contains a forbidden operation: {bad_type.__name__}"
            )

    # CTE names are temporary query-level result sets, not physical tables
    cte_names = {
        cte.alias_or_name.lower()
        for cte in parsed.find_all(exp.CTE)
        if cte.alias_or_name
    }

    # Extract only physical tables, excluding CTE references
    tables = {
        t.name.lower()
        for t in parsed.find_all(exp.Table)
        if t.name.lower() not in cte_names
    }

    if tables:
        unexpected = tables - {
            table.lower() for table in registry.known_tables()
        }

        if unexpected:
            raise ValidationError(
                f"Query references unexpected/unregistered table(s): "
                f"{sorted(unexpected)}"
            )

        validate_columns_exist(parsed, tables)
        validate_grain_dedup_rule(parsed, tables)
        validate_text_match_pattern(parsed, tables)
        validate_metric_logic(parsed)

    return sql