"""
app_nl — registry.py
=====================
Source of truth for table/column metadata, business rules, SQL patterns,
and metric definitions. Plain Python dicts/lists, looked up directly — NOT
an embedding-retrieval corpus. That's the deliberate Phase-1 choice: at a
handful of tables you can enumerate everything relevant to a question by
table-name lookup; you don't need semantic search over schema chunks yet.

TO ADD A NEW TABLE:
  1. Add an entry to TABLE_REGISTRY with its columns (grain + is_aggregatable
     follow the same convention as your existing COLUMN_METADATA).
  2. Add any table-specific BusinessRule entries (mandatory=True for anything
     a validator also enforces).
  3. Add SQLPattern entries for its common query shapes.
  4. Add MetricDefinition entries for its business metrics.
  5. Nothing else changes — pipeline.py, validator.py, and app_nl.py are all
     table-agnostic and read this registry dynamically.

PROMPT-SIZE DISCIPLINE (read this before adding a column description or a
business rule):
  - Column descriptions in TABLE_REGISTRY are STRUCTURAL FACTS ONLY — what a
    column IS (grain, whether it's aggregatable, PII, categorical). Never put
    an INSTRUCTION here ("requires dedup before...", "always filter by...").
  - Instructions belong in BUSINESS_RULES, stated exactly once per rule. If
    you're about to write "see business rules" in a column description, stop
    and check the rule actually says what you need — don't restate it here
    "just to be safe". A fact restated in two places that a query touches
    (schema doc AND a mandatory rule) is sent twice on every single request
    for that table.
  - render_schema_doc() must never contain an instruction — if you find
    yourself adding one there, it belongs in a BusinessRule instead.

WHEN THIS STOPS SCALING (per the architecture review):
  - candidate_tables accuracy from the intake LLM call visibly degrades
    (roughly 15+ tables) -> add a dedicated Table Selector stage.
  - Schema descriptions sent per query get too large even after table-level
    filtering -> add embedding-based column retrieval (Stage 4 in the fuller
    design doc) instead of sending a table's full column list.
  - Few-shot / pattern count per table exceeds ~15-20 -> add embedding
    retrieval over SQLPattern/few-shot instead of keyword matching.
"""
from typing import Dict, List, Optional, Tuple
from core.models import BusinessRule, SQLPattern, MetricDefinition

BUSINESS_DAY_CUTOFF_HOUR = 4
DEFAULT_SUCCESS_STATUS = "Success"

