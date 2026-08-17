# Petpooja NL-to-SQL — Query Generator

**Architecture & Technical Documentation**

---

## 1. Overview

This project is a **natural-language-to-SQL (NL2SQL)** system for Petpooja restaurant sales data. Users ask business questions in plain English; the system rewrites the question, retrieves schema/context, generates a safe MySQL `SELECT`, validates it against business rules, optionally executes it, and returns results.

**Primary stages covered** (per the architecture diagram):

```
User → Intent/Rewrite → Schema Retrieval → Business Glossary → Few-Shot
     → SQL Generation LLM → SQL Validator
```

Execution, result explanation, and dashboard stages are intentionally minimal (a “Run” button) so the generator can be tested end-to-end. They will be hardened later.

**Orchestration** is implemented as a **LangGraph `StateGraph`** (not hand-rolled nested loops). The public entry point `run_pipeline()` keeps its original signature and return shape so the Streamlit UI is unchanged.

---

## 2. High-Level Architecture (Mermaid)

```mermaid
flowchart TB
    subgraph UI["Streamlit UI"]
        A[User Question] --> B[Generate SQL Button]
        B --> C{Rate Limit?}
        C -->|Blocked| D[Warning]
        C -->|OK| E[run_pipeline]
        E --> F[Show Rewrite / Clarification / SQL]
        F --> G[Run Query Button]
        G --> H[execute_sql_with_recovery]
        H --> I[DataFrame + History]
    end

    subgraph Pipeline["LangGraph Pipeline"]
        E --> N1[canned_response]
        N1 --> N2[regex_guard]
        N2 --> N3[cache_lookup]
        N3 --> N4[intake]
        N4 --> N5[entity_disambiguation]
        N5 --> N6[item_matching]
        N6 --> N7[sql_generation]
        N7 --> N8[sql_validation]
        N8 --> N9[advance_cascade]
        N9 --> N7
        N8 --> N10[cache_store]
        N10 --> END([END])
    end

    style UI fill:#e8f4f8
    style Pipeline fill:#f0f7e8
```

---

## 3. LangGraph Node Flow (Detailed Mermaid)

```mermaid
flowchart TD
    START([START]) --> Canned[canned_response]

    Canned --> Regex[regex_guard]

    Regex -->|canned / blocked_regex| END1([END])
    Regex -->|continue| Cache[cache_lookup]

    Cache -->|cache hit| END2([END])
    Cache -->|miss / fresh| Intake[intake]

    Intake -->|blocked_llm_guardrail| END3([END])
    Intake -->|not answerable / clarification| END4([END])
    Intake -->|OK| Entity[entity_disambiguation]

    Entity -->|ambiguous location| END5([END])
    Entity -->|OK| Item[item_matching]

    Item --> Gen[sql_generation]
    Gen --> Val[sql_validation]

    Val -->|validated_sql present| Store[cache_store]
    Store --> END6([END])

    Val -->|validation failed| Check{attempt_count ≥ MAX_SQL_ATTEMPTS?}
    Check -->|yes| END7([END])
    Check -->|no| Adv[advance_cascade]
    Adv --> Gen

    style Canned fill:#ffeaa7
    style Regex fill:#ff7675
    style Cache fill:#74b9ff
    style Intake fill:#a29bfe
    style Entity fill:#fd79a8
    style Item fill:#55efc4
    style Gen fill:#00b894
    style Val fill:#e17055
    style Adv fill:#fdcb6e
    style Store fill:#6c5ce7
```

---

## 4. Agents Used

The system uses **pydantic-ai** `Agent` instances. There are two primary agent builders:

| Agent | Builder | Purpose | Output Model | Cascade |
|-------|---------|---------|--------------|---------|
| **Intake Agent** | `build_intake_agent(model)` | Combined security (jailbreak detection) + rewrite + answerability triage | `IntakeResult` | `INTAKE_MODEL_CASCADE` (always Groq-first, cheap/fast) |
| **SQL Generation Agent** | `build_sql_agent(model, categorical_ctx, item_name_matches)` | Produce a single read-only MySQL SELECT + explanation + assumptions | `SQLResult` | User-selected primary model → `SQL_FALLBACK_CASCADE` |

