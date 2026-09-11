"""The HTTP surface: start a mapping, watch it, answer its approval gate.

    uv run uvicorn repo_cartographer.api:app --reload

Three facts about this agent decide the shape of every route below.

**A run takes minutes.** So `POST /runs` cannot be the request that does the work.
It records the run, starts it in the background and returns immediately with an
id; the client polls `GET /runs/{id}`. That also means this service cannot be
serverless — there has to be a process alive between the two requests, which is
the same constraint that made a Postgres checkpointer necessary rather than nice.

**A run can stop and wait for a person.** `interrupt_on` halts the graph before
`open_pull_request` and hands back the pending call. That pause outlives the
request that triggered it and, thanks to `persistence.py`, the process too — so
`POST /runs/{id}/approve` is a separate request that may land on a different
worker minutes later, and works because the state is in Postgres rather than in
whatever memory started it.

**Every run needs its own scratch space.** `workspaces.py` keys that off
`thread_id`, which is why the thread id is minted here, stored on the row, and
used as the identity of the run everywhere afterwards.

The one thing deliberately *not* here is a progress stream. `astream` would give
one, and a run that emits nothing for four minutes is a poor experience — but a
stream is a second way to observe a run that has to agree with the first, and
polling a status the database already holds is correct while a stream is merely
nicer. It is the obvious next increment, not a gap in this one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
from pydantic import BaseModel, Field

from repo_cartographer import runs as runs_table
from repo_cartographer.agent import (
    WORKSPACE,
    build_agent,
    pending_approval,
    run_config,
)
from repo_cartographer.auth import JWKS_ENV, current_user
from repo_cartographer.link_checker import DEFAULT_GUIDE_PATH
from repo_cartographer.persistence import connected
from repo_cartographer.workspaces import workspace_for

logger = logging.getLogger(__name__)

# Comma-separated origins for the Next.js frontend. Defaulted to the usual local
# dev origin rather than `*`, because `*` and credentialed requests are mutually
# exclusive in the CORS spec and the frontend sends an Authorization header.
CORS_ENV = "CORS_ORIGINS"
DEFAULT_ORIGINS = "http://localhost:3000"


class NewRun(BaseModel):
    # `^...$` rather than `\A...\Z`: pydantic v2 compiles this with Rust's
    # regex crate, which rejects those escapes outright at class-definition
    # time. `\w` cannot match a newline, so the anchors are equivalent here.
    repo: str = Field(min_length=3, max_length=140, pattern=r"^[\w.-]+/[\w.-]+$")
    question: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    message: str = Field(default="", max_length=1000)


def brief(repo: str, question: str) -> str:
    """What the orchestrator is actually asked.

    `repo` is a separate field rather than something parsed back out of the
    question, because the row needs it and guessing at it with a regular
    expression would be a second, worse source of truth. Restating it here means
    the agent is never left inferring which repository a question is about.
    """
    return f"{question}\n\nThe repository is the public GitHub repository {repo}."


CurrentUserId = Annotated[UUID, Depends(current_user)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the connection pool and the graph for the life of the process.

    Both are expensive and both are shared: one pool rather than one per request
    is the whole point of pooling, and `build_agent` constructs chat models and a
    middleware stack that would be wasteful to rebuild per call. The pool comes
    from `persistence.checkpointer`, and the same pool serves the `runs` table —
    on a free tier the connection budget is small enough that a second pool for
    two columns of bookkeeping would be a poor trade.
    """
    # Checked here rather than on the first request that needs it. A service that
    # starts happily and then answers every authenticated call with a 500 looks
    # like a bug in the frontend; one that refuses to start names the variable.
    if not os.environ.get(JWKS_ENV):
        raise RuntimeError(
            f"{JWKS_ENV} is not set, so no request could be authenticated. It is the"
            " project's JWKS endpoint:"
            " https://<ref>.supabase.co/auth/v1/.well-known/jwks.json"
        )

    async with connected() as (pool, saver):
        app.state.pool = pool
        app.state.agent = build_agent(checkpointer=saver)
        app.state.tasks = {}
        logger.info("repo-cartographer API ready")
        try:
            yield
        finally:
            # Runs in flight are abandoned rather than awaited: their state is
            # already checkpointed in Postgres after every superstep, so a
            # restart resumes them rather than losing them, and a shutdown that
            # waits several minutes for a mapping to finish is a shutdown that
            # gets SIGKILLed instead.
            # Snapshotted before cancelling, not iterated live. `spawn` attaches a
            # done-callback that removes a task from this dict as it finishes, so
            # cancelling while iterating mutates the thing being iterated —
            # `RuntimeError: dictionary changed size during iteration`, at
            # shutdown, where it is least welcome and hardest to reproduce.
            pending = list(app.state.tasks.values())
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="Repo Cartographer",
        summary="Reads a public GitHub repository and writes an onboarding guide.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.environ.get(CORS_ENV, DEFAULT_ORIGINS).split(",")
            if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    register(app)
    return app


def spawn(app: FastAPI, thread_id: str, coro: Any) -> None:
    """Run a coroutine in the background, keeping a reference to it.

    The reference matters: `asyncio` holds only a weak one to a running task, so
    a fire-and-forget `create_task` whose result nobody keeps can be garbage
    collected mid-flight. The registry is also what stops the same run being
    resumed twice concurrently.
    """
    task = asyncio.create_task(coro, name=thread_id)
    app.state.tasks[thread_id] = task
    task.add_done_callback(lambda _: app.state.tasks.pop(thread_id, None))


