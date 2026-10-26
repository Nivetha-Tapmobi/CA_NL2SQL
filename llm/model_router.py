"""
app_nl — model_router.py
==========================
Deterministic tier -> cascade lookup. No LLM calls.
"""
from typing import List
from core.models import Complexity
from config import MODEL_ROUTING_TABLE


def build_sql_model_cascade(complexity: Complexity, manual_override: str = None) -> List[str]:
    """Returns an ordered list of model strings to try. If manual_override
    is given (e.g. a sidebar dropdown pick for debugging), it goes first;
    the tier's normal cascade follows, deduplicated."""
    tier = MODEL_ROUTING_TABLE[complexity.value]
    primary = manual_override or tier["primary"]
    seen = {primary}
    cascade = [primary]
    for m in ([tier["primary"]] if manual_override else []) + tier["fallback"]:
        if m not in seen:
            cascade.append(m)
            seen.add(m)
    return cascade
