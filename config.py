"""
app_nl — config.py
==================
Central config. Mirrors the env-var-driven style of your existing
petpooja_nl2sql.py so this drops into the same deployment habits
(.env file, systemd EnvironmentFile, etc).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Execution DB (the actual business data — MySQL, same as today) ────────
RECON_DB = os.environ.get(
    "RECON_DB",
    "mysql+pymysql://tinysync:TinySyncdata%2301@localhost:3306/CA_reconciliation",
)
EXEC_DB_URL = os.environ.get("EXEC_DB_URL", RECON_DB)

# ── Registry DB — SQLite, deliberately separate from the MySQL business DB.
# This is the Phase-1 pragmatic choice from the architecture review: at a
# few tables, a lightweight local registry beats provisioning a new MySQL
# schema. Promote to MySQL (nl2sql_registry schema) once you're seeding it
# from a real admin workflow instead of a Python seed script.
REGISTRY_DB_PATH = os.environ.get("REGISTRY_DB_PATH", os.path.join(os.path.dirname(__file__), "registry.sqlite3"))

CACHE_DB_PATH = os.environ.get("NL2SQL_CACHE_DB", os.path.join(os.path.dirname(__file__), "nl2sql_cache.sqlite3"))
CACHE_TTL_SECONDS = 6 * 3600
MAX_HISTORY_ITEMS = 20
PROMPT_VERSION = "1"  # bump whenever registry seed content or prompt templates change materially

# ── Model tiers. Deterministic routing table (Stage: Model Router) ────────
# Kept intentionally small — 3 tiers, mixed providers per tier so a single
# provider outage doesn't stall a whole complexity class.
MODEL_ROUTING_TABLE = {
    "SIMPLE": {
        "primary": "groq:openai/gpt-oss-20b",
        # gemini-2.0-flash was shut down by Google on June 1, 2026 (confirmed
        # via search — every request that reached this fallback slot was
        # 404ing, burning a full round-trip before failing over further).
        # gemini-3.1-flash-lite is Google's own recommended migration
        # target for 2.0-flash — cheap, low-latency, right fit for this tier.
        "fallback": ["groq:qwen/qwen3.6-27b", "google:gemini-3.1-flash-lite"],
    },
    "MEDIUM": {
        "primary": "groq:qwen/qwen3.6-27b",
        "fallback": ["groq:openai/gpt-oss-120b", "google:gemini-2.5-flash"],
    },
    "COMPLEX": {
        "primary": "google:gemini-2.5-flash",
        # Same dead-model issue as SIMPLE's fallback above, but COMPLEX
        # wants a stronger fallback than a flash-lite — gemini-3.5-flash is
        # current (GA since May 2026) and pitched at near-Pro reasoning
        # quality at flash-tier cost/speed, a better fit for this tier.
        "fallback": ["groq:openai/gpt-oss-120b", "google:gemini-3.5-flash"],
    },
}

# Intake (guardrail + rewrite + intent, one combined call) always uses the
# cheapest/fastest tier regardless of routed SQL-gen model — same reasoning
# as your existing INTAKE_MODEL_CASCADE.
INTAKE_MODEL_CASCADE = [
    m.strip() for m in os.environ.get(
        "INTAKE_MODEL_CASCADE",
        "groq:openai/gpt-oss-20b,groq:qwen/qwen3.6-27b,groq:openai/gpt-oss-120b",
    ).split(",") if m.strip()
]

# AGENT_OUTPUT_RETRIES is pydantic-ai's OWN internal retry count for a
# single agent.run() call — if the model's raw output fails to parse into
# the Pydantic output_type, pydantic-ai silently retries that SAME call
# this many times, with zero visibility into (or coordination with) our
# own fixer loop below. That means the two retry budgets multiply, not
# add — this was set to 1 briefly, which turned out to be TOO aggressive:
# a model that needs a genuine one-off self-correction (e.g. it forgot to
# invoke the structured-output tool call on the first try) now hard-fails
# with "Exceeded maximum output retries (1)" instead of quietly succeeding
# on the second attempt. Settled on 2 as the middle ground: still far
# below the original 3 (worst case 6 x 2 = 12 calls instead of 18), but
# gives a model one real chance to correct itself before our own fixer
# loop (which retries with the ACTUAL validation error attached) takes
# over. If you still see "Exceeded maximum output retries" crashes after
# this, that specific model is systematically failing structured output,
# not just occasionally slipping — see the note below about checking
# which model it is via the AI Observability dashboard.
AGENT_OUTPUT_RETRIES = 2
MAX_SQL_ATTEMPTS_PER_MODEL = 2   # 1 primary + 1 retry-with-error, per model in cascade
MAX_SQL_ATTEMPTS_TOTAL = 6       # hard ceiling across the whole cascade, bounds worst-case cost

MAX_SQL_REPAIR_ATTEMPTS = 2      # post-execution repair loop, separate from pre-execution fixer loop

EXECUTION_ROW_CAP = 2000
QUERY_ROW_SCAN_LIMIT = int(os.environ.get("QUERY_ROW_SCAN_LIMIT", "500000"))
MAX_DATE_RANGE_DAYS = int(os.environ.get("MAX_DATE_RANGE_DAYS", "365"))
QUERY_MAX_EXECUTION_TIME_MS = int(os.environ.get("QUERY_MAX_EXECUTION_TIME_MS", "8000"))

PER_MINUTE_LIMIT = int(os.environ.get("PER_MINUTE_LIMIT", "30"))
PER_HOUR_LIMIT = int(os.environ.get("PER_HOUR_LIMIT", "300"))
PER_DAY_LIMIT = int(os.environ.get("PER_DAY_LIMIT", "2000"))

APP_PASSWORD = os.environ.get("APP_PASSWORD")

LOGFIRE_TOKEN = os.environ.get("LOGFIRE_TOKEN")

LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

LANGFUSE_PROJECT_ID = 'cmsrehlge01i3ad0i76jmo6hp'