# Petpooja NL-to-SQL Query Generator

## Overview

Petpooja NL-to-SQL Query Generator converts natural language business questions into validated SQL queries against the `pet_pooja_sales` table.

The application is designed for restaurant sales analytics and includes multiple production guardrails to improve reliability, security, observability, and query accuracy.

### Supported Questions

* Total sales
* Revenue trends
* Store performance
* Top-selling items
* Order counts
* Cancellation analysis
* Payment type analysis
* Area-wise performance
* Average order value
* Date-range comparisons

---

## Architecture

```mermaid
flowchart LR

    A[User Question]

    A --> B[Guardrail Classifier]
    B --> C[Intent Rewrite & Spell Correction]

    C --> D[Schema Retrieval]
    D --> E[Business Glossary]
    E --> F[Few-Shot Examples]
    F --> G[Fuzzy Item Matching]

    G --> H[SQL Generation LLM]

    H --> I[SQL Validator]

    I -->|Valid| J[Query Execution]
    I -->|Invalid| K[Fixer Loop]

    K --> H

    K -->|Still Invalid| L[Fallback Model]
    L --> I

    I --> M[Results]
```

---

## End-to-End Flow

```mermaid
sequenceDiagram

    participant User
    participant Guardrail
    participant Rewriter
    participant SQLAgent
    participant Validator
    participant Database

    User->>Guardrail: Natural Language Question

    Guardrail->>Rewriter: Safe Question

    Rewriter->>SQLAgent: Rewritten Question + Context

    SQLAgent->>Validator: Generated SQL

    Validator-->>SQLAgent: Validation Errors (if any)

    SQLAgent->>Validator: Corrected SQL

    Validator->>Database: Execute Valid Query

    Database-->>Validator: Results

    Validator-->>User: Data + Explanation
```

---

## Core Components

### 1. Guardrail Layer

Detects:

* Prompt injection attempts
* Jailbreak attempts
* System prompt extraction attempts
* Non-business questions
* Unsafe SQL requests

Examples:

* Ignore previous instructions
* Show system prompt
* Act as DBA
* Generate DELETE query

---

### 2. Query Rewriter

Responsibilities:

* Correct spelling mistakes
* Normalize user intent
* Detect ambiguity
* Reject unsupported requests
* Maintain conversation context

Example:

**Input**

```text
totl sales roypettah lst week
```

**Rewritten**

```text
total sales Royapettah last week
```

---

### 3. Business Glossary

Maps business language to actual schema columns.

| Business Term | Database Column |
| ------------- | --------------- |
| Store         | restaurant_name |
| Outlet        | restaurant_name |
| Branch        | restaurant_name |
| Location      | restaurant_name |
| Region        | area            |
| Zone          | area            |

Example:

```text
Sales in Royapettah
```

becomes

```sql
LOWER(restaurant_name)
LIKE LOWER('%Royapettah%')
```

---

### 4. Schema Awareness

The application understands:

### Invoice-Level Columns

Examples:

* total
* discount
* total_tax
* delivery_charge

These repeat across every item row.

### Item-Level Columns

Examples:

* item_name
* item_quantity
* item_total
* category_name

These are safe for direct aggregation.

---

### 5. Fuzzy Item Matching

Uses real menu items from the database.

Supports:

* Typo correction
* Synonym expansion
* Similar item detection

Example:

```text
dosaa sales
```

may resolve to

```text
Dosa
```

Example synonym group:

```text
curd
dahi
yogurt
yoghurt
```

---

### 6. SQL Generation

Uses:

* Gemini
* Groq
* PydanticAI Agents

Available models:

| Model              |
| ------------------ |
| Gemini 2.5 Flash   |
| Gemini 2.0 Flash   |
| Groq Llama 3.3 70B |

---

### 7. SQL Validation

Before execution every query is validated.

Checks include:

#### Allowed Operations

Only:

```sql
SELECT
```

Allowed.

#### Blocked Operations