# ─────────────────────────────────────────────────────────────────────────
# TABLE REGISTRY — column_name -> (grain, is_aggregatable, description)
# pet_pooja_sales seeded from your real production schema/COLUMN_METADATA.
#
# Column descriptions below are deliberately terse and structural — what
# the column IS, not what to DO about it. "Requires invoice dedup before
# aggregation" used to be repeated on every one of the 12 aggregatable
# columns here AND stated in full (with example SQL) in the mandatory
# pps_invoice_dedup business rule below — i.e. the same instruction sent
# 13 times in every prompt for this table. That instruction now lives
# exactly once, in pps_invoice_dedup. The "*" marker on aggregatable
# columns (rendered in render_schema_doc) is the only pointer needed here.
# ─────────────────────────────────────────────────────────────────────────
TABLE_REGISTRY: Dict[str, Dict] = {
    "pet_pooja_sales": {
        "domain": "restaurant_sales",
        "grain_description": "ONE ROW PER ITEM PER INVOICE — invoice-level fields repeat across every item row of the same invoice_no.",
        "categorical_columns": ["status", "order_type", "payment_type", "category_name"],
        # Near-always-relevant for this table's dominant question shape
        # (sales/order lookups). Included unconditionally by
        # relevant_columns_for_question rather than only as an empty-match
        # fallback — cheap (4 columns) and covers the large majority of
        # real questions even when nothing else matches by name or fuzzy hit.
        "baseline_columns": ["invoice_no", "date", "status", "total"],
        # BUSINESS GLOSSARY — the piece that was silently dropped when this
        # registry was refactored from the old monolithic app's
        # DOMAIN_GLOSSARY dict into TABLE_REGISTRY's tuple-based columns.
        # Without this, relevant_columns_for_question can only match a
        # column by its LITERAL name — "split by restaurant" never
        # substring-matches "restaurant_name" (missing "_name"), so the
        # column silently never made it into the schema sent to the model,
        # which then correctly (from what it was shown) said the column
        # didn't exist. Add an entry here any time you see this failure
        # mode for a different column/phrasing.
        "column_synonyms": {
            "restaurant_name": ["restaurant", "store", "location", "branch", "outlet"],
            "area": ["region", "zone"],
            "order_type": ["dine in", "delivery", "takeaway", "order mode"],
            "payment_type": ["payment method", "payment mode"],
            "category_name": ["category", "menu category"],
            "item_name": ["item", "menu item", "dish", "product"],
        },
        # Columns that must NEVER be filtered with exact '=' — real values carry
        # prefix/casing/spelling variance (e.g. "Chaat Anna - Royapettah" vs
        # "Royapettah"), so these always need LOWER(col) LIKE LOWER('%value%'),
        # with the value fuzzy-matched against real live data first (see db.py).
        # Enforced by validator.validate_text_match_pattern — not just a prompt hint.
        # The full LOWER()/LIKE instruction lives once, in the auto-generated
        # mandatory rule from build_text_match_rules() below — not repeated here.
        "fuzzy_match_columns": ["restaurant_name", "item_name", "category_name", "area"],
        "join_keys": ["invoice_no"],
        "columns": {
            "s_no":               ("meta", False, "Auto-increment row id — not meaningful for analysis."),
            "restaurant_name":    ("invoice", False, "Store/location. Sometimes carries a 'Chaat Anna' prefix, sometimes doesn't, for the same place."),
            "invoice_no":         ("invoice", False, "Invoice identifier — dedup/join key."),
            "date":               ("invoice", False, "DATETIME of the order — business-day cutoff applies, see business rules."),
            "payment_type":       ("invoice", False, "Categorical."),
            "order_type":         ("invoice", False, "Categorical — NOT where parcel/packing orders live, see business rules."),
            "status":             ("invoice", False, "Categorical — order status."),
            "my_amount":          ("invoice", True,  "Invoice-level amount."),
            "total_tax":          ("invoice", True,  "Invoice-level tax."),
            "discount":           ("invoice", True,  "Invoice-level discount."),
            "total":              ("invoice", True,  "Invoice-level order total — the primary 'sales amount' field."),
            "delivery_charge":    ("invoice", True,  "Invoice-level."),
            "container_charge":   ("invoice", True,  "Invoice-level."),
            "service_charge":     ("invoice", True,  "Invoice-level."),
            "additional_charge":  ("invoice", True,  "Invoice-level."),
            "area":               ("invoice", False, "Broader geography grouping — distinct from restaurant_name, don't conflate."),
            "virtual_brand_name": ("invoice", False, "Invoice-level categorical."),
            "brand_grouping":     ("invoice", False, "Invoice-level categorical."),
            "assign_to":          ("invoice", False, "Invoice-level categorical."),
            "customer_phone":     ("invoice", False, "PII."),
            "customer_name":      ("invoice", False, "PII."),
            "customer_address":   ("invoice", False, "PII."),
            "persons":            ("invoice", False, "Headcount."),
            "order_cancel_reason":("invoice", False, "Only populated on cancellations."),
            "deduction_charge":   ("invoice", True,  "Invoice-level."),
            "waived_off":         ("invoice", True,  "Invoice-level."),
            "round_off":          ("invoice", True,  "Invoice-level."),
            "sap_code":           ("invoice", False, "Reference code."),
            "source_file":        ("invoice", False, "Ingestion metadata."),
            "loaded_at":          ("invoice", False, "Ingestion metadata."),
            "item_name":          ("item", False, "Menu item name — unique per row, safe to GROUP BY."),
            "category_name":      ("item", False, "Menu category — unique per row."),
            "item_price":         ("item", False, "Per-item price — unique per row."),
            "item_quantity":      ("item", False, "Per-item quantity — unique per row, safe to SUM directly."),
            "item_total":         ("item", False, "Per-item revenue — unique per row, safe to SUM directly."),
        },
    },
}


