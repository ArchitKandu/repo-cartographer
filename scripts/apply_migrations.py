"""Apply every SQL file in db/ to the configured Postgres database.

    uv run scripts/apply_migrations.py           # apply
    uv run scripts/apply_migrations.py --dry-run # list what would run

There is no migration-tracking table and nothing remembers what has already been
applied. Files are read in filename order and executed, every time. That trades a
piece of machinery for a rule the files themselves have to keep — every statement
must be safe to execute twice — and `db/001_runs.sql` explains at the top what
that means in practice.

The trade is worth making at this size. A tracking table is a second source of
truth about the schema, and the failure it produces is worse than the one it
prevents: a migration recorded as applied against a database where it was rolled
back leaves the two permanently out of step, and the fix is hand-editing
bookkeeping rows. Re-running idempotent DDL has no such failure mode. Revisit this
when a migration needs to be destructive — a `drop column`, a backfill — because
that is the point where "safe to run twice" stops being writable.

Each file runs in its own transaction, so a file either applies completely or not
at all, and a failure in the third file leaves the first two applied.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from dotenv import load_dotenv

from repo_cartographer.persistence import URI_ENV

ROOT = Path(__file__).resolve().parent.parent


def scalar(cur: psycopg.Cursor[Any]) -> Any:
    """The one value of a one-row, one-column query.

    `fetchone()` is typed as possibly `None` because most queries may legitimately
    return nothing. The queries here are counts and `current_user` — they always
    produce a row — so this asserts that once rather than at each call site.
    """
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected one row, got none")
    return row[0]


MIGRATIONS = ROOT / "db"


def migrations() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)

    files = migrations()
    if not files:
        print(f"No .sql files in {MIGRATIONS}.")
        return 1

    if "--dry-run" in sys.argv[1:]:
        print(f"Would apply {len(files)} file(s) from {MIGRATIONS}:")
        for path in files:
            print(f"   {path.name}  ({len(path.read_text(encoding='utf-8').splitlines())} lines)")
        return 0

    uri = os.environ.get(URI_ENV)
    if not uri:
        print(f"{URI_ENV} is not set. See repo_cartographer/persistence.py.")
        return 1

    # Autocommit off: psycopg opens a transaction per `execute` block, and each
    # file is committed as a unit below.
    with psycopg.connect(uri, connect_timeout=15) as conn:
        print(f"Connected as {scalar(conn.execute('select current_user'))}.\n")
        for path in files:
            print(f"applying {path.name} …", end=" ")
            try:
                conn.execute(path.read_text(encoding="utf-8"))
            except psycopg.Error as error:
                conn.rollback()
                print("FAILED")
                print(f"\n{path.name} was rolled back and nothing in it applied:\n\n{error}")
                return 1
            conn.commit()
            print("ok")

    print(f"\n{len(files)} file(s) applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
