"""Proof that the HTTP surface drives a real run from start to finish.

    uv run scripts/prove_api.py

`tests/test_api.py` checks the edge of the application — routes mounted, tokens
required — without a database or a model, which is the right shape for a suite
that has to stay fast. It cannot tell you the thing you actually want to know,
which is whether posting a question to this service produces a guide.

So this boots the real app with its real lifespan, against the real database, and
walks the whole path a browser would: start a run, poll it, read the guide it
wrote. The only substitution is the identity. `current_user` is overridden with a
user this script creates, because minting a genuine Supabase access token would
mean signing one, which would mean holding the project's private key — the exact
thing `auth.py` is designed never to need. Authentication is verified separately
and offline in `tests/test_api.py`; what is under test here is everything after it.

Everything it creates, it deletes. The user row is removed in a `finally`, and
`runs.user_id` is `on delete cascade`, so the run row goes with it.

Nothing reaches GitHub: `open_pull_request` refuses unless `ALLOW_PULL_REQUESTS`
is set, and this script refuses to run if it is.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg
from dotenv import load_dotenv
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

from repo_cartographer.api import create_app  # noqa: E402
from repo_cartographer.auth import current_user  # noqa: E402
from repo_cartographer.persistence import URI_ENV  # noqa: E402
from repo_cartographer.pull_requests import ALLOW_ENV  # noqa: E402

TARGET = "chalk/chalk"
# Asks for a guide *file* on purpose. An earlier version of this script asked for
# prose, and the run finished `done` with no guide to fetch — which was a correct
# outcome for that question and a useless one for a script whose job is to prove
# the guide route works. Both paths matter, so the question names the artefact and
# the checks below cover the prose answer as well.
QUESTION = (
    "Read the README and write a short onboarding guide for this project to "
    "/guide.md. Do not explore any other file."
)

# A mapping run is minutes of model calls, so this is a ceiling rather than an
# expectation. Reaching it is a failure of the run, not of the timeout.
DEADLINE_SECONDS = 600
POLL_SECONDS = 5

TERMINAL = {"done", "failed"}

# Named because ruff is right that a bare number in a comparison is opaque, and
# because what each one means here is the assertion rather than the number.
ACCEPTED = 202
OK = 200
NOT_FOUND = 404


def refuse_if_live() -> None:
    if os.environ.get(ALLOW_ENV, "").strip().lower() == "true":
        raise SystemExit(
            f"{ALLOW_ENV} is set. This script drives the real agent, so unset it "
            "before running — nothing here should be able to reach GitHub."
        )


def make_user(uri: str, user_id: UUID) -> None:
    with psycopg.connect(uri, connect_timeout=15, autocommit=True) as conn:
        conn.execute("insert into auth.users (id) values (%s)", (user_id,))


def drop_user(uri: str, user_id: UUID) -> None:
    """Delete the user, and with it — by cascade — the run row it owns."""
    with psycopg.connect(uri, connect_timeout=15, autocommit=True) as conn:
        conn.execute("delete from auth.users where id = %s", (user_id,))


def poll(client: TestClient, run_id: str) -> dict[str, Any]:
    """Watch a run the way the frontend will, and report what it sees."""
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < DEADLINE_SECONDS:
        run = client.get(f"/runs/{run_id}").json()
        status = run["status"]
        if status != last:
            print(f"   [{int(time.monotonic() - started):>4}s] {status}")
            last = status
        if status in TERMINAL:
            return run
        time.sleep(POLL_SECONDS)
    return {"status": "timed out", "error": f"still running after {DEADLINE_SECONDS}s"}


def inspect_finished(client: TestClient, run: dict[str, Any], final: dict[str, Any]) -> int:
    """Everything worth checking once a run has reached `done`.

    Split out of `main` because it is a different question. `main` is about
    getting a run to finish at all; this is about whether what it left behind is
    reachable — the list, the answer on the row, the guide, and the 404 that
    keeps one user's run ids from confirming another user's.
    """
    listed = client.get("/runs").json()
    print(f"GET /runs returns {len(listed)} run(s) for this user")

    answer = final.get("answer")
    if not answer:
        print("FAIL — the run finished with no answer recorded on the row.")
        return 1
    print(f"the row carries a {len(answer)}-char answer:")
    print(f"   │ {answer.splitlines()[0][:90]}")

    guide = client.get(f"/runs/{run['id']}/guide")
    if guide.status_code != OK:
        print(f"FAIL — GET guide answered {guide.status_code}: {guide.text}")
        return 1

    body = guide.text
    print(f"GET /runs/{{id}}/guide returns {len(body)} chars of markdown\n")
    for line in body.splitlines()[:6]:
        print(f"   │ {line}")
    print("   │ …\n")

    missing = client.get(f"/runs/{uuid4()}")
    print(f"a run id belonging to nobody -> {missing.status_code} (404 expected)")
    if missing.status_code != NOT_FOUND:
        print("FAIL — an unknown run should be indistinguishable from another user's.")
        return 1
    return 0


def main() -> int:
    refuse_if_live()
    uri = os.environ.get(URI_ENV)
    if not uri:
        print(f"{URI_ENV} is not set. See repo_cartographer/persistence.py.")
        return 1

    user_id = uuid4()
    app = create_app()
    # The one substitution. See the module docstring for why this is not the same
    # as skipping authentication.
    app.dependency_overrides[current_user] = lambda: user_id

    make_user(uri, user_id)
    print(f"Created a throwaway user {user_id}.")
    print(f"Asking the API to map {TARGET}.\n")

    try:
        # `with` is what runs the lifespan, which is what opens the pool and
        # builds the graph. Without it every route would 500 on `app.state.pool`.
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}  # noqa: S101

            created = client.post("/runs", json={"repo": TARGET, "question": QUESTION})
            if created.status_code != ACCEPTED:
                print(f"FAIL — POST /runs answered {created.status_code}: {created.text}")
                return 1
            run = created.json()
            print(f"   run {run['id']}  thread {run['thread_id']}")
            print(f"   POST /runs -> {created.status_code} {run['status']}\n")

            print("polling GET /runs/{id} the way the frontend will:")
            final = poll(client, run["id"])
            print()

            if final["status"] != "done":
                print(f"FAIL — the run ended as {final['status']}.")
                if final.get("error"):
                    print(f"   {final['error']}")
                return 1

            # Not `return` — the PASS banner lives after the `finally` that cleans
            # up, and returning here would skip it and exit 0 in silence.
            failed = inspect_finished(client, run, final)
            if failed:
                return failed
    finally:
        drop_user(uri, user_id)
        print(f"\nCleaned up: user {user_id} and its run row are gone.")

    print("\nPASS — a question posted over HTTP became a guide fetched over HTTP,")
    print("through the real agent, the real database and the real approval-gated")
    print("graph. Authentication was substituted; everything after it was not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
