# tests/test_db.py
# Verifies the DB safety guard, schema introspection, and result-set
# comparison using SQLite (stdlib — no external service needed).
#
# We deliberately don't test against PostgreSQL/Oracle/MSSQL here — those
# require live servers and the connector is dialect-agnostic via
# SQLAlchemy, so SQLite gives us functional coverage at zero infra cost.

from __future__ import annotations
import pytest
import sqlite3
import tempfile
from pathlib import Path

# Skip the whole module gracefully if SQLAlchemy isn't installed — we
# don't want to make it a hard test dependency.
pytest.importorskip("sqlalchemy")

from lib import db as db_mod


@pytest.fixture
def sqlite_project():
    """Build a tiny SQLite database + DBConfig pointing at it. Yields the
    DBConfig; cleans up the temp file on teardown."""
    tmpdir = Path(tempfile.mkdtemp(prefix="agt_db_"))
    db_path = tmpdir / "test.db"
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, plan TEXT);
        INSERT INTO users VALUES (1, 'alice@example.com', 'Pro');
        INSERT INTO users VALUES (2, 'bob@example.com', 'Free');
        INSERT INTO users VALUES (3, 'carol@example.com', 'Pro');

        CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT);
        INSERT INTO events VALUES (1, 1, 'login');
        INSERT INTO events VALUES (2, 1, 'purchase');
        INSERT INTO events VALUES (3, 3, 'login');
    """)
    con.commit()
    con.close()

    cfg = db_mod.DBConfig(url=f"sqlite:///{db_path.as_posix()}", read_only=True)
    yield cfg


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

def test_safety_guard_allows_simple_select():
    ok, reason = db_mod.is_safe_select("SELECT * FROM users")
    assert ok, reason


def test_safety_guard_allows_with_cte():
    ok, reason = db_mod.is_safe_select("WITH x AS (SELECT 1) SELECT * FROM x")
    assert ok, reason


def test_safety_guard_blocks_insert():
    ok, reason = db_mod.is_safe_select("INSERT INTO users VALUES (4, 'x', 'y')")
    assert not ok
    assert "select" in reason.lower() or "got" in reason.lower()


def test_safety_guard_blocks_update():
    ok, _ = db_mod.is_safe_select("UPDATE users SET plan='Pro' WHERE id=1")
    assert not ok


def test_safety_guard_blocks_drop():
    ok, _ = db_mod.is_safe_select("DROP TABLE users")
    assert not ok


def test_safety_guard_blocks_multiple_statements():
    """Even when both statements would be SELECTs, multiple statements
    are rejected — they're a vector for stacked attacks in some drivers."""
    ok, reason = db_mod.is_safe_select("SELECT 1; SELECT 2")
    assert not ok
    assert "multiple" in reason.lower()


def test_safety_guard_blocks_known_dos_constructs():
    """pg_sleep, benchmark, INTO OUTFILE — known abuse patterns."""
    for bad in (
        "SELECT pg_sleep(60)",
        "SELECT BENCHMARK(1000000, MD5('a'))",
        "SELECT * INTO OUTFILE '/tmp/x' FROM users",
    ):
        ok, _ = db_mod.is_safe_select(bad)
        assert not ok, f"Expected block on: {bad}"


def test_safety_guard_strips_leading_comments():
    """LLMs frequently prepend `-- ...` explanations to their SQL.
    The guard should look past comments to find the real first keyword."""
    ok, reason = db_mod.is_safe_select("-- Counts active Pro users\nSELECT COUNT(*) FROM users WHERE plan='Pro'")
    assert ok, reason


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def test_introspect_returns_both_tables(sqlite_project):
    schema = db_mod.introspect_schema(sqlite_project)
    assert set(schema.keys()) == {"users", "events"}
    # Verify column metadata shape on `users`.
    users_cols = {c["column"]: c for c in schema["users"]}
    assert "id" in users_cols
    assert users_cols["id"]["primary_key"] is True
    assert users_cols["email"]["nullable"] is False


def test_introspect_respects_allowed_tables(sqlite_project):
    sqlite_project.allowed_tables = ["users"]
    schema = db_mod.introspect_schema(sqlite_project)
    assert set(schema.keys()) == {"users"}


def test_schema_to_prompt_includes_pk_and_not_null(sqlite_project):
    schema = db_mod.introspect_schema(sqlite_project)
    text = db_mod.schema_to_prompt(schema)
    assert "TABLE users" in text
    assert "[PK" in text or "[PK," in text
    assert "NOT NULL" in text


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_execute_simple_select_returns_rows(sqlite_project):
    res = db_mod.safe_execute(sqlite_project, "SELECT COUNT(*) AS n FROM users")
    assert res.ok
    assert res.columns == ["n"]
    assert res.rows == [(3,)]


def test_execute_forbidden_sql_is_classified(sqlite_project):
    res = db_mod.safe_execute(sqlite_project, "DELETE FROM users")
    assert not res.ok
    assert res.error_kind == "forbidden"


def test_execute_syntax_error_is_classified(sqlite_project):
    res = db_mod.safe_execute(sqlite_project, "SELECT * FROMM users")
    assert not res.ok
    # Either 'syntax' or 'runtime' is acceptable depending on driver
    # exception class; just verify we didn't silently succeed.
    assert res.error_kind in ("syntax", "runtime")


def test_execute_respects_max_rows(sqlite_project):
    sqlite_project.max_rows = 2
    res = db_mod.safe_execute(sqlite_project, "SELECT * FROM users")
    assert res.ok
    assert len(res.rows) == 2
    assert res.truncated is True


# ---------------------------------------------------------------------------
# Result-set comparison
# ---------------------------------------------------------------------------

def test_compare_identical_results_scores_1(sqlite_project):
    r1 = db_mod.safe_execute(sqlite_project, "SELECT plan FROM users WHERE id=1")
    r2 = db_mod.safe_execute(sqlite_project, "SELECT plan FROM users WHERE id=1")
    assert db_mod.compare_result_sets(r1, r2) == 1.0


def test_compare_same_rows_different_aliases_scores_half(sqlite_project):
    """Same rows, but the second query renames the column."""
    r1 = db_mod.safe_execute(sqlite_project, "SELECT COUNT(*) FROM users")
    r2 = db_mod.safe_execute(sqlite_project, "SELECT COUNT(*) AS total FROM users")
    # Rows match (the count value), columns differ ('COUNT(*)' vs 'total').
    assert db_mod.compare_result_sets(r1, r2) == 0.5


def test_compare_different_rows_scores_zero(sqlite_project):
    r1 = db_mod.safe_execute(sqlite_project, "SELECT email FROM users WHERE plan='Pro'")
    r2 = db_mod.safe_execute(sqlite_project, "SELECT email FROM users WHERE plan='Free'")
    assert db_mod.compare_result_sets(r1, r2) == 0.0


def test_compare_failed_query_scores_zero(sqlite_project):
    r1 = db_mod.safe_execute(sqlite_project, "SELECT * FROM nonexistent")
    r2 = db_mod.safe_execute(sqlite_project, "SELECT * FROM users")
    assert db_mod.compare_result_sets(r1, r2) == 0.0