def known_tables() -> List[str]:
    return list(TABLE_REGISTRY.keys())


def known_columns(table: str) -> set:
    return set(TABLE_REGISTRY.get(table, {}).get("columns", {}).keys())


def invoice_level_agg_columns(table: str) -> set:
    cols = TABLE_REGISTRY.get(table, {}).get("columns", {})
    return {c for c, (grain, agg, _) in cols.items() if grain == "invoice" and agg}


def categorical_columns(table: str) -> List[str]:
    return TABLE_REGISTRY.get(table, {}).get("categorical_columns", [])


def fuzzy_match_columns(table: str) -> List[str]:
    return TABLE_REGISTRY.get(table, {}).get("fuzzy_match_columns", [])


def relevant_columns_for_question(table: str, question: str) -> set:
    """OPT-IN via build_sql_agent's `question` param — see agents.py, which
    also unions in any column that got a genuine fuzzy-match hit
    (fuzzy_matches_by_col) before calling render_schema_doc. That union
    matters: a mention like "Ritchie Street" will never substring-match the
    column name `restaurant_name`, but if the fuzzy-match layer already
    resolved it to a real live value, that IS the relevance signal — a
    stronger one than any keyword heuristic here.

    This function alone gives you:
      1. baseline_columns (registry-declared, near-always-relevant — cheap
         insurance so a question that matches nothing by name still gets a
         usable schema instead of degrading to nothing).
      2. name-substring matches against the question text.
      3. item-level columns when the question is shaped like an item/menu
         question (keyword-gated, not always included).
    """
    meta = TABLE_REGISTRY.get(table)
    if not meta:
        return set()
    q = question.lower()
    cols = meta["columns"]

    selected = set(meta.get("baseline_columns", [])) & set(cols)
    selected |= {c for c in cols if c.lower().replace("_", " ") in q or c.lower() in q}

    # Business-glossary match — see column_synonyms above. This is what
    # catches "split by restaurant" / "group by store" / "per branch"
    # resolving to restaurant_name even though none of those phrasings
    # contain the literal column name anywhere.
    for col, synonyms in meta.get("column_synonyms", {}).items():
        if col in cols and any(syn in q for syn in synonyms):
            selected.add(col)

    if any(w in q for w in ("item", "menu", "dish", "product", "sold", "selling", "quantity")):
        selected |= {c for c, (grain, _, _) in cols.items() if grain == "item"}

    if any(cols[c][1] for c in selected):  # any selected column is aggregatable
        join_key = meta.get("join_keys", ["invoice_no"])[0]
        selected.add(join_key)

    return selected & set(cols)


# Fuzzy matches below this score are only kept if their column is otherwise
# relevant to the question (name/baseline match) — see filter_fuzzy_noise.
DEFAULT_FUZZY_NOISE_CUTOFF = 0.75


def filter_fuzzy_noise(
    fuzzy_matches_by_col: Dict[str, List[Tuple[str, float]]],
    tables: List[str],
    question: str,
    noise_cutoff: float = DEFAULT_FUZZY_NOISE_CUTOFF,
) -> Dict[str, List[Tuple[str, float]]]:
    """Drops weak fuzzy hits on columns the question doesn't otherwise
    reference. Scanning every fuzzy_match_column upstream is still correct
    and necessary — it's the only way to catch e.g. "Ritchie Street"
    resolving to restaurant_name with zero literal name overlap — this only
    trims what actually reaches the LLM afterward. Lives here (not in
    pipeline.py) specifically so app_nl.py's execution/repair path can
    apply the identical narrowing instead of maintaining a second copy
    that could silently drift out of sync with pipeline.py's."""
    if not fuzzy_matches_by_col:
        return {}
    name_relevant = set()
    for t in tables:
        name_relevant |= relevant_columns_for_question(t, question)

    filtered: Dict[str, List[Tuple[str, float]]] = {}
    for col, matches in fuzzy_matches_by_col.items():
        strong = [m for m in matches if m[1] >= noise_cutoff]
        if strong:
            filtered[col] = strong
        elif col in name_relevant:
            filtered[col] = matches  # weak, but the column is otherwise clearly relevant
        # else: dropped — weak match, no other signal this column matters
    return filtered


