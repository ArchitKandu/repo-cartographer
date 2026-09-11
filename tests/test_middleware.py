"""A failed tool call is information, and for a while it was a traceback.

Both prompts promise the agents that a bad path teaches them something — *"when
`get_file_contents` reports a directory, a binary, or a 404, that tells you
something about the path"* — and `tools.py` was written for it, every error
message naming the path and often the right one instead. None of it reached the
model. LangGraph's default tool-error handler returns a message for a schema
error and re-raises everything else, so one 404 on one file in one explorer ended
a four-agent, thirty-request run with a stack trace.

The worse half is where it died: at the point where the system was working
correctly. A 404 means the model asked for a path that is not there, which is
exactly the case the prompts coach it through.

So the tests here come in two kinds. The plain ones check that a documented tool
failure becomes a `ToolMessage` addressed to the call that failed. The one that
matters checks the opposite — that a `TypeError` from a defect in this project
still ends the run, because a middleware that catches everything converts bugs
into paragraphs the agent politely works around, and then a broken run and a good
one produce the same shape of answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from repo_cartographer.middleware import (
    REPORTED_TOOL_ERRORS,
    UNUSED_BUILTIN_TOOLS,
    SurfaceToolErrorsMiddleware,
)
from repo_cartographer.tools import GitHubError

# --------------------------------------------------------------------------- #
# A failed tool call is information — the middleware that makes that true
# --------------------------------------------------------------------------- #


class _ToolCallRequest:
    """The one field `SurfaceToolErrorsMiddleware` reads off a tool call."""

    def __init__(self, name: str = "get_file_contents", call_id: str = "call_7") -> None:
        self.tool_call = {"name": name, "id": call_id, "args": {}}


def _raise(exc: Exception) -> Any:
    def handler(_request: Any) -> Any:
        raise exc

    return handler


def _surface(request: Any, handler: Any) -> Any:
    """One tool call through the middleware.

    The `Any` is for the stand-in request above: mypy is right that it is not a
    `ToolCallRequest`, and it is deliberately not one — a real one needs a state
    and a runtime this middleware never touches.
    """
    middleware: Any = SurfaceToolErrorsMiddleware()
    return middleware.wrap_tool_call(request, handler)


@pytest.mark.parametrize(
    "exc",
    [
        GitHubError("Failed to fetch file contents: 404 - Not Found"),
        ValueError("'src/requests' is a directory, not a file."),
    ],
)
def test_a_documented_tool_failure_becomes_a_message(exc: Exception) -> None:
    """The whole point, and it is what the prompts already promised.

    Both prompts say "a failed tool call is information ... fix the path, do not
    retry it unchanged". Until this middleware existed they were describing
    something that could not happen: LangGraph's default handler returns a
    message for a schema error and re-raises everything else, so a single 404 in
    one explorer ended a four-agent run with a traceback.
    """
    message = _surface(_ToolCallRequest(), _raise(exc))

    assert isinstance(message, ToolMessage)
    assert message.status == "error"
    assert str(exc) in message.content


def test_the_message_is_addressed_to_the_call_that_failed() -> None:
    """A tool result with the wrong id is not a tool result.

    The model matches results to its own calls by `tool_call_id`; a mismatch is a
    thread the provider will reject or, worse, an answer attached to the wrong
    question.
    """
    request = _ToolCallRequest(name="get_repo_tree", call_id="call_42")
    message = _surface(request, _raise(GitHubError("no")))

    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call_42"
    assert message.name == "get_repo_tree"


def test_a_bug_in_our_own_code_still_ends_the_run() -> None:
    """The line this middleware must not cross.

    A `TypeError` is a defect, not information. Handed to the model as a tool
    message it becomes a paragraph the agent politely works around, and the run
    finishes with a plausible answer and no sign anything broke — which is the
    failure shape this whole codebase is built to avoid.
    """
    with pytest.raises(TypeError):
        _surface(_ToolCallRequest(), _raise(TypeError("a defect")))


def test_a_successful_call_passes_straight_through() -> None:
    sentinel = ToolMessage(content="ok", tool_call_id="call_7")
    assert _surface(_ToolCallRequest(), lambda _r: sentinel) is sentinel


def test_it_catches_what_the_tools_layer_actually_raises() -> None:
    """Asserted against `tools.py`'s own documented contract, not a copy of it.

    Every function there raises `GitHubError` (the API said no, or could not be
    reached) or `ValueError` (the argument pointed somewhere unreadable). If a
    third kind is ever added, this is where the omission should show up.
    """
    assert set(REPORTED_TOOL_ERRORS) == {GitHubError, ValueError}
    assert Exception not in REPORTED_TOOL_ERRORS, "catching Exception would hide defects"


def test_both_agents_holding_github_tools_surface_their_errors() -> None:
    """On the explorer *and* the orchestrator, and outermost on each.

    Outermost is the load-bearing part: `wrap_tool_call` composes with the first
    middleware in the list outermost, and an exception caught anywhere further in
    has already unwound past `FilesystemMiddleware`. The orchestrator needs it for
    `get_repo_scopes` and `open_pull_request`; the doc-writer cannot reach GitHub
    at all, so it has nothing to surface.
    """
    from deepagents.backends import FilesystemBackend

    from repo_cartographer.agent import (
        TOOL_RESULT_TOKEN_LIMIT,
        WORKSPACE,
        build_subagents,
    )

    specs = {
        str(spec["name"]): spec
        for spec in build_subagents(
            FilesystemBackend(root_dir=WORKSPACE),
            tool_result_token_limit=TOOL_RESULT_TOKEN_LIMIT,
        )
    }
    explorer: Any = dict(specs["explorer"]).get("middleware") or []
    assert explorer[0].name == "SurfaceToolErrorsMiddleware", "not outermost on the explorer"

    doc_writer: Any = dict(specs["doc-writer"]).get("middleware") or []
    assert not any(m.name == "SurfaceToolErrorsMiddleware" for m in doc_writer)


def test_the_orchestrator_surfaces_them_too_asserted_on_the_built_graph() -> None:
    """The orchestrator's list is built inside `build_agent`, so this asks the graph.

    Reaching for `ToolNode._wrap_tool_call` is reaching into the library, which
    `tests/test_wiring.py` also does where our intent and the library's behaviour
    have to be checked separately. It is worth it here: the composed chain is the
    thing that either catches or does not, and asserting a position in a list
    would pass while a later middleware quietly swallowed the exception first.

    The orchestrator holds `get_repo_scopes` and `open_pull_request`, and both
    raise `GitHubError` — at a repository that does not exist, a branch that
    cannot be created, a spent quota.
    """
    from repo_cartographer.agent import build_agent

    tool_node: Any = build_agent().nodes["tools"].bound
    message = tool_node._wrap_tool_call(
        _ToolCallRequest(name="get_repo_scopes", call_id="call_9"),
        _raise(GitHubError("Failed to fetch repository tree: 404 - Not Found")),
    )

    assert isinstance(message, ToolMessage)
    assert message.status == "error"
    assert message.tool_call_id == "call_9"


def test_the_restricted_tool_set_is_still_what_it_was() -> None:
    """This module gained a second concern; the first one is still asserted.

    `UNUSED_BUILTIN_TOOLS` is checked in full by `tests/test_wiring.py`, against
    the compiled orchestrator rather than against this constant. Named here only
    so that the two middleware in one module do not read as one.
    """
    assert "execute" in UNUSED_BUILTIN_TOOLS
    assert "task" not in UNUSED_BUILTIN_TOOLS, "sub-agents are the point of Phase 4"
