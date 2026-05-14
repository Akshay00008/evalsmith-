# Databases (NLQ) & the Chat REPL

Two related additions:

1. **Connect a real DB** (SQLite, PostgreSQL, MySQL, Oracle, MSSQL) for **NLQ missions** so the framework optimizes against *execution equivalence* — i.e. "does the generated SQL produce the right rows?" — not just string match.
2. **Interactive chat REPL** (`genai chat`) so you can talk to a project's winning variant — chatbot, RAG, or NLQ — without going through the eval loop.

---

## Part 1 — Connecting a database (NLQ)

### 1.1 The mental model

The NLQ pipeline looks like:

```
       eval case input
   ("how many users signed up last week?")
                │
                ▼
   ┌──────────────────────┐
   │  System prompt with  │       lib/db.py:safe_execute()
   │  injected SCHEMA     │              ▲
   └──────────────────────┘              │
                │                        │
                ▼                        │
            LLM call ──► generated SQL ──┘
                                         │
                ┌────────────────────────┴──────────┐
                ▼                                   ▼
            execute against        execute the gold SQL
              the DB                 ('expected' field)
                │                                   │
                └─────────► compare result sets ────┘
                                       │
                                       ▼
                          execution_equivalence: 0.0–1.0
```

Two modes, picked via Mission.composition.eval_strategy:

| eval_strategy           | What gets scored                                                      | DB needed |
|-------------------------|----------------------------------------------------------------------|-----------|
| `tool_call_match`       | Normalized string match between generated SQL and gold SQL            | No        |
| `execution_equivalence` | Result sets of generated SQL vs. gold SQL (or pre-computed gold rows) | **Yes**   |

For real-world NLQ, **always prefer execution_equivalence**. Two queries can differ in surface form but be semantically identical (`COUNT(*)` vs `COUNT(id)`, different join orderings, alias renames). String match unfairly punishes the model for stylistic differences.

### 1.2 Supported databases

The framework uses **SQLAlchemy** as the abstraction. Any database with a SQLAlchemy dialect works. Driver packages are optional:

```bash
pip install 'sqlalchemy>=2.0'

# Plus the driver for your DB:
pip install 'psycopg[binary]'     # PostgreSQL
pip install pymysql                # MySQL / MariaDB
pip install oracledb               # Oracle (modern, replaces cx_Oracle)
pip install pyodbc                 # MS SQL Server (also needs ODBC Driver installed system-wide)
# SQLite needs nothing — stdlib.
```

Or grab the whole `db` extras bundle (excluding Oracle/MSSQL which depend on system libs):

```bash
pip install -e ".[db]"
```

### 1.3 Configure the connection — `data/db.json`

In your project workspace, create `<project>/data/db.json`:

```json
{
  "url": "postgresql+psycopg://readonly_user:password@db.example.com:5432/analytics",
  "query_timeout_ms": 5000,
  "max_rows": 1000,
  "allowed_tables": ["users", "events", "subscriptions"],
  "read_only": true
}
```

Connection URL examples by dialect:

| DB           | URL                                                                              |
|--------------|----------------------------------------------------------------------------------|
| SQLite       | `sqlite:///./projects/my_nlq/data/sample.db`                                     |
| PostgreSQL   | `postgresql+psycopg://user:pass@host:5432/dbname`                                |
| MySQL        | `mysql+pymysql://user:pass@host:3306/dbname`                                     |
| Oracle       | `oracle+oracledb://user:pass@host:1521/?service_name=XEPDB1`                     |
| MS SQL       | `mssql+pyodbc://user:pass@host/dbname?driver=ODBC+Driver+18+for+SQL+Server`      |

#### Safety rules baked in

`lib/db.safe_execute()` enforces these *before* the query reaches the DB:

| Guard                              | Why                                                                  |
|-----------------------------------|----------------------------------------------------------------------|
| **SELECT/WITH only** (when `read_only=true`) | Generated SQL is untrusted; no INSERT/UPDATE/DELETE/DDL.       |
| **Single statement**               | No `;` chaining → no stacked attacks.                                |
| **Blocked: `INTO OUTFILE`, `pg_sleep`, `BENCHMARK`, `PRAGMA write`** | Specific known abuse patterns.    |
| **`max_rows` cap**                 | Compare-only — no need for 10M-row result sets.                      |
| **`query_timeout_ms`**             | LLM-emitted joins can be cartesian; cap defensively.                 |