```sql
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
MERGE
```

#### Schema Validation

Ensures:

* Columns exist
* Table exists
* Only approved schema is referenced

#### Invoice Dedup Validation

Prevents:

```sql
SELECT SUM(total)
FROM pet_pooja_sales
```

which would overcount invoices.

Forces:

```sql
SELECT SUM(total)
FROM (
    SELECT DISTINCT invoice_no, total
    FROM pet_pooja_sales
) invoices
```

---

## Business Rules

### Business Day Logic

Restaurant business day starts at:

```text
04:00 AM
```

A sale at:

```text
01:15 AM Aug 15
```

belongs to:

```text
Aug 14 Business Day
```

Implementation:

```sql
DATE(date - INTERVAL 4 HOUR)
```

---

### Default Status Filtering

Sales-related questions automatically apply:

```sql
status = 'Success'
```

unless user explicitly asks for:

* Cancelled orders
* All orders
* Specific statuses

---

## Query Cache

SQLite-backed cache.

Cache key includes:

* User question
* Selected model
* Prompt version
* Categorical context snapshot

Benefits:

* Faster responses
* Reduced LLM cost
* Reduced database load

---

## Model Cascade Strategy

```mermaid
flowchart TD

    A[Generate SQL]

    A --> B[Validation]

    B -->|Pass| C[Execute]

    B -->|Fail| D[Retry Same Model]

    D --> E[Validation]

    E -->|Pass| C

    E -->|Fail| F[Fallback Model]

    F --> G[Validation]

    G -->|Pass| C

    G -->|Fail| H[Return Error]
```

Maximum SQL-generation attempts:

```text
3
```

---

## Observability

### Logfire

Tracks:

* Cache hits/misses
* Validation results
* Model usage
* Query duration
* Execution statistics

### Langfuse

Tracks:

* Prompts
* Responses
* Token usage
* Cost estimation
* Trace analysis

---

## Environment Variables

Create a `.env` file.

```env
GEMINI_API_KEY=

GROQ_API_KEY=

LOGFIRE_TOKEN=

LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=

LANGFUSE_HOST=https://cloud.langfuse.com

EXEC_DB_URL=mysql+pymysql://readonly_user:password@localhost:3306/CA_reconciliation

REWRITER_MODEL=groq:llama-3.1-8b-instant

FALLBACK_SQL_MODEL=google:gemini-2.5-flash

BUSINESS_DAY_CUTOFF_HOUR=4
```

---

## Installation

```bash
pip install \
streamlit \
pydantic-ai-slim[google,groq] \
pandas \
sqlalchemy \
pymysql \
sqlglot \
logfire \
python-dotenv
```

---

## Running the Application

```bash
streamlit run app.py
```

---

## Production Safety Features

* Prompt Injection Detection
* Jailbreak Detection
* Read-Only SQL Enforcement
* Schema Validation
* Invoice Dedup Validation
* Query Cost Checks
* Query Scan Thresholds
* Model Escalation Strategy
* Exact Match Caching
* Conversation Memory
* Structured Output Validation

---

## High-Level System Diagram

```mermaid
flowchart TB

    User

    User --> Guardrail
    Guardrail --> Rewriter
    Rewriter --> Context

    subgraph Context
        Schema
        Glossary
        FewShot
        Categories
        ItemMatching
    end

    Context --> SQLAgent
    SQLAgent --> Validator
    Validator --> Database

    Database --> Results

    Results --> User

    SQLAgent --> Logfire
    SQLAgent --> Langfuse
```

---

## Key Benefits

✅ Secure NL-to-SQL Generation

✅ Production-Oriented Guardrails

✅ Business-Aware Querying

✅ Automatic Error Recovery

✅ Cost Optimization via Cache

✅ Full Observability with Logfire & Langfuse

✅ Restaurant-Specific Business Rules

✅ Schema-Aware Validation

Source: Architecture and implementation details extracted from the Petpooja NL-to-SQL Query Generator source code. 