def relevant_columns_by_table(
    tables: List[str], question: str,
    fuzzy_matches_by_col: Optional[Dict[str, List[Tuple[str, float]]]] = None,
) -> Dict[str, set]:
    """Union of relevant_columns_for_question's baseline/name/item signals
    with any column that already has a genuine fuzzy-match hit, per table.
    Same reasoning as filter_fuzzy_noise above for why this lives in
    registry.py rather than only in pipeline.py."""
    result = {}
    for t in tables:
        selected = relevant_columns_for_question(t, question)
        if fuzzy_matches_by_col:
            selected |= (set(fuzzy_matches_by_col.keys()) & known_columns(t))
        result[t] = selected
    return result


def render_schema_lines(table: str, columns: Optional[set] = None) -> str:
    """Compact variant of render_schema_doc — plain '- col: description'
    lines, no grain/aggregatable marker, no footnote. Matches the minimal
    prompt template (agents.py's SQL_SYSTEM_PROMPT_TEMPLATE). Safe to drop
    the aggregatable marker here specifically because pps_invoice_dedup's
    rule_text was generalized (see registry.py's BUSINESS_RULES) to name
    "any invoice-level aggregatable column", not just the one example
    column — so the instruction still applies even without a per-column
    flag in the schema pointing at it. render_schema_doc (verbose, with the
    marker) is left intact for any other caller."""
    meta = TABLE_REGISTRY.get(table)
    if not meta:
        return f"(No registry entry for table '{table}')"
    all_cols = meta["columns"]
    cols = {c: all_cols[c] for c in all_cols if columns is None or c in columns}
    return "\n".join(f"- {c}: {desc}" for c, (grain, agg, desc) in cols.items())