### 4.1 Intake Agent Details

- **Jobs in one pass**:
  1. **Security** — flag `is_suspicious` for instruction overrides, prompt leaks, role-play, SQL injection attempts, authority claims, etc.
  2. **Rewrite & Triage** — fix spelling only, decide if the single table can answer the question, optionally ask for clarification.
- Always runs on the **INTAKE_MODEL_CASCADE** regardless of the sidebar model choice.
- Supports conversational follow-ups via `message_history` (pydantic-ai).

### 4.2 SQL Generation Agent Details

- Receives:
  - Full schema documentation (`SCHEMA_DOC`)
  - Domain glossary
  - Live categorical column values
  - Fuzzy item-name matches
  - Few-shot examples
- Enforced rules (business day cutoff, default status filter, parcel-order detection via item_name, invoice-level deduplication, etc.).
- On validation failure the same agent is re-invoked with the error appended to the prompt (fixer loop). After two failures on a model, the cascade advances.

### 4.3 Model Cascades

```python
INTAKE_MODEL_CASCADE = [
    "groq:openai/gpt-oss-20b",
    "groq:qwen/qwen3.6-27b",
    "groq:openai/gpt-oss-120b",
]

SQL_FALLBACK_CASCADE = [
    "google:gemini-2.5-flash",
    "google:gemini-2.0-flash",
    "groq:openai/gpt-oss-120b",
    "groq:qwen/qwen3.6-27b",
]

MAX_SQL_ATTEMPTS = 2 + len(SQL_FALLBACK_CASCADE)  # primary + 1 retry + fallbacks
```

`build_sql_model_cascade(primary_model)` puts the user-selected model first, then appends unique fallbacks.

---

## 5. LangGraph Nodes Reference

| Node | Type | Responsibility | Early-exit conditions |
|------|------|----------------|-----------------------|
| `canned_response` | sync | Match greeting / thanks / “who are you” patterns → friendly canned reply | Sets `source="canned"` |
| `regex_guard` | sync | Regex injection patterns (DROP, IGNORE PREVIOUS, etc.) | Sets `source="blocked_regex"` |
| `cache_lookup` | sync | Exact-match SQLite cache (question + model + categorical fingerprint + prompt version) with TTL | Sets `source="cache"` on hit |
| `intake` | async | LLM intake agent (security + rewrite + triage) | Sets `source="blocked_llm_guardrail"` or clarification |
| `entity_disambiguation` | sync | Detect ambiguous restaurant/location mentions | Clarification if multiple stores match a phrase |
| `item_matching` | sync | Fuzzy match question tokens against live `item_name` values (+ synonym groups) | Supplies matches to SQL agent |
| `sql_generation` | async | LLM SQL agent; feeds previous validation error on retries | Technical failures force immediate escalation |
| `sql_validation` | sync | `sqlglot` parse + column existence + invoice-dedup rule + metric-logic rules | Increments `tries_on_current_model` on failure |
| `advance_cascade` | sync | After 2 tries on current model, move to next model and reset try counter | — |
| `cache_store` | sync | Persist successful validated result | Only when `source=="fresh"` and validated |

**Routing functions**:

- `route_after_regex_guard`
- `route_after_cache_lookup`
- `route_after_intake`
- `route_after_disambiguation`
- `route_after_validation`

**State**: `AgentState` (TypedDict) carries question, rewrite verdicts, categorical context, item matches, SQL, validation error, cascade position, attempt counts, error history (appended via `operator.add`), source, models used, and pydantic message history.

**Checkpointer**: `MemorySaver` keyed by `session_id` / `thread_id`.

---