**Use a read-only DB role anyway.** App-level guards protect against accidents; DB-level perms protect against everything.

> **Credential hygiene:** `data/db.json` is **already in the per-project `.gitignore`** (we updated the template). Never commit it. If you collaborate, share via secret manager and document the URL shape in the project README.

### 1.4 Introspect the schema → seed the prompt

Once `data/db.json` exists, run:

```bash
python tools/introspect_db.py --project projects/my_nlq
```

This connects to the DB, lists every (allowed) table with columns + types + PK flags, samples 2 rows per table, and writes the schema dump to `projects/my_nlq/data/schema.txt`. Example output:

```
TABLE users:
  - id (INTEGER) [PK, NOT NULL]
  - email (VARCHAR(255)) [NOT NULL]
  - signup_date (DATE)
  - plan (VARCHAR(32))

TABLE events:
  - id (BIGINT) [PK, NOT NULL]
  - user_id (BIGINT) [NOT NULL]
  - event_name (VARCHAR(64))
  - occurred_at (TIMESTAMP)
```

`/plan` reads this when seeding the NLQ system prompt — so the Architect doesn't ask you to hand-write a schema description.

### 1.5 Write the eval set

For NLQ with execution equivalence, each `EvalCase.expected` is the **gold SQL string** the framework will execute alongside the generated SQL:

```jsonl
{"case_id": "nlq001", "input": "How many users signed up last week?", "expected": "SELECT COUNT(*) FROM users WHERE signup_date >= CURRENT_DATE - INTERVAL '7 days'", "tags": ["count","time_window"]}
{"case_id": "nlq002", "input": "Top 5 Pro plan users by event volume in October.", "expected": "SELECT u.email, COUNT(e.id) AS n FROM users u JOIN events e ON e.user_id=u.id WHERE u.plan='Pro' AND e.occurred_at >= '2024-10-01' AND e.occurred_at < '2024-11-01' GROUP BY u.email ORDER BY n DESC LIMIT 5", "tags": ["join","top_n","group_by"]}
```

If you'd rather pre-compute gold result sets (e.g. because the gold SQL is slow), pass `expected` as a dict:

```jsonl
{"case_id": "nlq003", "input": "Active subscriptions count.", "expected": {"columns": ["count"], "rows": [[1247]]}, "tags": ["count"]}
```

The execution_equivalence metric handles both shapes.

### 1.6 Run the pipeline

```bash
# In Claude Code:
/init my_nlq          # reads schema.txt for the E1 layer
/plan my_nlq          # use recipe nlq_sql; pick execution_equivalence
/run my_nlq           # iterates prompt / model / few-shots
```

The Strategist will try things like:
- Add more schema detail to the system prompt (`prompt_rewrite`)
- Add SQL examples for tricky joins (`few_shot_selection`)
- Upgrade Haiku → Sonnet for multi-join queries (`model_swap`)
- Switch to a structured tool-call output format (`tool_schema_edit`)

`FINAL.md` will tell you the winning configuration — the prompt + model that maximizes execution-equivalence within your cost/latency budget.

### 1.7 Iterate carefully

NLQ is one of the easier capabilities to overfit to the eval set. If your eval set has only `SELECT COUNT` questions, the optimized prompt may regress on `JOIN`s. Guard rails:

- **Diverse tag coverage**: tag every case with the SQL pattern it uses (`["count","join","group_by","window_function",...]`). The sketch's E3 slice perf layer will surface regressions on under-covered tags.
- **Hold out a slice for true OOD**: 10–20% of cases with tags not used in any other case. The Curator's `evidence_trial_ids` should include trials that did well on these.
- **Auditor's cost/latency checks**: a Sonnet-on-every-query winner with a $5/1k cost is rarely the right answer for production NLQ.

---

## Part 2 — Chat REPL

`genai chat <project>` opens an interactive session against a project's winning Variant.

### 2.1 Quick start

After `/run` terminates on any project:

```bash
genai chat my_pdfs
```

You'll see:

```
======================================================================
evalsmith chat · mission a4f8e92b1c5d7e90 (rag_qa)
variant 7a3f9c2d18b06e14 · model claude-haiku-4-5
Type /help for commands · /exit to quit
======================================================================

you> what's the refund window?

bot> Refunds are processed within 30 days of purchase. [doc_42]
   (retrieved: a4f8e92b, b3c9f1d2, c5e1a04f · 124+38 tok · $0.0003)
```

