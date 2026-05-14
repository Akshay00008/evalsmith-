# lib/db.py
# Database connector for NLQ missions. Single chokepoint for every SQL
# operation the framework performs — same role lib/registry.py plays for
# LLM calls.
#
# Three responsibilities:
#   1. Connect to a DB via SQLAlchemy URL (works for SQLite, PostgreSQL,
#      MySQL, Oracle, MSSQL — driver packages are optional extras).
#   2. Introspect schema → human-readable string the prompt can include.
#   3. Safely execute generated SQL with read-only guard + row cap +
#      timeout, then compare actual vs expected result sets.
#
# The safety guard is intentionally conservative: SELECT-only, single
# statement, no transactions. We never trust LLM-generated SQL to be
# benign — even with a read-only DB role, a runaway query can lock or DoS.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from pydantic import BaseModel, Field
import re


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class DBConfig(BaseModel):
    """Per-project DB connection settings. Saved at <project>/data/db.json
    by the user (or by `genai connect-db` — see tools/introspect_db.py).

    The framework never persists credentials in MISSION.json or any
    committed artifact. db.json itself is git-ignored at the project
    template level."""

    # SQLAlchemy connection URL. Examples:
    #   sqlite:///path/to/file.db
    #   postgresql+psycopg://user:pass@host:5432/dbname
    #   mysql+pymysql://user:pass@host/dbname
    #   oracle+oracledb://user:pass@host:1521/?service_name=XEPDB1
    #   mssql+pyodbc://user:pass@host/dbname?driver=ODBC+Driver+18+for+SQL+Server
    url: str

    # Hard timeout per query in milliseconds. Generated SQL with bad joins
    # can run for hours; cap defensively.
    query_timeout_ms: int = 5000

    # Maximum rows returned. Comparing 10M-row result sets is meaningless
    # for an LLM eval; cap.
    max_rows: int = 1000

    # Tables to expose to the LLM (and to introspect). Empty list = all.
    # Use this to scope the schema to a sub-domain when the DB is huge.
    allowed_tables: list[str] = Field(default_factory=list)

    # If True, refuse anything except SELECT / WITH...SELECT. The
    # framework's NLQ capability assumes True; flip only if you're
    # *intentionally* evaluating an INSERT/UPDATE-emitting model.
    read_only: bool = True


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    """Output of safe_execute. We carry the row data plus the column names
    + any error so the caller can both compare result sets and surface
    syntax/runtime errors for the Inspector."""
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error_kind: Optional[str] = None     # 'syntax' | 'runtime' | 'forbidden' | 'timeout'
    error_message: str = ""
    truncated: bool = False              # True if max_rows hit


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _engine(cfg: DBConfig):
    """Lazy-import SQLAlchemy. If it's missing we fail loud with an
    install hint rather than producing a cryptic ImportError."""
    try:
        from sqlalchemy import create_engine  # type: ignore
    except ImportError:
        raise RuntimeError(
            "SQLAlchemy not installed. Install with:\n"
            "    pip install 'sqlalchemy>=2.0'\n"
            "Plus the driver for your DB:\n"
            "    PostgreSQL: pip install psycopg[binary]\n"
            "    MySQL:      pip install pymysql\n"
            "    Oracle:     pip install oracledb\n"
            "    MSSQL:      pip install pyodbc"
        )
    # Connection pool kept small — the framework's call pattern is
    # one query per case, single-threaded.
    return create_engine(cfg.url, pool_size=2, max_overflow=2, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

# Allowed leading keywords. Anything else is rejected pre-execution.
_ALLOWED_STARTS = ("select", "with")

# Compiled once; checks if a string starts with one of the allowed words,
# tolerating leading whitespace + SQL line comments (-- ... and /* ... */).
_LEAD_RE = re.compile(r"\s*(--[^\n]*\n|/\*.*?\*/|\s)*", re.DOTALL)


def is_safe_select(sql: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative — false negatives are cheaper than
    false positives when LLMs are emitting the SQL."""
    if not sql or not sql.strip():
        return False, "empty SQL"

    # Strip leading whitespace + comments, then peek at the first word.
    m = _LEAD_RE.match(sql)
    head_idx = m.end() if m else 0
    stripped = sql[head_idx:].lstrip()
    if not stripped:
        return False, "SQL is whitespace/comments only"

    first_word = stripped.split(None, 1)[0].lower()
    if first_word not in _ALLOWED_STARTS:
        return False, f"only SELECT/WITH allowed; got {first_word!r}"

    # Reject multiple statements. Naive split — fine for the SELECT-only
    # case where semicolons are illegal inside identifiers without quotes.
    # Trailing semicolon is OK; mid-statement semicolons are not.
    body = sql.rstrip().rstrip(";")
    if ";" in body:
        return False, "multiple statements not allowed"

    # Reject obvious side-effecting clauses even *inside* a SELECT context
    # (e.g. SELECT ... INTO dest; pg_sleep(...); pragma writes).
    forbidden_patterns = (
        r"\binto\s+(outfile|dumpfile)\b",       # MySQL file write
        r"\bpg_sleep\s*\(",                     # PG DoS
        r"\bbenchmark\s*\(",                    # MySQL DoS
        r"\bpragma\s+",                         # SQLite settings
    )
    for pat in forbidden_patterns:
        if re.search(pat, sql, re.IGNORECASE):
            return False, f"forbidden construct: {pat}"

    return True, "ok"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def safe_execute(cfg: DBConfig, sql: str) -> ExecResult:
    """Run SQL with the safety guard + row cap + timeout. Never raises
    on a DB error — returns an ExecResult with `ok=False` and the error
    classified, so the calling capability can score it as a failure
    rather than crashing the trial."""

    if cfg.read_only:
        ok, reason = is_safe_select(sql)
        if not ok:
            return ExecResult(ok=False, error_kind="forbidden", error_message=reason)

    try:
        from sqlalchemy import text  # type: ignore
    except ImportError:
        return ExecResult(ok=False, error_kind="runtime",
                          error_message="SQLAlchemy not installed")

    engine = _engine(cfg)
    try:
        # Driver-agnostic timeout: SQLAlchemy doesn't unify timeouts across
        # dialects, so we wrap the call in a thread + join with timeout.
        # Works for any dialect; the trade-off is the in-flight query keeps
        # running on the DB until it completes — for production you should
        # also set a server-side statement_timeout (PG/MySQL) or profile.
        import threading
        result_holder: dict = {}

        def _worker():
            try:
                with engine.connect() as conn:
                    # Some dialects honor execute_options(stream_results=True)
                    # for streaming; we just cap rows manually below.
                    cursor = conn.execute(text(sql))
                    cols = list(cursor.keys()) if cursor.returns_rows else []
                    rows = []
                    truncated = False
                    if cursor.returns_rows:
                        for i, row in enumerate(cursor):
                            if i >= cfg.max_rows:
                                truncated = True
                                break
                            rows.append(tuple(row))
                    result_holder["res"] = ExecResult(
                        ok=True, columns=cols, rows=rows, truncated=truncated,
                    )
            except Exception as e:
                # SQLAlchemy wraps driver errors — classify by error class
                # name to avoid coupling to a specific dialect's exception
                # hierarchy.
                kind = "runtime"
                lower = str(e).lower()
                if "syntax" in lower or "ProgrammingError" in type(e).__name__:
                    kind = "syntax"
                result_holder["res"] = ExecResult(
                    ok=False, error_kind=kind, error_message=str(e)[:500],
                )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=cfg.query_timeout_ms / 1000.0)
        if t.is_alive():
            # Thread still running — the DB-side query will eventually
            # complete and be discarded. We report timeout to the caller.
            return ExecResult(ok=False, error_kind="timeout",
                              error_message=f"query exceeded {cfg.query_timeout_ms}ms")
        return result_holder.get("res", ExecResult(
            ok=False, error_kind="runtime", error_message="no result captured"))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def introspect_schema(cfg: DBConfig) -> dict:
    """Return {table_name: [{column, type, nullable, primary_key}, ...]}.
    Used by /init to enrich the NLQ system prompt with a real schema
    description rather than hand-written text."""
    from sqlalchemy import inspect  # type: ignore

    engine = _engine(cfg)
    try:
        insp = inspect(engine)
        out: dict = {}
        tables = insp.get_table_names()
        for tname in tables:
            if cfg.allowed_tables and tname not in cfg.allowed_tables:
                continue
            cols = insp.get_columns(tname)
            pk_set = set(insp.get_pk_constraint(tname).get("constrained_columns", []) or [])
            out[tname] = [
                {
                    "column": c["name"],
                    "type": str(c["type"]),
                    "nullable": c.get("nullable", True),
                    "primary_key": c["name"] in pk_set,
                }
                for c in cols
            ]
        return out
    finally:
        engine.dispose()


def schema_to_prompt(schema: dict) -> str:
    """Format an introspected schema as a compact, prompt-friendly string.
    Limits to one table per paragraph; types are SQL-dialect-normalized
    (the SQLAlchemy type repr is good enough for prompting)."""
    if not schema:
        return "(no tables available)"
    parts = []
    for table, cols in schema.items():
        col_lines = []
        for c in cols:
            tags = []
            if c["primary_key"]:
                tags.append("PK")
            if not c["nullable"]:
                tags.append("NOT NULL")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            col_lines.append(f"  - {c['column']} ({c['type']}){tag_str}")
        parts.append(f"TABLE {table}:\n" + "\n".join(col_lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Result-set comparison
# ---------------------------------------------------------------------------

def compare_result_sets(
    actual: ExecResult,
    expected: ExecResult,
    *,
    order_sensitive: bool = False,
) -> float:
    """Return a score in [0,1]:
       1.0 — same rows + same columns
       0.5 — same rows but different column order/names
       0.0 — different rows or either query failed
    The Inspector reads the raw ExecResults for qualitative diagnosis;
    this number is what drives the execution_equivalence metric.
    """
    if not (actual.ok and expected.ok):
        return 0.0

    # Normalize values to strings for type-agnostic comparison.
    def norm_rows(r: list[tuple]) -> list[tuple]:
        out = [tuple("" if v is None else str(v) for v in row) for row in r]
        return out if order_sensitive else sorted(out)

    a_rows = norm_rows(actual.rows)
    e_rows = norm_rows(expected.rows)
    if a_rows != e_rows:
        return 0.0

    # Rows match. Did column names match too?
    if [c.lower() for c in actual.columns] == [c.lower() for c in expected.columns]:
        return 1.0
    # Same rows, different column labels — partial credit.
    return 0.5