## 6. Pipeline Sequence (Sequence Diagram)

```mermaid
sequenceDiagram
    participant U as User
    participant S as Streamlit
    participant G as LangGraph
    participant I as Intake Agent
    participant SQL as SQL Agent
    participant V as Validator
    participant C as Cache
    participant DB as MySQL

    U->>S: Type question + Generate
    S->>S: Rate-limit check
    S->>G: run_pipeline(question, model, …)

    G->>G: canned_response
    alt canned match
        G-->>S: clarification reply
    else
        G->>G: regex_guard
        alt injection
            G-->>S: blocked reply
        else
            G->>C: cache_lookup
            alt hit
                C-->>G: cached payload
                G-->>S: rewrite + SQL + validated
            else
                G->>I: intake (cascade)
                I-->>G: IntakeResult
                alt suspicious / not answerable
                    G-->>S: blocked or clarification
                else
                    G->>G: entity_disambiguation
                    alt ambiguous
                        G-->>S: clarification
                    else
                        G->>G: item_matching
                        loop until validated or max attempts
                            G->>SQL: sql_generation
                            SQL-->>G: SQLResult
                            G->>V: sql_validation
                            alt fail & retries left
                                G->>G: advance_cascade (maybe)
                            end
                        end
                        G->>C: cache_store (if success)
                        G-->>S: rewrite + SQL + validation
                    end
                end
            end
        end
    end

    opt User clicks Run
        S->>DB: execute_sql_with_recovery
        alt execution error
            S->>SQL: repair prompt
            SQL-->>S: repaired SQL
            S->>DB: retry
        end
        DB-->>S: DataFrame
        S-->>U: Table + history
    end
```

---

## 7. Key Business Rules Enforced

| Rule | Implementation |
|------|----------------|
| **Invoice-level aggregation** | Never `SUM(total)` directly on the item-grain table. Must `SELECT DISTINCT invoice_no, total` first. Validated by `validate_invoice_dedup_rule`. |
| **Business-day cutoff** | Default 04:00. `DATE(date - INTERVAL N HOUR)` for “today / yesterday / this week”. |
| **Default status filter** | Sales-amount questions without a named status → `status = 'Success'` (or live categorical value). |
| **Parcel / packing orders** | Detected via `item_name LIKE '%packing charges%'`, **never** via `order_type`. Validated by `validate_metric_logic`. |
| **Restaurant name matching** | Prefer `LOWER(restaurant_name) LIKE LOWER('%place%')` because of optional “Chaat Anna - ” prefix. |
| **Read-only only** | `sqlglot` rejects INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/MERGE/Command. Single statement only. |
| **Column whitelist** | Only columns present in `COLUMN_METADATA` / `KNOWN_COLUMNS`. |

---

## 8. Caching, Rate Limiting & Observability

- **Exact-match query cache** — SQLite (`nl2sql_cache.sqlite3`), key = normalized question + model + categorical fingerprint + `PROMPT_VERSION`. TTL = 6 h.
- **Rate limits** (per session_id) — configurable via env: 30/min, 300/h, 2000/day.
- **Logfire** — spans for every stage, metrics counters (cache hits/misses, guardrail blocks, validation failures, escalations, repairs, etc.). Optional Langfuse OTLP export.
- **Conversation memory** — pydantic-ai `message_history` for follow-ups (“what about last week instead”).

---

## 9. Environment Variables

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Gemini models (pydantic-ai auto-reads) |
| `GROQ_API_KEY` | Groq models |
| `LOGFIRE_TOKEN` | Optional cloud tracing |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional Langfuse |
| `EXEC_DB_URL` | Read-only execution DB (defaults to recon DB — warned) |
| `INTAKE_MODEL_CASCADE` | Comma-separated intake models |
| `SQL_FALLBACK_CASCADE` | Comma-separated SQL fallback models |
| `NL2SQL_CACHE_DB` | Cache SQLite path |
| `BUSINESS_DAY_CUTOFF_HOUR` | Default 4 |
| `DEFAULT_SUCCESS_STATUS` | Default “Success” |
| `PER_MINUTE_LIMIT` / `PER_HOUR_LIMIT` / `PER_DAY_LIMIT` | Rate limits |
| `APP_PASSWORD` | Optional Streamlit gate |
| `QUERY_ROW_SCAN_LIMIT`, `MAX_DATE_RANGE_DAYS`, `QUERY_MAX_EXECUTION_TIME_MS` | Cost guards |