### 2.2 Behavior by mission type

| Capability         | What each turn does                                                              |
|--------------------|----------------------------------------------------------------------------------|
| `chatbot`          | Multi-turn conversation, full context carried. `/reset` clears memory.           |
| `rag_qa`           | Each turn: retrieve top-k chunks → build prompt → generate. No multi-turn memory.|
| `nlq_to_query`     | Generates SQL; if `data/db.json` exists, executes + renders result table.        |
| Other capabilities | Falls back to single-turn `model_call`.                                          |

### 2.3 Built-in commands

| Command     | Effect                                                                            |
|-------------|-----------------------------------------------------------------------------------|
| `/help`     | Show commands.                                                                    |
| `/variant`  | Dump the active Variant config (prompt, model, retrieval).                        |
| `/reset`    | (chatbot only) Clear conversation buffer.                                         |
| `/exit`     | Quit. Transcript saved to `results/chat_log_<ts>.jsonl` unless `--no-transcript`. |

### 2.4 Picking which variant

By default, the REPL picks (in this order):

1. **`results/winning_variant.json`** — the Curator's pinned winner (most reliable).
2. **Best trial in `experiment_log.jsonl`** by primary metric — falls back here when no pinned winner yet. We warn that the full prompt may not be reproducible.
3. **Domain seed Variant** — when there's no log either. Useful for pre-`/run` smoke testing.

Override with `--trial`:

```bash
genai chat my_pdfs --trial 7a3f9c2d18b06e14
```

### 2.5 Transcripts

Every session is logged to `<project>/results/chat_log_<unix_ts>.jsonl`:

```jsonl
{"turn": 0, "user": "what's the refund window?", "assistant": "Refunds are processed within 30 days..."}
{"turn": 1, "user": "what about digital products?", "assistant": "Digital products are non-refundable..."}
```

These transcripts are themselves useful **eval-set growth fodder** — if you find the bot answering badly on a real question, copy the case into `data/eval_set.jsonl` and re-run optimization.

### 2.6 NLQ chat example

With a SQLite DB configured at `data/db.json`:

```bash
genai chat my_nlq
```

```
you> how many users signed up this month?

bot> SQL:
SELECT COUNT(*) FROM users
WHERE signup_date >= date_trunc('month', current_date)

count
-----
1247
(124+22 tok · $0.0002)
```

If the model produces invalid SQL the REPL shows the safety error:

```
bot> SQL:
DROP TABLE users
[execution failed: forbidden] only SELECT/WITH allowed; got 'drop'
```

### 2.7 Using chat to grow your eval set

A productive workflow loop:

```
genai chat my_project   →  notice a wrong answer
                       │
                       │  add the question + gold answer to
                       │  data/eval_set.jsonl
                       ▼
/run my_project         →  framework re-optimizes against the
                            expanded eval set
                       │
                       ▼
genai chat my_project   →  verify the regression is fixed
```

This is how the eval set should grow — driven by real interactions, not synthetic generation.

---

## End-to-end NLQ recipe (with SQLite, no external DB needed)

A complete worked example you can run locally. Creates a tiny SQLite DB, configures NLQ, and runs the pipeline.

### Step 1: scaffold + create the DB

```bash
genai new-project sqlite_nlq --recipe nlq_sql
```

Build a tiny SQLite DB:

```bash
python -c "
import sqlite3
db = sqlite3.connect('projects/sqlite_nlq/data/sample.db')
db.executescript('''
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, plan TEXT, signup_date DATE);
INSERT INTO users VALUES (1, 'alice@example.com', 'Pro', '2024-09-15');
INSERT INTO users VALUES (2, 'bob@example.com', 'Free', '2024-10-01');
INSERT INTO users VALUES (3, 'carol@example.com', 'Pro', '2024-10-12');
INSERT INTO users VALUES (4, 'dave@example.com', 'Team', '2024-11-02');

CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, event_name TEXT, occurred_at TIMESTAMP);
INSERT INTO events VALUES (1, 1, 'login', '2024-10-15 09:00');
INSERT INTO events VALUES (2, 1, 'purchase', '2024-10-15 10:30');
INSERT INTO events VALUES (3, 3, 'login', '2024-10-16 11:00');
INSERT INTO events VALUES (4, 1, 'login', '2024-10-17 09:00');
''')
db.commit()
print('SQLite DB created.')
"
```

