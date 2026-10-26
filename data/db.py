"""
app_nl — db.py
=================
Execution-DB engine + live categorical value / item-name fetch helpers.
Same pattern as your existing app: cached, refreshed on a TTL, never
hardcoded — the SQL agent is always shown real current values.
"""
import re
import time
from functools import lru_cache
from typing import Dict, List, Tuple

from sqlalchemy import create_engine, text

from config import EXEC_DB_URL
import retrieval.registry as registry

_engine = None
_cache_store: Dict[str, Tuple[float, object]] = {}
_CACHE_TTL = 3600


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(EXEC_DB_URL, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def _ttl_cached(key: str, ttl: int, fn):
    now = time.time()
    if key in _cache_store and now - _cache_store[key][0] < ttl:
        return _cache_store[key][1]
    value = fn()
    _cache_store[key] = (now, value)
    return value


def clear_cache():
    _cache_store.clear()


def fetch_categorical_context(table: str) -> Dict[str, List[str]]:
    def _fetch():
        eng = get_engine()
        ctx: Dict[str, List[str]] = {}
        for col in registry.categorical_columns(table):
            try:
                with eng.connect() as conn:
                    rows = conn.execute(
                        text(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> '' LIMIT 50")
                    ).fetchall()
                ctx[col] = sorted({str(r[0]) for r in rows})
            except Exception:
                continue
        return ctx

    return _ttl_cached(f"catctx:{table}", _CACHE_TTL, _fetch)


def format_categorical_context(ctx: Dict[str, List[str]]) -> str:
    lines = []
    for col, vals in ctx.items():
        shown = ", ".join(vals[:30])
        more = f" (+{len(vals) - 30} more)" if len(vals) > 30 else ""
        lines.append(f"  - {col}: {shown}{more}")
    return "\n".join(lines) if lines else "  (no categorical columns registered for this table)"


def fetch_distinct_values(table: str, column: str) -> List[str]:
    """Generic version of the old fetch_item_names — works for ANY column,
    used for every column in registry.fuzzy_match_columns(table), not just
    item_name. This is what backs the fuzzy matching the SQL prompt is shown."""
    if column not in registry.known_columns(table):
        return []

    def _fetch():
        eng = get_engine()
        with eng.connect() as conn:
            rows = conn.execute(
                text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> '' LIMIT 1000")
            ).fetchall()
        return sorted({str(r[0]) for r in rows})

    return _ttl_cached(f"distinct:{table}:{column}", _CACHE_TTL, _fetch)


def fetch_item_names(table: str, column: str = "item_name") -> List[str]:
    # kept for backward compatibility — prefer fetch_distinct_values directly
    return fetch_distinct_values(table, column)


def fetch_all_fuzzy_match_values(table: str) -> Dict[str, List[str]]:
    """{column_name: [distinct live values]} for every column registry.py
    marks as fuzzy_match_columns for this table."""
    return {col: fetch_distinct_values(table, col) for col in registry.fuzzy_match_columns(table)}


ITEM_MATCH_STOPWORDS = {
    "total", "sales", "revenue", "today", "yesterday", "week", "month", "year",
    "store", "stores", "order", "orders", "item", "items", "sale", "price",
    "the", "for", "and", "with", "from", "what", "how", "much", "many",
    "show", "give", "get", "find", "last", "this", "all", "top", "best",
    "sold", "which", "who", "when", "where", "did", "was", "were", "are",
    "is", "of", "in", "on", "at", "to", "a", "an",
}


def find_fuzzy_matches(question: str, values: List[str], top_k: int = 8, cutoff: float = 0.55) -> List[Tuple[str, float]]:
    """Generic fuzzy match — works for item names, store names, categories,
    anything in registry.fuzzy_match_columns. 1-3 word spans of the question
    are difflib-matched against real live values (typos AND spelling/prefix
    variants both resolve to the actual stored value)."""
    import difflib
    if not values:
        return []
    words = re.findall(r"[a-zA-Z]+", question.lower())
    candidates = {w for w in words if w not in ITEM_MATCH_STOPWORDS}
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            span = words[i:i + n]
            if all(w in ITEM_MATCH_STOPWORDS for w in span):
                continue
            candidates.add(" ".join(span))

    values_lower = {v.lower(): v for v in values}
    scored: Dict[str, float] = {}
    for cand in candidates:
        for m in difflib.get_close_matches(cand, values_lower.keys(), n=3, cutoff=cutoff):
            score = difflib.SequenceMatcher(None, cand, m).ratio()
            original = values_lower[m]
            if original not in scored or score > scored[original]:
                scored[original] = score
        # substring containment catches prefix cases plain difflib often
        # misses at the default cutoff — e.g. "Ritchie Street" as a substring
        # of "Chaat Anna - Ritchie Street" — difflib's ratio can undershoot
        # cutoff on that pair since the strings differ a lot in length.
        for low, original in values_lower.items():
            if len(cand) > 3 and cand in low:
                score = max(scored.get(original, 0.0), 0.9)
                scored[original] = score
    return sorted(scored.items(), key=lambda x: -x[1])[:top_k]


# Backward-compat alias
find_item_name_matches = find_fuzzy_matches


def find_all_fuzzy_matches(question: str, table: str) -> Dict[str, List[Tuple[str, float]]]:
    """Runs find_fuzzy_matches for every fuzzy_match_column on this table."""
    result = {}
    for col, values in fetch_all_fuzzy_match_values(table).items():
        matches = find_fuzzy_matches(question, values)
        if matches:
            result[col] = matches
    return result


def find_ambiguous_matches(question: str, table: str, min_matches: int = 2) -> Dict[str, Dict[str, List[str]]]:
    """For each fuzzy_match_column, checks whether any phrase in the question
    would match 2+ (but not ALL) distinct real values via LIKE '%phrase%' —
    e.g. "Anna Nagar" matching both "Anna Nagar East" and "Anna Nagar West".
    That's a genuine ambiguity the pipeline should ask about BEFORE SQL
    generation, not silently aggregate across. Returns
    {column: {phrase: [matching_values]}}."""
    result: Dict[str, Dict[str, List[str]]] = {}
    for col, values in fetch_all_fuzzy_match_values(table).items():
        if len(values) < 2:
            continue
        words = re.findall(r"[a-zA-Z]+", question.lower())
        candidates = {w for w in words if len(w) > 3 and w not in ITEM_MATCH_STOPWORDS}
        for n in (2, 3, 4):
            for i in range(len(words) - n + 1):
                span = words[i:i + n]
                if all(w in ITEM_MATCH_STOPWORDS for w in span):
                    continue
                candidates.add(" ".join(span))

        values_lower = [(orig, orig.lower()) for orig in values]
        total = len(values)
        all_hits: Dict[str, List[str]] = {}
        for cand in candidates:
            matches = [orig for orig, low in values_lower if cand in low]
            if matches:
                all_hits[cand] = matches

        ambiguous: Dict[str, List[str]] = {}
        for cand, matches in all_hits.items():
            if not (min_matches <= len(matches) < total):
                continue
            # suppressed if a longer, more specific phrase already resolves to exactly one value
            resolved_by_longer = any(
                other != cand and cand in other and len(all_hits[other]) == 1 for other in all_hits
            )
            if not resolved_by_longer:
                ambiguous[cand] = matches
        if ambiguous:
            result[col] = ambiguous
    return result


def format_item_matches(matches: List[Tuple[str, float]]) -> str:
    if not matches:
        return "  (no values fuzzy-matched any word in this question)"
    return "\n".join(f'  - "{name}" (similarity {score:.0%})' for name, score in matches)


def format_fuzzy_matches_by_column(matches_by_col: Dict[str, List[Tuple[str, float]]]) -> str:
    if not matches_by_col:
        return "  (no columns had a fuzzy match against this question)"
    lines = []
    for col, matches in matches_by_col.items():
        lines.append(f"  {col}:")
        for name, score in matches:
            lines.append(f'    - "{name}" (similarity {score:.0%})')
    return "\n".join(lines)