def render_schema_doc(table: str, columns: Optional[set] = None) -> str:
    """Renders the schema block for ONE table — only the tables in
    intent.candidate_tables get rendered into the prompt.

    STRUCTURAL FACTS ONLY. This function must never emit an instruction
    ("dedup before aggregating", "use LOWER() for this column", etc) — every
    instruction for this table already lives, stated exactly once, in
    BUSINESS_RULES and is retrieved alongside this doc for the same prompt.
    Previously this function also rendered separate invoice/item column-name
    lists plus a full "CRITICAL — never SUM directly..." paragraph with
    example SQL — both were verbatim duplicates of pps_invoice_dedup, which
    is mandatory=True and therefore already injected into every prompt for
    this table. Removed; the "*" marker below is the only pointer needed."""
    meta = TABLE_REGISTRY.get(table)
    if not meta:
        return f"(No registry entry for table '{table}')"
    all_cols = meta["columns"]
    # columns=None (the default, and every current caller) -> full column
    # set, byte-for-byte the same output as before this parameter existed.
    cols = {c: all_cols[c] for c in all_cols if columns is None or c in columns}
    has_agg = bool(invoice_level_agg_columns(table) & set(cols))

    lines = [f"Table: {table}", meta["grain_description"], "", "Columns:"]
    for col, (grain, agg, desc) in cols.items():
        marker = " *" if agg else ""
        lines.append(f"  - {col} ({grain}{marker}): {desc}")
    if has_agg:
        lines.append("")
        lines.append("* = invoice-level, aggregatable — see business rules for the required dedup pattern.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# BUSINESS RULES — mandatory=True rules are ALWAYS injected for their
# table(s) regardless of keyword match (see rule_retrieval.py). These are
# the rules with a matching code-level validator — the prompt text and the
# AST check are two views of the same guarantee.
#
# Each rule below is the SINGLE source of its instruction — nothing else
# in the registry (schema doc, column descriptions) restates it. If you're
# editing a rule's wording, that edit is the only place it needs to happen.
# ─────────────────────────────────────────────────────────────────────────
BUSINESS_RULES: List[BusinessRule] = [
    BusinessRule(
        id="pps_invoice_dedup",
        rule_name="invoice_level_deduplication",
        trigger_condition="Any SUM/AVG/MIN/MAX on an invoice-level aggregatable column in pet_pooja_sales.",
        rule_text=(
            # Compact one-liner (was a multi-line commented block) but keeps
            # the literal SQL shape inline — the model needs the exact
            # subquery pattern, not just a prose description of "dedup
            # first", or cheaper/smaller models tend to reconstruct it
            # wrong and cost you a validator-triggered repair retry, which
            # spends more tokens than this line ever saved. Generalized
            # beyond "total" specifically (any invoice-level aggregatable
            # column, e.g. my_amount/delivery_charge/etc), since the
            # compact schema render no longer carries a per-column marker
            # for which ones need this.


             "MANDATORY SALES DEDUPLICATION: In pet_pooja_sales, invoice-level "

            "amount fields such as total, my_amount, delivery_charge, "
            "container_charge, service_charge, additional_charge, etc. repeat "
            "on every item row of the same invoice. NEVER apply SUM, AVG, MIN, "
            "or MAX directly to these fields at the raw row level. "
            "First create an invoice-level dataset with exactly ONE ROW PER "
            "invoice_no using GROUP BY invoice_no and MAX() for the required "
            "invoice-level amount fields. Then perform the final aggregation "
            "on that invoice-level dataset. "
            "For example: "
            "`SELECT invoice_no, MAX(total) AS invoice_total "
            "FROM pet_pooja_sales WHERE <filters> GROUP BY invoice_no`. "
            "Then use `SUM(invoice_total)` or `AVG(invoice_total)` in the "
            "outer query. For invoice counts always use "
            "`COUNT(DISTINCT invoice_no)`, never `COUNT(*)`."

        ),
        applies_to_tables=["pet_pooja_sales"],
        mandatory=True,
        validator_function="validate_invoice_dedup_rule",
    ),
    BusinessRule(
        id="pps_business_day_cutoff",
        rule_name="business_day_cutoff",
        trigger_condition="Any date-bucketing (today/yesterday/week/month/date range) on pet_pooja_sales.date.",
        rule_text=(
            # Kept the literal expression inline for the same reason as
            # pps_invoice_dedup above — this is the other rule where a
            # cheap model reconstructing the syntax from prose alone is
            # the most likely place for a subtle, hard-to-notice error
            # (off-by-one on the interval direction, wrong function).
            f"Business day runs {BUSINESS_DAY_CUTOFF_HOUR}:00 AM–{BUSINESS_DAY_CUTOFF_HOUR}:00 AM "
            f"(not midnight). Bucket dates with `DATE(date - INTERVAL {BUSINESS_DAY_CUTOFF_HOUR} HOUR)`, "
            f"e.g. today = `DATE(date - INTERVAL {BUSINESS_DAY_CUTOFF_HOUR} HOUR) = CURDATE()` "
            f"(not `DATE(date) = CURDATE()`), unless the user explicitly wants calendar/clock time."
        ),
        applies_to_tables=["pet_pooja_sales"],
        mandatory=True,   # no AST validator today, so this MUST be in every prompt for this table
        validator_function=None,
    ),
    BusinessRule(
        id="pps_relative_dates",
        rule_name="relative_date_arithmetic",
        trigger_condition="Question uses a relative time phrase (past/last N days/weeks/months, this week, yesterday, this month, etc.) rather than explicit calendar dates.",
        rule_text=(
            "For RELATIVE date phrases (past N days, last N weeks, this month, yesterday, etc.), "
            "NEVER compute or hardcode literal calendar-date strings into the SQL. "
            "Always express the window relative to CURDATE()/NOW() using INTERVAL arithmetic so the query remains valid when re-run in the future. "
            "Use inclusive ranges with BETWEEN whenever possible. "
            "Examples: "
            "'past 8 days (including today)' = "
            "`DATE(date - INTERVAL 4 HOUR) BETWEEN CURDATE() - INTERVAL 7 DAY AND CURDATE()`. "
            "'past 30 days (including today)' = "
            "`DATE(date - INTERVAL 4 HOUR) BETWEEN CURDATE() - INTERVAL 29 DAY AND CURDATE()`. "
            "For a comparison period immediately preceding the current window, use a non-overlapping adjacent range. "
            "Example: if the current window is "
            "`BETWEEN CURDATE() - INTERVAL 7 DAY AND CURDATE()`, "
            "then the previous 4 weeks should be "
            "`BETWEEN CURDATE() - INTERVAL 35 DAY AND CURDATE() - INTERVAL 8 DAY`. "
            "Only use literal calendar dates when the user explicitly specifies a date or date range."
        ),
        applies_to_tables=["pet_pooja_sales"],
        mandatory=True,
        validator_function=None,
    ),
    BusinessRule(
        id="pps_default_status",
        rule_name="default_status_filter",
        trigger_condition="A sales-amount question (total sales, revenue, AOV, trend) with no status named.",
        rule_text=(
            f"Sales-amount questions with no status named default to `status = '{DEFAULT_SUCCESS_STATUS}'` "
            f"(verify against live values). Skip only if the user names a different status or explicitly "
            f"asks for all/cancelled orders. State this default in `assumptions`."
        ),
        applies_to_tables=["pet_pooja_sales"],
        mandatory=False,
        keywords=["sales", "revenue", "total", "amount", "aov", "average order value", "trend"],
    ),
    BusinessRule(
        id="pps_parcel_detection",
        rule_name="parcel_order_detection",
        trigger_condition="Question mentions parcel/packing orders.",
        rule_text=(
            "Parcel/packing orders are detected via "
            "`LOWER(item_name) LIKE LOWER('%packing charges%')`, never via order_type. "
            "Count with COUNT(DISTINCT invoice_no). Don't assume a business term maps to a categorical "
            "value not actually present in the live values — return the fallback instead of guessing."
        ),
        applies_to_tables=["pet_pooja_sales"],
        # Was mandatory=True — meaning this full rule (with example SQL) was
        # sent on EVERY prompt for this table regardless of the question,
        # even though it already had a `keywords` list sitting unused right
        # next to it. Flipped to keyword-gated: your rule_retrieval.py's
        # existing keyword-match path for non-mandatory rules now includes
        # this rule only when the question actually mentions parcel/packing.
        # validate_metric_logic (unchanged, code-level, table-agnostic) stays
        # as the backstop for the rare case a parcel-shaped question phrases
        # itself without either keyword.
        mandatory=False,
        validator_function="validate_metric_logic",
        keywords=["parcel", "packing"],
    ),
]


def rules_for_tables(tables: List[str]) -> List[BusinessRule]:
    return [r for r in BUSINESS_RULES if any(t in r.applies_to_tables for t in tables)]


def build_text_match_rules() -> List[BusinessRule]:
    """One mandatory rule per table that has fuzzy_match_columns — generated
    from the registry instead of hand-written, so adding/removing a column
    from fuzzy_match_columns automatically updates the prompt AND the
    validator (validate_text_match_pattern) stays the enforcement backstop.

    This is the ONLY place the LOWER()/LIKE instruction is written out —
    the schema doc's fuzzy-match columns and agents.py's fuzzy-match prompt
    section both just point here rather than restating the syntax."""
    rules = []
    for table, meta in TABLE_REGISTRY.items():
        cols = meta.get("fuzzy_match_columns", [])
        if not cols:
            continue
        rules.append(BusinessRule(
            id=f"{table}_text_match_pattern",
            rule_name="fuzzy_text_matching",
            trigger_condition=f"Any filter on {', '.join(cols)} in {table}.",
            rule_text=(
                f"Never use exact '=' on {', '.join(cols)} (real values carry prefix/casing variance) — "
                f"always `LOWER({cols[0]}) LIKE LOWER('%value%')` with a value from the fuzzy-match "
                f"candidates, not the user's raw text. If 2+ candidates for one mention are similarly "
                f"strong and it's unclear the user wants them combined, ask instead of guessing."
            ),
            applies_to_tables=[table],
            mandatory=True,
            validator_function="validate_text_match_pattern",
        ))
    return rules


# appended once at import time so retrieve_rules() picks these up automatically
BUSINESS_RULES.extend(build_text_match_rules())


# ─────────────────────────────────────────────────────────────────────────
# SQL PATTERNS — the lightweight substitute for a Query Planner. Named,
# parameterized templates retrieved by keyword match. See architecture
# review question D.
# ─────────────────────────────────────────────────────────────────────────
SQL_PATTERNS: List[SQLPattern] = [
SQLPattern(
        name="sales_total",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["total sales", "total revenue", "overall sales", "sales amount"],
        description="Total sales amount over a date range, invoice-deduplicated.",
        sql_template=(
            "SELECT SUM(total) AS total_sales FROM (\n"
            "  SELECT DISTINCT invoice_no, total FROM pet_pooja_sales\n"
            "  WHERE <date filter, business-day adjusted> AND status = '<default success status>'\n"
            ") AS invoices;"
        ),
        tags=["invoice_dedup", "business_day_cutoff", "default_status"],
    ),
    SQLPattern(
        name="sales_by_store",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["by store", "by restaurant", "by location", "per store", "each store"],
        description="Sales broken down by restaurant_name/store, invoice-deduplicated.",
        sql_template=(
            "SELECT restaurant_name, SUM(total) AS total_sales FROM (\n"
            "  SELECT DISTINCT invoice_no, restaurant_name, total FROM pet_pooja_sales\n"
            "  WHERE <date filter, business-day adjusted> AND status = '<default success status>'\n"
            ") AS invoices GROUP BY restaurant_name ORDER BY total_sales DESC;"
        ),
        tags=["invoice_dedup", "business_day_cutoff", "default_status"],
    ),
    SQLPattern(
        name="top_items",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["top selling", "best selling", "top items", "most sold", "popular items"],
        description="Top N items by quantity sold and revenue — item-level, no dedup needed.",
        sql_template=(
            "SELECT item_name, SUM(item_quantity) AS qty_sold, SUM(item_total) AS revenue\n"
            "FROM pet_pooja_sales\n"
            "WHERE <date filter, business-day adjusted> AND status = '<default success status>'\n"
            "GROUP BY item_name ORDER BY qty_sold DESC LIMIT <N>;"
        ),
        tags=["default_status"],
    ),
    SQLPattern(
        name="average_order_value",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["average order value", "aov", "average sale", "average bill"],
        description="Average invoice total — invoice-deduplicated before averaging.",
        sql_template=(
            "SELECT AVG(total) AS avg_order_value FROM (\n"
            "  SELECT DISTINCT invoice_no, total FROM pet_pooja_sales\n"
            "  WHERE <date filter, business-day adjusted> AND status = '<default success status>'\n"
            ") AS invoices;"
        ),
        tags=["invoice_dedup", "business_day_cutoff", "default_status"],
    ),
    SQLPattern(
        name="parcel_order_count",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["parcel orders", "packing orders", "how many parcel"],
        description="Count of orders with a packing-charges line item.",
        sql_template=(
            "SELECT COUNT(DISTINCT invoice_no) AS parcel_orders FROM pet_pooja_sales\n"
            "WHERE LOWER(item_name) LIKE LOWER('%packing charges%')\n"
            "  AND <date filter, business-day adjusted>;"
        ),
        tags=["parcel_detection", "business_day_cutoff"],
    ),
    SQLPattern(
        name="order_count_by_status",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["how many orders", "cancelled orders", "order count"],
        description="Count of distinct invoices matching a given status.",
        sql_template=(
            "SELECT COUNT(DISTINCT invoice_no) AS order_count FROM pet_pooja_sales\n"
            "WHERE status = '<status>' AND <date filter, business-day adjusted>;"
        ),
        tags=["business_day_cutoff"],
    ),
    SQLPattern(
        name="top_n_per_group",
        applies_to_tables=["pet_pooja_sales"],
        keywords=["last three", "last 3", "top 3", "top three", "latest", "most recent",
                   "per store", "for each store", "each location", "by store"],
        description=(
            "Ranked/limited subset PER GROUP (e.g. the last N invoices per store) — needs a "
            "window function, not a plain LIMIT, which only limits the whole result set once."
        ),
        sql_template=(
            "SELECT restaurant_name, invoice_no, date FROM (\n"
            "  SELECT restaurant_name, invoice_no, date,\n"
            "         ROW_NUMBER() OVER (PARTITION BY restaurant_name ORDER BY date DESC) AS rn\n"
            "  FROM pet_pooja_sales\n"
            "  WHERE <filters — e.g. LOWER(restaurant_name) LIKE LOWER('%store%') for each named "
            "store, OR'd together; DATE(date - INTERVAL 4 HOUR) = '<business_date>' if a date is named>\n"
            ") ranked\n"
            "WHERE rn <= <N>\n"
            "ORDER BY restaurant_name, rn;"
        ),
        tags=["window_function", "ranking"],
    ),
]


def retrieve_patterns(question: str, tables: List[str], top_k: int = 3) -> List[SQLPattern]:
    """Keyword-overlap scoring — NOT embeddings. At 6 patterns per table this
    is plenty; revisit with real retrieval once a table's pattern count
    exceeds ~15-20 (see module docstring)."""
    q = question.lower()
    scored = []
    for p in SQL_PATTERNS:
        if not any(t in p.applies_to_tables for t in tables):
            continue
        score = sum(1 for kw in p.keywords if kw in q)
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:top_k]]


