"""Proof that a signed-in user can read their own runs and nobody else's.

    uv run scripts/prove_rls.py

`db/001_runs.sql` claims two things it cannot demonstrate about itself: that
`authenticated` can read its own rows, and that it can do nothing else. Both are
worth checking rather than believing, because every way of getting RLS wrong
fails silently in the safe-looking direction — a policy that is never consulted,
a grant that was never made, a `using` clause that matches every row. None of
those raise. You get rows back, or you get none, and either can look correct from
the outside.

They are also not checkable as `postgres`, which is what every other script here
connects as. `postgres` owns the table and carries `rolbypassrls`, so a query it
runs never consults a policy at all. The only honest test is to become the role
the frontend will actually be, which is what `set local role authenticated` plus
a `sub` claim does — the same pair PostgREST sets up for each request it serves.

Nothing here persists. The whole proof runs inside one transaction that is always
rolled back, so the two users it invents and the runs it gives them exist for the
length of the check and are gone before the connection closes. That is what makes
it safe to point at the real database rather than a scratch one.
"""

from __future__ import annotations

import os
import sys
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

from repo_cartographer.persistence import URI_ENV

ROOT = Path(__file__).resolve().parent.parent

OK = "  ok  "
NO = " FAIL "


def scalar(cur: psycopg.Cursor[Any]) -> Any:
    """The one value of a one-row, one-column query.

    `fetchone()` is typed as possibly `None` because most queries may legitimately
    return nothing. Every query below is a count, a `current_user` or an
    `auth.uid()`, all of which always produce a row, so the assertion lives here
    once rather than at nine call sites.
    """
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected one row, got none")
    return row[0]


def check(label: str, actual: object, expected: object, failures: list[str]) -> None:
    passed = actual == expected
    print(f"[{OK if passed else NO}] {label}")
    if not passed:
        print(f"          expected {expected!r}, got {actual!r}")
        failures.append(label)


def become(conn: psycopg.Connection[Any], user: UUID | None) -> None:
    """Take on the identity PostgREST would give a request.

    Two settings, because they answer different questions. `set local role`
    decides which policies apply at all — as `postgres`, none of them do. The
    `request.jwt.claims` setting is where `auth.uid()` reads `sub`, so it decides
    which rows those policies then match. Both are `local` and end with the
    transaction.

    `set_config(...)` rather than a `set local` statement for the claims because
    the value is data: `SET` takes no parameters, so the claim would otherwise
    have to be interpolated into SQL text.
    """
    role = "authenticated" if user else "anon"
    claims = f'{{"sub": "{user}", "role": "{role}"}}' if user else '{"role": "anon"}'
    conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
    conn.execute(f"set local role {role}")


def refusals(conn: psycopg.Connection[Any], alice: UUID, failures: list[str]) -> None:
    """Confirm that none of the write verbs can change anything.

    Two things make this less obvious than it looks.

    The first is that **RLS does not raise**. With no matching policy, an UPDATE or
    a DELETE is not an error — it matches no rows and reports success, having done
    nothing. So "did it throw?" is the wrong question; an earlier version of this
    script asked exactly that and reported a perfectly safe table as broken. The
    question is whether anything actually changed, which is `rowcount`.

    The second is that **TRUNCATE is not covered by RLS at all**. Postgres does not
    apply row security to it, so no policy above can stop it and the privilege is
    the only control. It is therefore held to a stricter standard here: the others
    may pass by affecting nothing, but TRUNCATE has to be refused outright.

    Each statement gets its own savepoint via a nested `conn.transaction()`,
    because a failed statement aborts the surrounding transaction — without one,
    the first refusal would poison every check after it and one denial would be
    reported as four failures.
    """
    insert = "insert into public.runs (user_id, thread_id, repo, question) values (%s, %s, %s, %s)"
    attempts: tuple[tuple[str, str, tuple[Any, ...], bool], ...] = (
        ("INSERT", insert, (alice, "thread-new", "x/y", "q"), False),
        ("UPDATE", "update public.runs set repo = %s", ("hijacked",), False),
        ("DELETE", "delete from public.runs", (), False),
        ("TRUNCATE", "truncate public.runs", (), True),
    )
    for label, statement, params, must_raise in attempts:
        try:
            with conn.transaction():
                affected = conn.execute(statement, params).rowcount
        except psycopg.Error as error:
            print(f"[{OK}] {label} refused — {str(error).strip().splitlines()[0]}")
            continue

        if must_raise:
            print(f"[{NO}] {label} was permitted — no policy can prevent this one")
            failures.append(f"{label} was permitted")
        elif affected == 0:
            print(f"[{OK}] {label} permitted by grant but matched no rows (RLS)")
        else:
            print(f"[{NO}] {label} changed {affected} row(s)")
            failures.append(f"{label} changed {affected} row(s)")


