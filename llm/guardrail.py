"""
app_nl — guardrail.py
========================
Layer 0/1 of the pipeline: canned responses (zero cost) and a free regex
injection guard, both BEFORE any LLM call. Same as your existing app.

FIX (this pass): the greeting ("hi") and identity ("who are you") canned
patterns were re.match-anchored to the WHOLE stripped string — meaning
"Hi, who are you?" matched NEITHER (it's not purely "hi", and it's not
purely "who are you") and fell all the way through to the intake LLM,
which correctly said "not a data question" but had nothing to point
candidate_tables at, landing on pipeline.py's generic "I couldn't tell
which part of the data this relates to" fallback instead of a friendly
identity reply.

The fix is NOT to switch to re.search — that would make "hi, show me
today's sales" (a real question with a pleasantry attached) get
short-circuited into a canned greeting reply instead of actually running.
Instead: strip any greeting/identity phrase out of the message, and only
treat it as canned if nothing but punctuation/filler is left over — i.e.
the message really is JUST a greeting/identity question, not a real
question that happens to start with one. "Thanks"/"ok"/etc. filler
patterns keep the original exact-match behavior unchanged — a message like
"great, but what about last week" has real content after "great" and
should never be swallowed as pure acknowledgement, so those stay strict.
"""
import re
from typing import List, Optional, Tuple

INJECTION_PATTERNS = [
    r"ignore (all |the )?(previous|above|prior) instructions",
    r"system prompt",
    r"you are now",
    r"act as (a|an)\b",
    r"jailbreak",
    r"disregard (all|the) rules",
    r"reveal your (instructions|prompt)",
    r"\bdrop\s+table\b",
    r"\bdelete\s+from\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\binsert\s+into\b",
    r"\btruncate\b",
    r"\balter\s+table\b",
    r"--\s*$",
    r";\s*(drop|delete|update|insert|truncate|alter)\b",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


def looks_like_injection(question: str) -> bool:
    return bool(_INJECTION_RE.search(question))


# ── Exact-match filler acknowledgements — unchanged behavior on purpose.
# These stay strict: "great, but what about last week" has real content
# after "great" and must NOT be swallowed as pure thanks/acknowledgement.
CANNED_RESPONSES: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"^(thanks?( you)?( so much| a lot| a ton| very much)?|thank\s*you( so much| a lot)?"
            r"|thx|ty|appreciate it|great|nice|cool|awesome|perfect|ok(ay)?)[\s!.,]*$",
            re.IGNORECASE,
        ),
        "You're welcome! Let me know if you need anything else.",
    ),
]

# ── Greeting / identity — handled via residual-content check below, NOT a
# whole-string anchor, so compound phrasing ("Hi, who are you?", "hey, what
# can you do?") is caught. IDENTITY checked before GREETING when a message
# contains both, since "who are you" is the more substantive part of a
# combined message and should get the fuller identity reply, not just "hi!".
GREETING_RE = re.compile(r"\b(hi+|hello+|hey+|yo|greetings|good\s*(morning|afternoon|evening))\b", re.IGNORECASE)
IDENTITY_RE = re.compile(r"\b(who are you|what are you|what can you do|what do you do|help)\b", re.IGNORECASE)

GREETING_REPLY = (
    "Hi! I'm a sales analytics assistant — ask me about sales, revenue, orders, "
    "store performance, or trends over a date range."
)
IDENTITY_REPLY = (
    "I'm a sales analytics assistant — happy to help with sales, revenue, orders, "
    "store performance, and related business data questions."
)

# Connector/filler words ignored when checking whether anything real is left
# over after stripping a greeting/identity phrase — "Hi, who are you?" should
# reduce to nothing after removing both phrases and this punctuation/filler.
_RESIDUAL_FILLER_WORDS = {"and", "there", "please", "so", "just", "um", "uh"}


def _pure_greeting_or_identity_reply(stripped: str) -> Optional[str]:
    residual = IDENTITY_RE.sub(" ", stripped)
    has_identity = residual != stripped
    residual2 = GREETING_RE.sub(" ", residual)
    has_greeting = residual2 != residual

    residual_words = [w for w in re.findall(r"[a-zA-Z]+", residual2.lower())
                       if w not in _RESIDUAL_FILLER_WORDS]
    if residual_words:
        return None  # real content left over — not pure chit-chat, let it flow to the real pipeline

    if has_identity:
        return IDENTITY_REPLY
    if has_greeting:
        return GREETING_REPLY
    return None


def match_canned_response(question: str) -> Optional[str]:
    stripped = question.strip()

    # exact-match filler acknowledgements first (fast path, strict)
    for pattern, reply in CANNED_RESPONSES:
        if pattern.match(stripped):
            return reply

    # then the residual-content check for greeting/identity, exact or compound
    return _pure_greeting_or_identity_reply(stripped)