# ─────────────────────────────────────────────────────────────────────────
# METRIC REGISTRY — structured definitions, NOT a retrieval corpus (per
# review question E: build the definitions now, defer embedding search
# until metric count grows past ~15).
# ─────────────────────────────────────────────────────────────────────────
METRIC_REGISTRY: Dict[str, MetricDefinition] = {
    "sales": MetricDefinition(
        metric_name="sales",
        applies_to_tables=["pet_pooja_sales"],
        description="Successful order revenue — invoice-level, must be deduplicated by invoice_no.",
        sql_fragment="SUM(total)",
        grain="invoice",
        requires_dedup=True,
        default_filters={"status": DEFAULT_SUCCESS_STATUS},
    ),
    "aov": MetricDefinition(
        metric_name="aov",
        applies_to_tables=["pet_pooja_sales"],
        description="Average order value — mean invoice total, not per item row.",
        sql_fragment="AVG(total)",
        grain="invoice",
        requires_dedup=True,
        default_filters={"status": DEFAULT_SUCCESS_STATUS},
    ),
    "parcel_orders": MetricDefinition(
        metric_name="parcel_orders",
        applies_to_tables=["pet_pooja_sales"],
        description="Orders containing a packing-charges line item.",
        sql_fragment="COUNT(DISTINCT invoice_no)",
        grain="invoice",
        requires_dedup=False,
        never_use_columns=["order_type"],
        detection_column="item_name",
        detection_pattern="packing charges",
    ),
    "top_items": MetricDefinition(
        metric_name="top_items",
        applies_to_tables=["pet_pooja_sales"],
        description="Best-selling items by quantity and revenue — item-level, no dedup needed.",
        sql_fragment="SUM(item_quantity), SUM(item_total)",
        grain="item",
        requires_dedup=False,
    ),
}


def metrics_for_tables(tables: List[str]) -> List[MetricDefinition]:
    return [m for m in METRIC_REGISTRY.values() if any(t in m.applies_to_tables for t in tables)]