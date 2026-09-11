"""Run the API.

    uv run python serve.py              # 127.0.0.1:8000
    uv run python serve.py --reload     # and restart when the code changes
    HOST=0.0.0.0 PORT=8080 uv run python serve.py

This exists because `uvicorn repo_cartographer.api:app` does not work on Windows,
and fails in a way that gives no hint why.

Uvicorn picks its event loop with a factory rather than with asyncio's policy —
`uvicorn.loops.asyncio.asyncio_loop_factory` returns `ProactorEventLoop` on
Windows — so the policy this project sets at import is never consulted. psycopg
cannot run in async mode on that loop, the pool retries each refusal silently, and
thirty seconds later the server exits with `PoolTimeout: couldn't get a connection
after 30.00 sec`, naming neither the loop nor the database.

The oddity worth knowing, because it makes the bug look intermittent: that same
factory returns a *compatible* loop whenever uvicorn is going to use a subprocess,
which `--reload` and `--workers` both do. So the command works in development and
fails the moment someone drops `--reload` to run it for real. This module removes
the coincidence by choosing the loop itself.

None of it applies to Linux, where the factory returns a selector loop either way,
and `uvicorn repo_cartographer.api:app` is fine.
"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn

APP = "repo_cartographer.api:app"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> int:
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    reload = "--reload" in sys.argv[1:]

    if reload:
        # Delegated rather than hand-rolled: reloading needs uvicorn's supervisor
        # process, which re-executes this target on every change. That path already
        # selects a compatible loop on every platform, so there is nothing to fix
        # here and reimplementing the supervisor to prove it would be worse.
        uvicorn.run(APP, host=host, port=port, reload=True)
        return 0

    server = uvicorn.Server(uvicorn.Config(APP, host=host, port=port))

    if sys.platform == "win32":
        # The whole point of the module. `SelectorEventLoop` explicitly, rather
        # than whatever the factory would have chosen.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()
        return 0

    asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