---

## 10. Install & Run

```bash
pip install streamlit pydantic-ai-slim[google,groq] pandas sqlalchemy pymysql sqlglot logfire python-dotenv langgraph
```

Required secrets in `.env` (or environment):

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
# optional
LOGFIRE_TOKEN=...
EXEC_DB_URL=mysql+pymysql://readonly:...@host/db
```

```bash
streamlit run <this_file>.py
```

---

## 11. File Structure (Logical)

```
petpooja-nl2sql/
├── main application (single file in this delivery)
│   ├── CONFIG (models, cascades, glossary, schema doc, few-shots)
│   ├── LOGFIRE + metrics
│   ├── DB engine + categorical / item / restaurant fetchers
│   ├── Query cache (SQLite)
│   ├── Item fuzzy matching + location disambiguation
│   ├── Column metadata & business metrics
│   ├── pydantic models (IntakeResult, SQLResult, RewriteResult)
│   ├── Agent builders (intake + SQL)
│   ├── SQL validator (sqlglot + business rules)
│   ├── LangGraph StateGraph (10 nodes + routers)
│   ├── run_pipeline() entry point
│   ├── execute_sql_with_recovery()
│   ├── Rate limiting
│   └── Streamlit UI
├── nl2sql_cache.sqlite3          # runtime cache
└── .env
```

---

## 12. AgentState Fields (Summary)

| Group | Fields |
|-------|--------|
| Input / rewrite | `question`, `rewritten_question` |
| Intake verdicts | `is_suspicious`, `attack_type`, `is_answerable`, `clarification_needed` |
| Retrieval | `categorical_context`, `item_matches`, `ambiguous_locations` |
| Generation | `sql`, `validated_sql`, `validation_error`, `assumptions`, `explanation` |
| Cascade bookkeeping | `model_cascade`, `cascade_position`, `tries_on_current_model`, `attempt_count`, `error_history` |
| Tracing / control | `trace_id`, `request_id`, `outcome`, `source`, `intake_model_used`, `intake_attempts`, `final_model_used`, `pydantic_message_history` |

`source` values: `"canned"` | `"blocked_regex"` | `"blocked_llm_guardrail"` | `"cache"` | `"fresh"`.

---

## 13. SQL Execution Recovery (Separate from Graph)

`execute_sql_with_recovery` is **not** part of the LangGraph. It runs only when the user clicks “Run this query”:

1. Set `MAX_EXECUTION_TIME`.
2. `EXPLAIN` + cost heuristics (row scan, missing WHERE, date range).
3. Execute with safety `LIMIT`.
4. On DB error → repair prompt to the same SQL agent (up to `MAX_SQL_REPAIR_ATTEMPTS = 2`) → re-validate → retry.

---

## 14. Security Layers (Defense in Depth)

1. **Canned responses** — trivial non-business queries never reach an LLM.
2. **Regex guard** — classic injection / override phrases.
3. **LLM intake guardrail** — semantic jailbreak / prompt-leak / role-play detection.
4. **SQL validator** — structural (SELECT only, single table, known columns) + business-rule checks.
5. **Execution limits** — row caps, execution timeout, scan-row estimates.
6. **Rate limiting** — per-session windows.
7. **Optional app password**.

---

*Generated from the Petpooja NL-to-SQL codebase. All stage names, node names, agent builders, cascades, and business rules reflect the implementation as of the documented source.*
