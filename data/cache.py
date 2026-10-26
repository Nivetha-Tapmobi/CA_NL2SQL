"""
app_nl — cache.py
===================
Exact-match SQLite query cache. Same design as your existing app: keyed on
normalized question + model + categorical-value fingerprint + prompt
version, TTL'd. Skips both LLM calls on a hit.
"""
import hashlib
import json
import sqlite3
import time
from typing import Dict, List, Optional

from config import CACHE_DB_PATH, CACHE_TTL_SECONDS, PROMPT_VERSION

_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS query_cache (
                cache_key TEXT PRIMARY KEY,
                question TEXT,
                model TEXT,
                payload TEXT,
                created_at REAL
            )"""
        )
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS request_log (
                session_id TEXT,
                ts REAL
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_session_ts ON request_log(session_id, ts)")
        _conn.commit()
    return _conn


def _cache_key(question: str, model: str, categorical_ctx: Dict[str, List[str]]) -> str:
    normalized_q = " ".join(question.strip().lower().split())
    ctx_fingerprint = hashlib.sha256(json.dumps(categorical_ctx, sort_keys=True).encode()).hexdigest()[:16]
    raw = f"{normalized_q}|{model}|{ctx_fingerprint}|{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(question: str, model: str, categorical_ctx: Dict[str, List[str]]) -> Optional[dict]:
    conn = get_conn()
    key = _cache_key(question, model, categorical_ctx)
    row = conn.execute("SELECT payload, created_at FROM query_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    payload, created_at = row
    if time.time() - created_at > CACHE_TTL_SECONDS:
        conn.execute("DELETE FROM query_cache WHERE cache_key = ?", (key,))
        conn.commit()
        return None
    return json.loads(payload)


def cache_set(question: str, model: str, categorical_ctx: Dict[str, List[str]], payload: dict) -> None:
    conn = get_conn()
    key = _cache_key(question, model, categorical_ctx)
    conn.execute(
        "INSERT OR REPLACE INTO query_cache (cache_key, question, model, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (key, question, model, json.dumps(payload), time.time()),
    )
    conn.commit()


def check_rate_limit(session_id: str, per_minute: int, per_hour: int, per_day: int) -> Optional[str]:
    conn = get_conn()
    now = time.time()
    conn.execute("INSERT INTO request_log (session_id, ts) VALUES (?, ?)", (session_id, now))
    conn.execute("DELETE FROM request_log WHERE session_id = ? AND ts < ?", (session_id, now - 86400))
    conn.commit()

    counts = {}
    for label, window in (("minute", 60), ("hour", 3600), ("day", 86400)):
        row = conn.execute(
            "SELECT COUNT(*) FROM request_log WHERE session_id = ? AND ts >= ?", (session_id, now - window)
        ).fetchone()
        counts[label] = row[0] if row else 0

    if counts["minute"] > per_minute:
        return f"more than {per_minute} requests in the last minute"
    if counts["hour"] > per_hour:
        return f"more than {per_hour} requests in the last hour"
    if counts["day"] > per_day:
        return f"more than {per_day} requests in the last day"
    return None