def as_alice(conn: psycopg.Connection[Any], alice: UUID, bob: UUID, failures: list[str]) -> None:
    """Every check that runs while wearing Alice's identity."""
    check(
        "current role is authenticated",
        scalar(conn.execute("select current_user")),
        "authenticated",
        failures,
    )
    check(
        "auth.uid() resolves to Alice",
        scalar(conn.execute("select auth.uid()")),
        alice,
        failures,
    )
    check(
        "Alice sees exactly one run",
        scalar(conn.execute("select count(*) from public.runs")),
        1,
        failures,
    )
    check(
        "...and it is hers",
        scalar(conn.execute("select repo from public.runs")),
        "pallets/flask",
        failures,
    )
    check(
        "Bob's run stays invisible even when asked for by id",
        scalar(conn.execute("select count(*) from public.runs where user_id = %s", (bob,))),
        0,
        failures,
    )


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    uri = os.environ.get(URI_ENV)
    if not uri:
        print(f"{URI_ENV} is not set. See repo_cartographer/persistence.py.")
        return 1

    alice, bob = uuid4(), uuid4()
    failures: list[str] = []

    # Not autocommit, so everything below runs inside one implicit transaction
    # that is rolled back at the end. That is what keeps the invented users and
    # their runs from ever becoming real rows in the real database.
    with psycopg.connect(uri, connect_timeout=15) as conn:
        # Only `id` is NOT NULL without a default on auth.users, so a test
        # identity costs exactly one column.
        conn.execute("insert into auth.users (id) values (%s), (%s)", (alice, bob))
        for owner, repo in ((alice, "pallets/flask"), (bob, "psf/requests")):
            conn.execute(
                "insert into public.runs (user_id, thread_id, repo, question)"
                " values (%s, %s, %s, %s)",
                (owner, f"thread-{owner}", repo, "how does it work?"),
            )

        print("two users, one run each, inserted as postgres\n")
        check(
            "postgres sees both rows (it owns the table, so RLS is not consulted)",
            scalar(conn.execute("select count(*) from public.runs")),
            2,
            failures,
        )

        print("\nnow acting as Alice, the way PostgREST would:\n")
        become(conn, alice)
        as_alice(conn, alice, bob, failures)
        refusals(conn, alice, failures)

        print("\nand as an anonymous visitor:\n")
        conn.execute("reset role")
        become(conn, None)
        try:
            with conn.transaction():
                seen = scalar(conn.execute("select count(*) from public.runs"))
        except psycopg.Error as error:
            print(f"[{OK}] anon is refused outright — {str(error).strip().splitlines()[0]}")
        else:
            check("anon sees nothing", seen, 0, failures)

        conn.execute("reset role")
        # Everything above is undone here. The check that follows opens a fresh
        # transaction and only reads, so it sees the database as it really is.
        conn.rollback()

        check(
            "the invented users are gone (the transaction was rolled back)",
            scalar(
                conn.execute(
                    "select count(*) from auth.users where id in (%s, %s)",
                    (alice, bob),
                )
            ),
            0,
            failures,
        )

    print()
    if failures:
        print(f"FAIL — {len(failures)} check(s) did not hold:")
        for label in failures:
            print(f"   - {label}")
        return 1

    print("PASS — a signed-in user reads their own runs and only their own, and")
    print("cannot write them at all. The policy in db/001_runs.sql is doing that,")
    print("not application code that has yet to be written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
