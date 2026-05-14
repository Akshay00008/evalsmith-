# tools/introspect_db.py
# Connect to a project's DB (configured in <project>/data/db.json), dump
# the schema to <project>/data/schema.txt, and print a sample of each
# table's first row. The schema dump is the artifact /plan reads to seed
# the NLQ system prompt.
#
# Usage:
#   python tools/introspect_db.py --project projects/my_nlq
#
# Before running, create <project>/data/db.json with at least:
#   {"url": "sqlite:///./projects/my_nlq/data/sample.db"}
# For other DBs see lib/db.py:DBConfig for the URL shape per dialect.

from __future__ import annotations
from pathlib import Path
import argparse
import json
import sys


def introspect_project(project_dir: Path, *, sample_rows: int = 2) -> int:
    """Return the number of tables discovered (or 0 on error)."""
    sys.path.insert(0, str(project_dir.parent.parent))
    from lib import db as db_mod  # noqa: E402

    cfg_path = project_dir / "data" / "db.json"
    if not cfg_path.exists():
        print(f"ERROR: missing {cfg_path}")
        print("Create it with at minimum:")
        print('  {"url": "sqlite:///./path/to.db"}')
        print("See lib/db.py:DBConfig for full options.")
        return 0

    cfg = db_mod.DBConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    print(f"Connecting to {cfg.url} ...")
    schema = db_mod.introspect_schema(cfg)
    if not schema:
        print("No tables found (or all filtered by allowed_tables).")
        return 0

    print(f"Found {len(schema)} table(s):\n")
    rendered = db_mod.schema_to_prompt(schema)
    print(rendered)

    # Sample a few rows per table — helps the user sanity-check the data
    # and write better gold SQL for eval cases.
    print("\n--- sample rows ---\n")
    for tname in schema:
        # Use a parameter-free query so we don't accidentally trip the
        # safety guard.
        res = db_mod.safe_execute(cfg, f"SELECT * FROM {tname} LIMIT {sample_rows}")
        if not res.ok:
            print(f"{tname}: ERROR {res.error_kind}: {res.error_message}")
            continue
        print(f"{tname} (showing {len(res.rows)} of up to {sample_rows}):")
        print("  columns:", res.columns)
        for row in res.rows:
            print("   ", row)
        print()

    # Persist the schema dump — /plan reads this to seed the NLQ system prompt.
    out_path = project_dir / "data" / "schema.txt"
    out_path.write_text(rendered, encoding="utf-8")
    print(f"\nWrote schema dump to {out_path}")
    print("Architect will reference this during /plan.")
    return len(schema)


def main() -> None:
    p = argparse.ArgumentParser(description="Introspect a project's DB schema for NLQ missions.")
    p.add_argument("--project", required=True, help="Project directory (e.g. projects/my_nlq/).")
    p.add_argument("--sample-rows", type=int, default=2, help="Rows to sample per table.")
    args = p.parse_args()
    project_dir = Path(args.project).resolve()
    if not project_dir.exists():
        print(f"ERROR: project dir not found: {project_dir}")
        sys.exit(1)
    n = introspect_project(project_dir, sample_rows=args.sample_rows)
    sys.exit(0 if n > 0 else 2)


if __name__ == "__main__":
    main()
