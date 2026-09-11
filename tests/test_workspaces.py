"""Do two concurrent runs stay out of each other's files?

No model, no network. The bug this module guards against is not a crash and does
not show up in a transcript: two runs share a workspace, the second one's explorer
overwrites `/notes/overview.md`, and the first one's doc-writer reads it and
writes a confident, well-cited guide about the wrong repository. Nothing raises.
The only place that failure is visible is in a test that runs two threads on
purpose and looks at where the bytes went.

The paths make it certain rather than likely. `prompts.py` hands every agent a
*fixed* convention — `/notes/<scope>.md`, `/guide.md` — so a collision is not an
unlucky filename clash, it is the design working exactly as written in two runs at
once.

What is checked here, in order of how badly it fails if wrong:

1. Two threads writing the same virtual path get two different real files.
2. Every agent within *one* run still shares a workspace — the thing that must
   NOT be isolated, since the workspace is how an explorer hands notes to the
   doc-writer.
3. A hostile `thread_id` cannot escape the workspace root.
4. Outside a run there is still somewhere to write, because the test suite and
   the scripts build backends with no thread at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from repo_cartographer.workspaces import (
    UNSCOPED,
    ThreadScopedBackend,
    current_thread_id,
    safe_segment,
    workspace_for,
)


class State(TypedDict):
    written: list[str]


def run_in_graph(backend: ThreadScopedBackend, thread: str, path: str, body: str) -> str:
    """Write one file through the backend from inside a real LangGraph run.

    The whole mechanism under test is that the backend reads `thread_id` off
    LangGraph's runtime context, so the test has to *be* a run rather than mock
    one. A one-node graph is the smallest thing that establishes that context,
    and it costs no model call.
    """

    def node(state: State) -> State:
        backend.write(path, body)
        return {"written": [*state["written"], path]}

    graph = StateGraph(State)
    graph.add_node("write", node)
    graph.add_edge(START, "write")
    graph.add_edge("write", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    compiled.invoke({"written": []}, config={"configurable": {"thread_id": thread}})
    return thread


@pytest.fixture
def backend(tmp_path: Path) -> ThreadScopedBackend:
    return ThreadScopedBackend(root_dir=tmp_path)


def test_two_runs_writing_the_same_path_do_not_collide(
    backend: ThreadScopedBackend, tmp_path: Path
) -> None:
    """The bug itself: same virtual path, two threads, two real files."""
    run_in_graph(backend, "thread-a", "/notes/overview.md", "flask")
    run_in_graph(backend, "thread-b", "/notes/overview.md", "requests")

    a = tmp_path / "thread-a" / "notes" / "overview.md"
    b = tmp_path / "thread-b" / "notes" / "overview.md"
    assert a.read_text(encoding="utf-8") == "flask"
    assert b.read_text(encoding="utf-8") == "requests"


def test_one_run_shares_one_workspace(backend: ThreadScopedBackend, tmp_path: Path) -> None:
    """The property that must survive isolation.

    An explorer writes a note and the doc-writer — a different agent, in a
    different message thread — reads it back. Both run under the orchestrator's
    `thread_id`, so isolating per *run* must not isolate per *agent*. This is the
    check that would fail if the scope were ever narrowed to something finer.
    """
    run_in_graph(backend, "shared", "/notes/src.md", "the explorer was here")

    def reader(state: State) -> State:
        content = backend.read("/notes/src.md")
        assert "explorer" in str(content)
        return state

    graph = StateGraph(State)
    graph.add_node("read", reader)
    graph.add_edge(START, "read")
    graph.add_edge("read", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    compiled.invoke({"written": []}, config={"configurable": {"thread_id": "shared"}})

    assert (tmp_path / "shared" / "notes" / "src.md").is_file()
    assert not (tmp_path / "notes").exists(), "wrote to the shared root, not the run's"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc",
        "../sibling",
        "/absolute",
        "with/slash",
        "",
        "." * 200,
    ],
)
def test_a_hostile_thread_id_cannot_escape(hostile: str, tmp_path: Path) -> None:
    """`thread_id` is caller-supplied on resume, so it is untrusted input.

    A workspace name is a path segment. Without this, resuming a run under the id
    `../../etc` would point an agent holding `write_file` at somewhere outside the
    workspace entirely.
    """
    segment = safe_segment(hostile)
    assert "/" not in segment
    assert "\\" not in segment
    assert ".." not in segment

    resolved = workspace_for(tmp_path, hostile).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())


def test_distinct_threads_get_distinct_directories(tmp_path: Path) -> None:
    assert workspace_for(tmp_path, "a") != workspace_for(tmp_path, "b")


def test_outside_a_run_there_is_still_somewhere_to_write(
    backend: ThreadScopedBackend, tmp_path: Path
) -> None:
    """Tests and scripts build a backend with no run in progress.

    `get_config()` raises outside a runnable context, which is a legitimate state
    here rather than an error — so it resolves to a named directory instead. A
    named one rather than the root itself, so that "no thread" is visible in a
    listing instead of being a pile of loose files among the per-run directories.
    """
    assert current_thread_id() is None
    backend.write("/loose.md", "no thread")
    assert (tmp_path / UNSCOPED / "loose.md").read_text(encoding="utf-8") == "no thread"


def test_the_composite_backend_is_thread_scoped() -> None:
    """The wiring: `build_backend` must hand back the scoped backend, not a plain one.

    Asserted because the isolation is one keyword in `skills.py`, and reverting it
    would leave every other test in this module passing — they build the backend
    directly.
    """
    from repo_cartographer.agent import WORKSPACE
    from repo_cartographer.skills import build_backend

    composite: Any = build_backend(WORKSPACE)
    assert isinstance(composite.default, ThreadScopedBackend)
