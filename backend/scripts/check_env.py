"""Is this machine actually configured to run the project?

    uv run scripts/check_env.py

Setup scripts can install packages and copy templates. What they cannot do is
tell you whether the values you pasted into `.env` are the right ones, and that is
where the time goes — a wrong connection string, a project reference that does not
match, a migration nobody applied. Every check here answers a question that would
otherwise be answered by a stack trace several minutes into a run.

So this connects. It opens the database, looks for the tables the code expects,
and fetches the JWKS the API verifies tokens against. It does not call a model:
that costs money and a key being *present* is what a setup check can honestly
assert. Nothing here writes anything.

Exit status is 0 when everything required is in place, 1 otherwise, so
`setup.sh` and `setup.ps1` can both end by running it and stop if it fails.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

OK = "  ok  "
NO = " FAIL "
WARN = " warn "

# Required to run anything at all, with what each is for. A model key is handled
# separately below because either of two will do.
REQUIRED = {
    "POSTGRESQL_URI": "Postgres connection — use Supabase's *session pooler* URI",
    "SUPABASE_JWKS_URL": "verifies access tokens; the API refuses to start without it",
}

OPTIONAL = {
    "GITHUB_TOKEN": "raises the GitHub API rate limit from 60/hour to 5000",
    "SUPABASE_URL": "needed once guides are uploaded to Supabase Storage",
    "SUPABASE_SECRET_KEY": "server-side Storage writes; never expose to the browser",
    "SUPABASE_PUBLISHABLE_KEY": "the frontend's key — belongs in frontend/.env.local too",
    "LANGSMITH_API_KEY": "traces every run at smith.langchain.com",
    "CORS_ORIGINS": "defaults to http://localhost:3000",
}

MODEL_KEYS = ("GEMINI_API_KEY", "OPENROUTER_API_KEY")


class Report:
    """Accumulates results so every check runs before anything is reported."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def ok(self, message: str) -> None:
        print(f"[{OK}] {message}")

    def warn(self, message: str) -> None:
        print(f"[{WARN}] {message}")

    def fail(self, message: str, remedy: str = "") -> None:
        print(f"[{NO}] {message}")
        if remedy:
            print(f"         -> {remedy}")
        self.failed.append(message)


def check_env_file(report: Report) -> None:
    if (ROOT / ".env").is_file():
        report.ok(".env exists")
        return
    report.fail(
        ".env is missing",
        "copy backend/.env.example to backend/.env and fill it in",
    )


def check_variables(report: Report) -> None:
    for name, purpose in REQUIRED.items():
        if os.environ.get(name):
            report.ok(f"{name} is set")
        else:
            report.fail(f"{name} is not set", purpose)

    if any(os.environ.get(name) for name in MODEL_KEYS):
        present = [name for name in MODEL_KEYS if os.environ.get(name)]
        report.ok(f"model provider key present ({', '.join(present)})")
    else:
        report.fail(
            "no model provider key",
            f"set one of {' or '.join(MODEL_KEYS)} — the agent cannot think without one",
        )

    for name, purpose in OPTIONAL.items():
        if not os.environ.get(name):
            report.warn(f"{name} is not set — {purpose}")


def check_database(report: Report) -> None:
    uri = os.environ.get("POSTGRESQL_URI")
    if not uri:
        return

    try:
        import psycopg
    except ImportError:
        report.fail("psycopg is not installed", "run `uv sync` in backend/")
        return

    try:
        with psycopg.connect(uri, connect_timeout=15) as conn:
            version = one(conn, "select version()").split(",")[0]
            report.ok(f"database reachable — {version}")

            tables = {
                row[0]
                for row in conn.execute(
                    "select tablename from pg_tables where schemaname in ('public', 'langgraph')"
                ).fetchall()
            }
            if "runs" in tables:
                report.ok("public.runs exists")
            else:
                report.fail(
                    "public.runs is missing",
                    "run `uv run scripts/apply_migrations.py` in backend/",
                )
            if "checkpoints" in tables:
                report.ok("langgraph checkpoint tables exist")
            else:
                report.warn(
                    "langgraph checkpoint tables not created yet — "
                    "they appear the first time the API or a proof script starts"
                )
    except psycopg.OperationalError as error:
        first = str(error).strip().splitlines()[0]
        report.fail(
            f"cannot connect to the database — {first}",
            "on Supabase use the *session pooler* URI from Dashboard -> Connect. "
            "The direct db.<ref>.supabase.co host resolves on IPv6 only and is "
            "unreachable from most networks.",
        )
    except psycopg.Error as error:
        report.fail(f"database error — {str(error).strip().splitlines()[0]}")


def check_jwks(report: Report) -> None:
    url = os.environ.get("SUPABASE_JWKS_URL")
    if not url:
        return
    try:
        import requests

        response = requests.get(url, timeout=15)
        keys = response.json().get("keys", []) if response.ok else []
    except Exception as error:  # noqa: BLE001 - any failure here is the same answer
        report.fail(f"JWKS endpoint unreachable — {error}")
        return

    if keys:
        algorithms = sorted({key.get("alg", "?") for key in keys})
        report.ok(f"JWKS reachable — {len(keys)} key(s), {', '.join(algorithms)}")
    else:
        report.fail(
            f"JWKS returned no keys (HTTP {response.status_code})",
            "check the project reference in the URL",
        )


def one(conn: Any, query: str) -> Any:
    row = conn.execute(query).fetchone()
    return row[0] if row else None


def main() -> int:
    print(f"Checking {ROOT}\n")
    report = Report()
    check_env_file(report)
    check_variables(report)
    print()
    check_database(report)
    check_jwks(report)

    print()
    if report.failed:
        print(f"{len(report.failed)} problem(s) must be fixed before the API will run:")
        for message in report.failed:
            print(f"   - {message}")
        return 1

    print("Ready. Start the API with:  uv run python serve.py --reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
