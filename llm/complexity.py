"""
app_nl — complexity.py
========================
Deterministic Python rules over IntakeResult's intent flags. Zero LLM
calls, zero added latency. This is intentionally simple — see the
architecture review, question 6: complexity classification and model
routing should never be LLM calls.

FIX (this pass): needs_ranking alone conflated two very different SQL
shapes into the same MEDIUM tier — "top 5 items this week" (a plain
ORDER BY ... LIMIT) and "last 3 invoices PER STORE across 3 named stores"
(needs a window function, PARTITION BY, AND multi-entity fuzzy resolution).
The second is COMPLEX-tier difficulty, not MEDIUM, and routing it to a
MEDIUM-tier model is exactly what produced a generation failure (the model
couldn't produce valid structured SQL and fell back to a plain-English
refusal instead).

Doing this properly means adding a real field to IntakeResult (e.g.
ranking_is_grouped: bool, set by the intake LLM) — not done here, since
models.py wasn't available when this fix was made; adding a new field to a
Pydantic model I haven't seen risks breaking whatever validates it
elsewhere. Instead, this uses a heuristic over fields IntakeResult already
exposes: needs_ranking=True combined with a required_dimension that names
something store/location-shaped is a reasonable proxy for "the ranking has
to happen within each group, not once over the whole result" — imperfect
(it's matching free-text dimension strings, which the intake LLM extracts
loosely), but strictly better than treating every needs_ranking=True
question identically. If IntakeResult gains a real ranking_is_grouped
field later, replace _implies_per_group_ranking's body with a direct read
of that field and delete this heuristic.
"""
from core.models import IntakeResult, Complexity

# Dimension words that suggest the ranking needs to happen WITHIN each named
# group (store/location/etc), not once over the whole result set — that's
# the structural line between a plain ORDER BY ... LIMIT and a window
# function (PARTITION BY ... ROW_NUMBER()). Not exhaustive; extend as you
# see more grouping-shaped dimension words come through in practice.
_GROUPING_DIMENSION_KEYWORDS = {"store", "location", "branch", "outlet", "restaurant", "area"}


def _implies_per_group_ranking(intake: IntakeResult) -> bool:
    if not intake.needs_ranking:
        return False
    dims_text = " ".join(d.lower() for d in intake.required_dimensions)
    return any(kw in dims_text for kw in _GROUPING_DIMENSION_KEYWORDS)


def classify_complexity(intake: IntakeResult) -> Complexity:
    # COMPLEX: any join, any comparison, any time-series
    if intake.needs_join or intake.needs_comparison or intake.needs_time_series:
        return Complexity.COMPLEX
    if len(intake.candidate_tables) > 1:
        return Complexity.COMPLEX

    # COMPLEX: ranking that has to happen per-group (needs a window function),
    # not just once over the whole result — see module docstring. Checked
    # BEFORE the plain needs_ranking -> MEDIUM path below, since this is a
    # strictly harder subset of "needs ranking."
    if _implies_per_group_ranking(intake):
        return Complexity.COMPLEX

    # MEDIUM: ranking (whole-result, not per-group), or aggregation across
    # 2+ dimensions, or 2+ metrics
    if intake.needs_ranking:
        return Complexity.MEDIUM
    if intake.needs_aggregation and len(intake.required_dimensions) >= 2:
        return Complexity.MEDIUM
    if len(intake.required_metrics) >= 2:
        return Complexity.MEDIUM

    # SIMPLE: single metric, 0-1 dimension
    return Complexity.SIMPLE