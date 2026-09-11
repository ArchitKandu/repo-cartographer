"""Reading and writing the `runs` table.

One row per mapping run — the durable record that it happened, who asked, and
where it got to. `db/001_runs.sql` is the schema and the argument for its shape;
this is the only Python that touches it.

Every query here is written `where user_id = %s` even though the service connects
as `postgres`, which owns the table and bypasses RLS. That looks redundant next to
the policy in the migration and is not: RLS is protecting the *browser's* direct
Data API access, a path that does not come through this module at all. On this
connection there is no policy in the way, so a missing predicate here is a real
cross-user read with nothing behind it to catch the mistake. Two layers, two
different attackers.

The status column is the frontend's whole model of a run. It moves
`running → awaiting_approval → done | failed`, and `awaiting_approval` is the one
that earns the table: it is how a page rendered minutes later — in a different
process, possibly on a different machine — knows there is a pending approval to
show, without asking LangGraph to enumerate threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

# Mirrors the CHECK constraint in db/001_runs.sql. Duplicated deliberately: the
# database is the authority and will reject anything else, but a typo caught here
# names the column and the value, while the same typo caught there arrives as a
# constraint violation from inside a background task nobody is watching.
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
DONE = "done"
FAILED = "failed"

# Composed rather than interpolated. These are constants, not input, so an
# f-string would be safe in fact — but it would also be indistinguishable from the
# unsafe version at a glance and to a linter, and this table is the one place in
# the project where a user id appears in a predicate. `sql.Identifier` makes the
# distinction structural: values can only ever arrive as `%s` parameters.
_FIELDS = (
    "id",
    "user_id",
    "thread_id",
    "repo",
    "question",
    "status",
    "guide_path",
    "error",
    "created_at",
    "finished_at",
    "answer",
)
_COLUMNS = sql.SQL(", ").join(sql.Identifier(field) for field in _FIELDS)


@dataclass(frozen=True)
class Run:
    """One row, as the API layer sees it."""

    id: UUID
    user_id: UUID
    thread_id: str
    repo: str
    question: str
    status: str
    guide_path: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    answer: str | None

    @property
    def is_finished(self) -> bool:
        return self.status in (DONE, FAILED)

    def as_json(self) -> dict[str, Any]:
        """The shape the frontend receives.

        `user_id` is deliberately absent. The caller already knows who they are —
        it is the only user whose runs they can fetch — so returning it adds
        nothing except one more place a user id can leak into a log or a URL.
        """
        return {
            "id": str(self.id),
            "thread_id": self.thread_id,
            "repo": self.repo,
            "question": self.question,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "answer": self.answer,
        }


def _row(record: tuple[Any, ...]) -> Run:
    return Run(*record)


async def create(
    pool: AsyncConnectionPool, *, user_id: UUID, thread_id: str, repo: str, question: str
) -> Run:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL(
                "insert into public.runs (user_id, thread_id, repo, question, status)"
                " values (%s, %s, %s, %s, %s) returning {columns}"
            ).format(columns=_COLUMNS),
            (user_id, thread_id, repo, question, RUNNING),
        )
        record = await cur.fetchone()
    if record is None:  # pragma: no cover - insert ... returning always yields a row
        raise RuntimeError("insert returned no row")
    return _row(record)


async def get(pool: AsyncConnectionPool, *, user_id: UUID, run_id: UUID) -> Run | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL(
                "select {columns} from public.runs where id = %s and user_id = %s"
            ).format(columns=_COLUMNS),
            (run_id, user_id),
        )
        record = await cur.fetchone()
    return _row(record) if record else None


async def list_for(pool: AsyncConnectionPool, *, user_id: UUID, limit: int = 50) -> list[Run]:
    """This user's runs, newest first — the query `runs_user_id_created_at_idx` covers."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL(
                "select {columns} from public.runs where user_id = %s"
                " order by created_at desc limit %s"
            ).format(columns=_COLUMNS),
            (user_id, limit),
        )
        records = await cur.fetchall()
    return [_row(record) for record in records]


@dataclass(frozen=True)
class Outcome:
    """What a run produced, for the columns that are written together.

    Grouped rather than passed as three more keyword arguments because they are
    one thing — the result — and because every one of them is `coalesce`d below,
    so a caller that forgets one silently keeps the previous value. A single
    object makes "I am recording a result" one decision instead of three.
    """

    error: str | None = None
    guide_path: str | None = None
    answer: str | None = None


NOTHING = Outcome()


async def set_status(
    pool: AsyncConnectionPool,
    *,
    thread_id: str,
    status: str,
    outcome: Outcome = NOTHING,
) -> None:
    """Move a run to a new state.

    Keyed on `thread_id` rather than `id` because the background task that calls
    this knows the thread — that is what it handed LangGraph — and looking the row
    up by id first would be a second query to learn something it already has.
    `thread_id` is unique, so it addresses exactly one row.

    `finished_at` is set by the same statement that sets a terminal status, rather
    than by the caller remembering to. A row in `done` with a null `finished_at`
    is not a state this table should be able to represent.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "update public.runs set status = %s,"
            " error = coalesce(%s, error),"
            " guide_path = coalesce(%s, guide_path),"
            " answer = coalesce(%s, answer),"
            " finished_at = case when %s in ('done', 'failed') then now() else finished_at end"
            " where thread_id = %s",
            (status, outcome.error, outcome.guide_path, outcome.answer, status, thread_id),
        )