### Step 2: configure db.json

```bash
cat > projects/sqlite_nlq/data/db.json <<'JSON'
{
  "url": "sqlite:///./projects/sqlite_nlq/data/sample.db",
  "read_only": true,
  "query_timeout_ms": 3000,
  "max_rows": 100
}
JSON
```

### Step 3: introspect

```bash
python tools/introspect_db.py --project projects/sqlite_nlq
```

You should see:

```
Connecting to sqlite:///./projects/sqlite_nlq/data/sample.db ...
Found 2 table(s):

TABLE users:
  - id (INTEGER) [PK]
  - email (TEXT)
  - plan (TEXT)
  - signup_date (DATE)

TABLE events:
  - id (INTEGER) [PK]
  - user_id (INTEGER)
  - event_name (TEXT)
  - occurred_at (TIMESTAMP)

--- sample rows ---
users (showing 2 of up to 2):
  columns: ['id', 'email', 'plan', 'signup_date']
    (1, 'alice@example.com', 'Pro', '2024-09-15')
    (2, 'bob@example.com', 'Free', '2024-10-01')
...
```

### Step 4: write 20+ eval cases

`projects/sqlite_nlq/data/eval_set.jsonl`:

```jsonl
{"case_id": "n01", "input": "How many users total?", "expected": "SELECT COUNT(*) FROM users", "tags": ["count","simple"]}
{"case_id": "n02", "input": "How many Pro plan users?", "expected": "SELECT COUNT(*) FROM users WHERE plan='Pro'", "tags": ["count","filter"]}
{"case_id": "n03", "input": "List all emails.", "expected": "SELECT email FROM users", "tags": ["select","simple"]}
{"case_id": "n04", "input": "Most recent signup.", "expected": "SELECT email, signup_date FROM users ORDER BY signup_date DESC LIMIT 1", "tags": ["sort","limit"]}
{"case_id": "n05", "input": "Total events per user.", "expected": "SELECT user_id, COUNT(*) AS n FROM events GROUP BY user_id", "tags": ["group_by","count"]}
... (expand to 20+)
```

### Step 5: run

```
/init sqlite_nlq
/plan sqlite_nlq   # pick execution_equivalence as eval_strategy
/run sqlite_nlq
```

### Step 6: chat with the winner

```bash
genai chat sqlite_nlq
```

```
you> show me Pro users who logged in in October

bot> SQL:
SELECT DISTINCT u.email
FROM users u
JOIN events e ON e.user_id = u.id
WHERE u.plan='Pro'
  AND e.event_name='login'
  AND strftime('%Y-%m', e.occurred_at) = '2024-10'

email
-----
alice@example.com
carol@example.com
```

---

## Troubleshooting

### `SQLAlchemy not installed`
`pip install 'sqlalchemy>=2.0'`. Add a driver too — see section 1.2.

### Connection works but introspection returns 0 tables
Probably a schema/permissions issue. Check that the connection user has access to the tables. For PostgreSQL specifically, the `search_path` may exclude your schema — set it in the URL: `?options=-csearch_path=myschema,public`.

### `forbidden: only SELECT/WITH allowed`
The model emitted DDL/DML. This is the safety guard doing its job. The `syntactic_validity` metric will catch this and the Strategist will iterate.

### `query exceeded 5000ms`
Increase `query_timeout_ms` in `db.json`, OR (better) flag the case for review — most NLQ queries on a small eval DB shouldn't take >1s. A slow query is usually a sign of a bad join the LLM produced.

### Chat REPL says "Using seed variant" with a warning
The Curator hasn't pinned `results/winning_variant.json` yet (older runs may lack this artifact). For a faithful winner reproduction, either re-run `/run` (the current Curator pins it) or pass `--trial <id>` explicitly.

### Chat hangs after first message
You don't have an API key set, and you're using a real-API capability. Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, or accept that stub mode produces synthetic answers.

---

For more, see [WALKTHROUGH.md](WALKTHROUGH.md) (general pipeline) and [PDF_RAG_GUIDE.md](PDF_RAG_GUIDE.md) (RAG over PDFs).