async def execute(app: FastAPI, thread_id: str, payload: Any) -> None:
    """Drive one run to its next stopping point and record where it stopped.

    `payload` is either an opening message or a `Command` resuming a paused run —
    the two cases are identical from here on, which is why they share this
    function: both may pause again, both may finish, and both have to leave the
    row describing what actually happened.
    """
    pool = app.state.pool
    try:
        state = await app.state.agent.ainvoke(payload, config=run_config(thread_id))
    except Exception as error:
        # Broad on purpose. This is the top of a background task: nothing is
        # waiting on it, so an exception that escapes here is logged by asyncio
        # and lost, leaving the row in `running` forever. Whatever went wrong,
        # the client's only way to find out is the status column.
        logger.exception("run %s failed", thread_id)
        await runs_table.set_status(
            pool,
            thread_id=thread_id,
            status=runs_table.FAILED,
            outcome=runs_table.Outcome(error=str(error)[:2000]),
        )
        return

    if pending_approval(state) is not None:
        await runs_table.set_status(
            pool, thread_id=thread_id, status=runs_table.AWAITING_APPROVAL
        )
        return

    guide = workspace_for(WORKSPACE, thread_id) / DEFAULT_GUIDE_PATH.lstrip("/")
    await runs_table.set_status(
        pool,
        thread_id=thread_id,
        status=runs_table.DONE,
        outcome=runs_table.Outcome(
            guide_path=str(guide) if guide.is_file() else None,
            answer=final_answer(state),
        ),
    )


def final_answer(state: dict[str, Any]) -> str | None:
    """The orchestrator's closing message, as prose.

    Not every run writes a guide — asked to explain a repository rather than to
    document it, the agent answers and writes no file. Before this was captured,
    such a run finished `done` with `guide_path` null and nothing anywhere for the
    client to read, having produced the answer and then discarded it.

    `.text` rather than `.content`, for the reason `agent.ask` gives: Gemini fills
    `content` with a list of typed blocks, so printing it yields a repr of that
    structure instead of the answer.
    """
    messages = state.get("messages") or []
    if not messages:
        return None
    text = getattr(messages[-1], "text", None)
    return str(text) if text else None


async def require(pool: Any, run_id: UUID, user_id: UUID) -> runs_table.Run:
    """Fetch a run or 404.

    Scoped to the caller, so another user's run id is indistinguishable from one
    that does not exist. A 403 would confirm the id was real, which is a small
    leak but a free one to avoid.
    """
    run = await runs_table.get(pool, user_id=user_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    return run


def register_runs(app: FastAPI) -> None:
    """Starting a run and looking at it."""

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Unauthenticated on purpose: a load balancer has no token."""
        return {"status": "ok"}

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start(body: NewRun, user_id: CurrentUserId) -> dict[str, Any]:
        thread_id = str(uuid4())
        run = await runs_table.create(
            app.state.pool,
            user_id=user_id,
            thread_id=thread_id,
            repo=body.repo,
            question=body.question,
        )
        opening = {"messages": [{"role": "user", "content": brief(body.repo, body.question)}]}
        spawn(app, thread_id, execute(app, thread_id, opening))
        # 202 rather than 201: the run is accepted and under way, and the thing
        # the client actually wants does not exist yet.
        return run.as_json()

    @app.get("/runs")
    async def index(user_id: CurrentUserId) -> list[dict[str, Any]]:
        runs = await runs_table.list_for(app.state.pool, user_id=user_id)
        return [run.as_json() for run in runs]

    @app.get("/runs/{run_id}")
    async def show(run_id: UUID, user_id: CurrentUserId) -> dict[str, Any]:
        return (await require(app.state.pool, run_id, user_id)).as_json()


def register_actions(app: FastAPI) -> None:
    """Reading the guide, and answering the approval gate."""

    @app.get("/runs/{run_id}/guide")
    async def guide(run_id: UUID, user_id: CurrentUserId) -> Response:
        run = await require(app.state.pool, run_id, user_id)
        path = workspace_for(WORKSPACE, run.thread_id) / DEFAULT_GUIDE_PATH.lstrip("/")
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No guide yet - this run is {run.status}.",
            )
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown")

    @app.post("/runs/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
    async def approve(run_id: UUID, body: Decision, user_id: CurrentUserId) -> dict[str, Any]:
        run = await require(app.state.pool, run_id, user_id)
        if run.status != runs_table.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This run is {run.status}; there is nothing waiting to be answered.",
            )
        if run.thread_id in app.state.tasks:
            # The row says `awaiting_approval` but a task is already driving this
            # thread, which means a second approval arrived while the first was
            # still running. Resuming twice would push two decisions into one
            # interrupt.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This run is already being resumed.",
            )

        decision: dict[str, Any] = {"type": body.decision}
        if body.decision == "reject":
            decision["message"] = body.message or "Declined."

        await runs_table.set_status(
            app.state.pool, thread_id=run.thread_id, status=runs_table.RUNNING
        )
        resume: Command[Any] = Command(resume={"decisions": [decision]})
        spawn(app, run.thread_id, execute(app, run.thread_id, resume))
        return {**run.as_json(), "status": runs_table.RUNNING}


def register(app: FastAPI) -> None:
    register_runs(app)
    register_actions(app)


app = create_app()
