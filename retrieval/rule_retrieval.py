"""
app_nl — rule_retrieval.py
=============================
mandatory rules for the candidate tables ∪ keyword-relevant rules.
Mandatory rules are never dropped by relevance filtering — see the
architecture review: a rule with a code-level validator is a correctness
guarantee, not optional context.

FORMATTING NOTE (this pass): format_rules/format_patterns used to wrap each
item in a header line — "RULE — name (applies when: ...)" and
"Pattern: name — description" — before the actual content. That header was
pure overhead once registry.py's rule_text/description fields already say
what the rule is about in plain language; the trigger_condition/applies-when
framing is retrieval metadata, useful for debugging which rule fired and
why, not something the SQL-generation model needs restated in the prompt.
Both functions now emit plain lines with no header, and return "" (not a
placeholder string) when there's nothing to show — agents.py already
coalesces an empty rules_text/patterns_text to "(none retrieved)"/"None"
via `rules_text or "(none retrieved)"` / `patterns_text or "None"`, so the
empty-case wording lives in exactly one place instead of being duplicated
(and potentially drifting out of sync) between this file and agents.py.
"""
from typing import List
from core.models import BusinessRule
from retrieval.registry import BUSINESS_RULES


def retrieve_rules(question: str, tables: List[str]) -> List[BusinessRule]:
    q = question.lower()
    mandatory = [r for r in BUSINESS_RULES if r.mandatory and any(t in r.applies_to_tables for t in tables)]
    relevant = [
        r for r in BUSINESS_RULES
        if not r.mandatory
        and any(t in r.applies_to_tables for t in tables)
        and any(kw in q for kw in r.keywords)
    ]
    seen_ids = {r.id for r in mandatory}
    result = mandatory + [r for r in relevant if r.id not in seen_ids]
    return result


def format_rules(rules: List[BusinessRule]) -> str:
    """Plain '- rule_text' per line — no 'RULE — name (applies when: ...)'
    header. That header was retrieval metadata (useful in a Logfire span
    attribute, e.g. retrieval_span.set_attribute("rules_retrieved", ...) in
    pipeline.py, which already logs rule_name separately) — restating it
    inside the prompt text itself was pure duplication once rule_text was
    already compressed to a self-contained one-liner (see registry.py)."""
    if not rules:
        return ""
    return "\n".join(f"- {r.rule_text}" for r in rules)


def format_patterns(patterns) -> str:
    """Single best-match pattern, no 'Pattern: name — description' header.
    The dynamic context template has one {few_shot_example} slot, not a
    list — if retrieve_patterns ever returns more than one, only the top
    match (patterns[0], already the highest-scored per registry.py's
    retrieve_patterns) is shown; showing several here would silently
    reintroduce the "send everything, just in case" pattern this whole
    redesign is meant to avoid. sql_template's own inline comments already
    explain what to adapt (date filter, status default), so no separate
    description line is needed on top of it."""
    if not patterns:
        return ""
    return patterns[0].sql_template