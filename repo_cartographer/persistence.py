"""Durable checkpointing, for runs that outlive the process that started them.

`agent.py` builds with an `InMemorySaver`, and for `uv run main.py` that is the
right choice: a run pauses at the approval gate and is answered seconds later by
the same Python process, so the state never has to leave memory. A deployment
breaks that assumption twice over. The gate at `interrupt_on` spans two HTTP
requests with a human's attention span in between, and any restart, redeploy or
second replica in that window would lose a run that is sitting there waiting to
be answered — not fail it, lose it, because an in-memory thread id that no longer
resolves is indistinguishable from one that never existed.

So this module is the other half of the sentence in `build_agent`: "a deployment
that survives a restart wants a real one." It hands back an `AsyncPostgresSaver`
over a pooled connection, with the three pieces of Supabase-specific wiring that
are needed to make that work and are not discoverable from the failure they
cause. Each is commented where it happens.

    from repo_cartographer.persistence import checkpointer

    async with checkpointer() as saver:
        cartographer = build_agent(checkpointer=saver)
        state = await cartographer.ainvoke(..., config=run_config(thread_id))

Async rather than sync deliberately. The sync `PostgresSaver` exists and would
have left `map_repo` and `ask` untouched, but the thing this is for is a web
server holding open a run that takes minutes, and that server is async. Keeping
one saver rather than two means there is one set of connection semantics to
reason about. The CLI path keeps its `InMemorySaver` and is unaffected.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

# The environment variable holding the connection string. Named here rather than
# inlined because the error below is the first thing anyone deploying this will
# read, and it should name the thing they have to go and set.
URI_ENV = "POSTGRESQL_URI"

# Checkpoint tables live in their own schema rather than in `public`.
#
# This is a Supabase-specific precaution with a general reason behind it.
# `public` is exposed through the Data API, so a table there is reachable over
# HTTPS by the `anon` and `authenticated` roles the moment they are granted
# access — and a LangGraph checkpoint is not a bland bookkeeping row. It holds
# the serialised state of a run: the whole message thread, every tool result that
# was not evicted, whatever the explorer read. Putting that behind a schema the
# Data API does not expose removes the question rather than answering it with an
# RLS policy that has to stay correct forever.
SCHEMA = "langgraph"

# `autocommit` is required by the saver's own `setup()`, which issues DDL and
# cannot run inside psycopg's default implicit transaction.
#
# `prepare_threshold=0` disables named prepared statements. It costs nothing on
# the session pooler (port 5432), where a connection is held for the whole
# session and a named statement stays valid — but it is what would keep this
# working if the URI were ever pointed at the transaction pooler (6543), where
# connections are handed back between statements and a named statement prepared
# on one shows up missing on the next. That failure appears under load, as an
# intermittent `prepared statement "_pg3_0" does not exist`, which is a bad way
# to find out. One keyword now is cheaper.
_CONNECT_KWARGS: dict[str, Any] = {"autocommit": True, "prepare_threshold": 0}


# psycopg's async mode cannot drive the ProactorEventLoop that Python selects by
# default on Windows, and the way it says so is worth knowing because it says it
# to nobody: the pool catches the `InterfaceError` from each failed connection
# attempt and retries, so what a developer sees is `PoolTimeout: couldn't get a
# connection after 30.00 sec` half a minute later, naming nothing.
#
# This runs at import rather than inside `checkpointer()`, which is where it was
# first written and where it does not work. A policy only decides which loop
# `asyncio.run()` *creates*; by the time a coroutine is running to set it, the
# Proactor loop it was meant to prevent already exists and is already the one
# psycopg will be handed. Import happens before any of that.
#
# Linux deployment never reaches this branch. It is here so local development on
# Windows does not open with a thirty-second timeout that points at nothing.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def _configure(conn: psycopg.AsyncConnection[Any]) -> None:
    """Put a newly opened pooled connection into the checkpoint schema.

    The obvious way to do this is on the connection string —
    `?options=-csearch_path%3Dlanggraph` — and against Supabase it silently does
    not work. Supavisor, the pooler, drops the `options` startup parameter, so
    the connection comes back on the default `"$user", public, extensions` and
    nothing raises to say so. `setup()` would then create its tables in `public`:
    the exact schema `SCHEMA` exists to stay out of, with no error anywhere to
    suggest the precaution had failed.

    Running it as a statement per connection is immune to that, because it is an
    ordinary query rather than something the pooler has to forward at handshake
    time. `configure` fires once per physical connection, and in session mode the
    `SET` then holds for every checkout that reuses it.
    """
    await conn.execute(sql.SQL("set search_path to {}, public").format(sql.Identifier(SCHEMA)))


@asynccontextmanager
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """A Postgres-backed saver and the connection pool underneath it.

    A context manager because the pool is a resource with a lifetime: it opens
    connections eagerly and must be closed, which in a web application means
    binding it to the server's startup and shutdown rather than to a function
    call. FastAPI's `lifespan` is the intended caller.

    `setup()` and the `create schema` before it are both idempotent, so this is
    safe to run on every boot and there is no separate migration step to forget.
    """
    try:
        uri = os.environ[URI_ENV]
    except KeyError:
        raise RuntimeError(
            f"{URI_ENV} is not set. Durable checkpointing needs a Postgres "
            "connection string — on Supabase, use the *session pooler* URI from "
            "Dashboard -> Connect, not the direct `db.<ref>.supabase.co` one, "
            "which resolves on IPv6 only and is unreachable from most hosts."
        ) from None

    async with AsyncConnectionPool(
        conninfo=uri,
        # `min_size` is stated because psycopg's default is 4, which both holds
        # four connections open against a shared pooler for an idle server and
        # makes any `max_size` below it raise rather than clamp.
        min_size=1,
        max_size=10,
        kwargs=_CONNECT_KWARGS,
        configure=_configure,
        open=False,
    ) as pool:
        await pool.open()
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("create schema if not exists {}").format(sql.Identifier(SCHEMA))
            )
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
