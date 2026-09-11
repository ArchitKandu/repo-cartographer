"""Where a run's files live, and why each run needs its own.

Until now there was one workspace directory and every run shared it. For a CLI
that is not a bug — one person asks one question at a time, and finding the last
run's notes still sitting there is arguably a feature. The moment two runs
overlap it is a data-corruption bug of the worst kind, because the paths the
agents use are *conventions* rather than accidents: `prompts.py` tells the
explorer to write `/notes/<scope>.md` and tells the doc-writer to read exactly
that, and `link_checker.py` defaults to `/guide.md`. Two concurrent runs do not
collide occasionally on a coincidental filename. They collide every time, on
purpose, because both were told to use the same one — and the failure is silent:
the doc-writer reads notes about somebody else's repository and writes a
confident guide from them.

The fix has to keep one property that Phase 4 depends on. The workspace is not
scratch space, it is the *channel* between the orchestrator, the explorers and
the doc-writer: an explorer writes a note and a different agent, running in a
different message thread, reads it back. So the isolation has to be per *run*,
not per agent — all three have to land in the same directory, and only a
different run may land somewhere else.

`thread_id` is exactly that boundary. LangGraph already scopes a run by it, the
checkpointer already keys state on it, and sub-agents inherit it from the parent
rather than minting their own. So the workspace root is derived from it, at the
moment a file operation happens rather than when the graph is built — which is
the only option available, since the graph is built once at import and the thread
is not chosen until somebody invokes it.
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from langgraph.config import get_config

# Thread ids are uuid4 in every path this repository controls, but one of them —
# resuming a paused run — takes the id from whoever is asking. A workspace name is
# a path segment, so an id like `../../etc` would be a directory traversal with a
# a very short route to somewhere it should not reach. Anything that is not
# plainly safe is replaced by a hash of itself rather than rejected: the id still
# maps to one stable directory, it just stops being a name of the caller's
# choosing.
_SAFE_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Where a file operation goes when there is no run in progress — the test suite
# building a backend to inspect it, or a script reading a guide after the fact.
# A named directory rather than the base itself, so that "no thread" is a visible
# state in the directory listing rather than a pile of loose files among the
# per-run ones.
UNSCOPED = "_unscoped"


def safe_segment(thread_id: str) -> str:
    """A single path segment that stands for this thread and cannot escape."""
    if _SAFE_SEGMENT.match(thread_id):
        return thread_id
    return "t-" + sha256(thread_id.encode("utf-8")).hexdigest()[:32]


def current_thread_id() -> str | None:
    """The thread of the run executing right now, or `None` outside a run.

    `get_config()` reads LangGraph's contextvar, which is set for the duration of
    a node and inherited by everything the node calls — tools, middleware, and
    sub-agents, which is the property this whole module rests on. Outside a run
    there is no context and it raises, which is not an error here: a test or a
    script is allowed to open the backend and look around.
    """
    try:
        config = get_config()
    except RuntimeError:
        return None
    thread = (config.get("configurable") or {}).get("thread_id")
    return str(thread) if thread else None


class ThreadScopedBackend(FilesystemBackend):
    """A `FilesystemBackend` rooted at the current run's own directory.

    Subclasses rather than wraps for the same reason `ReadOnlyBackend` in
    `skills.py` does: `CompositeBackend` inspects the backend *class* to decide
    how to call it, so a delegating proxy would satisfy every call at run time and
    fail that inspection.

    Every path the base class touches goes through `self.cwd`, so overriding that
    one attribute redirects reads, writes, `ls`, `grep` and `glob` together. It has
    to be a property rather than a value because the run is not known when the
    backend is constructed — the graph is built once at import, and `thread_id`
    arrives per invocation.
    """

    @property
    def cwd(self) -> Path:
        thread = current_thread_id()
        root = self._base / (UNSCOPED if thread is None else safe_segment(thread))
        # Created on access rather than up front, because "every thread that will
        # ever exist" is not a knowable set. The cost is one `mkdir` syscall per
        # file operation against a directory that already exists.
        root.mkdir(parents=True, exist_ok=True)
        return root

    @cwd.setter
    def cwd(self, value: str | Path) -> None:
        # `FilesystemBackend.__init__` assigns `self.cwd`, which would fail against
        # a read-only property. That assignment is what establishes the base, so it
        # is accepted here and stored as one.
        self._base = Path(value).resolve()


def workspace_for(base: Path, thread_id: str | None) -> Path:
    """The directory a given run's files are in, for callers outside the graph.

    The backend above resolves this for itself while a run is in progress. This is
    for the code that has to look afterwards — a server serving the finished guide,
    a cleanup job, a script inspecting what a run produced.
    """
    return base / (UNSCOPED if thread_id is None else safe_segment(thread_id))


def prune(base: Path, keep: set[str]) -> list[Path]:
    """Delete every run directory whose thread is not in `keep`.

    Not called from anywhere yet. It exists because the per-run directories this
    module creates are the thing that makes an unbounded mess on a server, and the
    tidy-up belongs next to the code that makes the mess rather than being
    rediscovered later.
    """
    import shutil

    removed = []
    wanted = {safe_segment(thread) for thread in keep} | {UNSCOPED}
    for child in base.iterdir():
        if child.is_dir() and child.name not in wanted:
            shutil.rmtree(child)
            removed.append(child)
    return removed


def describe(backend: Any) -> str:
    """A one-line summary of where a backend is pointing, for scripts."""
    base = getattr(backend, "_base", None)
    thread = current_thread_id()
    return f"base={base} thread={thread or '(none)'}"
