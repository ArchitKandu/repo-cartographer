"""Phase 4's structural checks: is the work actually split the way we claim?

No model is called here. Every assertion is about the shape of the graph, which
makes these tests the cheapest useful thing in the project — they run in about a
second, cost nothing against a daily request quota, and they fail for exactly one
reason: the wiring is not what `agent.py` says it is.

That matters because Phase 4's central guarantee is a *capability* claim, not a
behavioural one. "The doc-writer cannot cite a file nobody read" is true only
while `"tools": []` is in its spec, and nothing in a normal run would tell you if
that key went missing — you would get a plausible guide with invented paths and no
error anywhere. Same for the two failure modes the library makes easy: a spec that
omits `tools` silently inherits the parent's, and a sub-agent that does not restate
`FilesystemMiddleware` silently gets the full built-in suite back.

Two layers, on purpose:

**Spec-level** (most of the file) asserts against what `build_subagents` returns —
dicts and middleware instances this project owns. Stable across deepagents
upgrades.

**Compiled-graph level** (`test_compiled_*`) asserts the library actually honoured
those specs, which is the part no amount of checking our own dicts can establish.
It reaches into the `task` tool's closure to do it, so it is the test most likely
to break on an upgrade. If it ever does, that is a signal worth reading rather
than a test worth deleting: it means the mechanism the design rests on has moved.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from repo_cartographer.link_checker import DEFAULT_GUIDE_PATH
from repo_cartographer.prompts import (
    DOC_WRITER_PROMPT,
    EXPLORER_PROMPT,
    ORCHESTRATOR_PROMPT,
)

if TYPE_CHECKING:
    from deepagents import SubAgent

# Importing repo_cartographer.agent builds a chat model, which needs a provider
# key — see the note in `__init__.py` about why `agent` is not re-exported. Without
# one, these tests skip rather than fail: the wiring they check is worth checking
# in CI, but not at the price of making a key a hard requirement for the suite.
_HAS_KEY = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
needs_model = pytest.mark.skipif(not _HAS_KEY, reason="no provider key configured")

# What each agent is supposed to end up holding. Written out here rather than
# derived from the code, so a change to the split has to be made twice — once in
# `agent.py` and once here, deliberately.
GITHUB_TOOLS = {"get_repo_tree", "get_file_contents", "search_code"}

# The tools that read what a repository *says*. `get_repo_scopes` is deliberately
# not in here: it reports directory names and file counts, which is shape, not
# content. That distinction is the one 4b rests on, so it gets its own name.
CONTENT_TOOLS = GITHUB_TOOLS
EXPECTED = {
    # The orchestrator divides, delegates and checks. `get_repo_scopes` is how it
    # learns a repository has a `src/` worth exploring; everything about what is
    # *in* src/ comes back through `task`.
    "orchestrator": {
        "get_repo_scopes",
        "open_pull_request",
        "ls",
        "read_file",
        "task",
        "write_todos",
    },
    "explorer": GITHUB_TOOLS | {"read_file", "write_file"},
    # Still the whole design in one line: three workspace tools and no repository
    # access. `write_file` arrived at Phase 6 so the guide becomes a file the
    # link-checker can read; it does not touch the blindfold, because the
    # workspace is not GitHub.
    "doc-writer": {"ls", "read_file", "write_file"},
}

# The delegates that are language models, which since Phase 6 is not all of them.
# `link-checker` is a CompiledSubAgent — a graph, no prompt, no tools, no model —
# so every assertion below about prompts and tool sets would be meaningless for
# it, and a spec-shaped test that silently skipped it would be worse than one
# that names the distinction.
MODEL_SUBAGENTS = ("explorer", "doc-writer")

# Every tool name that exists anywhere in the system: the deepagents built-ins
# that could show up if a restriction stopped working, plus this project's own.
# Used to check prompts against reality — a prompt that names one of these is
# telling its agent about a tool it may not have.
ALL_TOOL_NAMES = {
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
    "task",
    "write_todos",
    "get_repo_scopes",
    "open_pull_request",
    *GITHUB_TOOLS,
}


@pytest.fixture(scope="module")
def specs() -> list[SubAgent]:
    """The sub-agent specs, built against a throwaway backend."""
    from deepagents.backends import FilesystemBackend

    # The backend is never written through here — only handed to middleware so it
    # can be constructed — so pointing it at the real workspace creates nothing.
    from repo_cartographer.agent import (
        TOOL_RESULT_TOKEN_LIMIT,
        WORKSPACE,
        build_subagents,
    )

    return build_subagents(
        FilesystemBackend(root_dir=WORKSPACE),
        tool_result_token_limit=TOOL_RESULT_TOKEN_LIMIT,
    )


def _by_name(specs: list[SubAgent]) -> dict[str, SubAgent]:
    return {spec["name"]: spec for spec in specs}


def _fs_middleware(spec: SubAgent) -> Any:
    """The spec's FilesystemMiddleware, or None if it forgot to restate one."""
    return next(
        (m for m in spec.get("middleware", []) if m.name == "FilesystemMiddleware"),
        None,
    )


def _workspace_tools(spec: SubAgent) -> set[str]:
    fs = _fs_middleware(spec)
    return {t.name for t in fs.tools} if fs else set()


def _declared_tools(spec: SubAgent) -> set[str]:
    return {getattr(t, "name", None) or t.__name__ for t in spec.get("tools", [])}


# --------------------------------------------------------------------------- #
# Spec level: our own dicts, stable across library upgrades.
# --------------------------------------------------------------------------- #


def test_exactly_three_subagents(specs: list[SubAgent]) -> None:
    assert set(_by_name(specs)) == {"explorer", "doc-writer", "link-checker"}


def test_the_link_checker_holds_no_model(specs: list[SubAgent]) -> None:
    """Phase 6's claim, asserted at the only place it could stop being true.

    A `CompiledSubAgent` carries a `runnable` and nothing else; a `SubAgent`
    carries a prompt and tools and gets a model built for it. If someone ever
    "fixed" the link-checker by giving it a system prompt, the citation check
    would quietly become an opinion — it would still return verdicts, they would
    still look like verdicts, and they would sometimes be wrong for reasons no
    test downstream could see. So the absence of a prompt is the assertion.
    """
    checker = _by_name(specs)["link-checker"]
    assert "runnable" in checker
    assert not {"system_prompt", "tools", "model"} & set(checker)
    # And it really is a graph that can be invoked, not a placeholder.
    assert hasattr(checker["runnable"], "invoke")


def test_every_spec_declares_tools_explicitly(specs: list[SubAgent]) -> None:
    """A spec that omits `tools` inherits the parent's, whatever those happen to be.

    The orchestrator's list is empty as of Phase 4, so the omission would leave a
    sub-agent with nothing and no error. Before Phase 4 the same omission would
    have handed it the GitHub tools. Both are wrong and neither announces itself,
    so the key is required rather than defaulted.
    """
    for name in MODEL_SUBAGENTS:
        spec = _by_name(specs)[name]
        assert "tools" in spec, f"{name} would inherit the orchestrator's tools"


def test_doc_writer_cannot_reach_github(specs: list[SubAgent]) -> None:
    """The guarantee the design rests on, asserted at the only place it is true."""
    doc_writer = _by_name(specs)["doc-writer"]
    assert doc_writer["tools"] == []
    assert not _declared_tools(doc_writer) & GITHUB_TOOLS


def test_explorer_holds_exactly_the_github_tools(specs: list[SubAgent]) -> None:
    assert _declared_tools(_by_name(specs)["explorer"]) == GITHUB_TOOLS


def test_each_spec_restates_filesystem_middleware(specs: list[SubAgent]) -> None:
    """Without this, a sub-agent gets the full built-in suite back.

    Parent middleware is not inherited by declarative sub-agents, so
    `RestrictToolsMiddleware` — which keeps `execute`, `glob`, `grep` and `delete`
    away from the orchestrator — does not reach either delegate. Each spec has to
    narrow its own, and the narrowing is what this asserts.
    """
    for name in MODEL_SUBAGENTS:
        spec = _by_name(specs)[name]
        assert _fs_middleware(spec) is not None, f"{name} would get every built-in"


def test_workspace_tools_match_the_intended_split(specs: list[SubAgent]) -> None:
    for name in MODEL_SUBAGENTS:
        expected = EXPECTED[name] - GITHUB_TOOLS
        assert _workspace_tools(_by_name(specs)[name]) == expected


def test_no_subagent_can_run_a_shell_or_delete_notes(specs: list[SubAgent]) -> None:
    """Stated separately from the set comparison because it is the consequence.

    `execute` has no sandbox to run in and only returns an error; `delete` points
    at the notes that are a run's durable output. Neither belongs anywhere in this
    system, and a set equality above passing is easy to misread as covering it.
    """
    for name in MODEL_SUBAGENTS:
        spec = _by_name(specs)[name]
        held = _workspace_tools(spec) | _declared_tools(spec)
        assert not held & {"execute", "delete", "glob", "grep"}, f"{name} holds one"


def test_explorer_keeps_phase_3s_eviction_threshold(specs: list[SubAgent]) -> None:
    """The explorer is the only agent that reads files, so it needs the threshold.

    Sub-agents get a fresh FilesystemMiddleware at the library default of 20_000
    tokens unless the spec says otherwise, which would leave Phase 3's tuned limit
    installed on the two agents that no longer read anything and absent from the
    one that does — offloading silently switched off where it matters.
    """
    from repo_cartographer.agent import TOOL_RESULT_TOKEN_LIMIT

    fs = _fs_middleware(_by_name(specs)["explorer"])
    # Private attribute: there is no public reader, and the alternative is trusting
    # a keyword argument we cannot observe. If an upgrade renames it, this fails
    # loudly, which is the outcome we want.
    assert fs._tool_token_limit_before_evict == TOOL_RESULT_TOKEN_LIMIT


# --------------------------------------------------------------------------- #
# Prompt level: do the prompts describe the tools their agent actually has?
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("agent_name", "prompt"),
    [
        ("orchestrator", ORCHESTRATOR_PROMPT),
        ("explorer", EXPLORER_PROMPT),
        ("doc-writer", DOC_WRITER_PROMPT),
    ],
)
def test_prompt_names_no_tool_its_agent_lacks(agent_name: str, prompt: str) -> None:
    """Catches the drift a wiring change makes but a prompt change forgets.

    Narrowing a sub-agent's tools is one line; the prompt that still tells it to
    `ls` the workspace is somewhere else entirely. The model then spends a turn on
    a tool it was promised and does not have, and the transcript reads like a model
    failure rather than a documentation one.

    Both spellings count: the prompts introduce some tools bare (`` `ls` ``) and
    others with their signature (`` `get_repo_scopes(owner, repo)` ``), and a check
    that only caught the first would quietly pass on every tool documented properly.
    """
    named = {
        name for name in ALL_TOOL_NAMES if f"`{name}`" in prompt or f"`{name}(" in prompt
    }
    assert named, f"{agent_name} prompt names no tools at all — is the regex right?"
    assert named <= EXPECTED[agent_name], f"{agent_name} prompt over-promises"


def test_the_notes_handoff_is_stated_on_both_sides() -> None:
    """The one convention no code enforces: where notes go.

    The orchestrator picks the path and puts it in the brief; the explorer writes
    there; the doc-writer reads it. Nothing in the graph checks any of that, so if
    the orchestrator's prompt stops naming a path the explorer has nothing to write
    to and the run degrades quietly.
    """
    assert "/notes/" in ORCHESTRATOR_PROMPT
    assert "the path your brief gives you" in EXPLORER_PROMPT


def test_the_guide_handoff_is_stated_on_both_sides() -> None:
    """Phase 6's convention, and it is agreed by two prompts and one module.

    The orchestrator tells the doc-writer to save the guide at `/guide.md` and
    then briefs the link-checker to read that same path; `link_checker.py` falls
    back to it when a brief names none. Nothing in the graph enforces the
    agreement. If one side drifts, the doc-writer writes a file nobody reads and
    the checker finds nothing to check — and "nothing to check" must never be
    mistaken for "nothing wrong", which is why the fallback in `link_checker.py`
    is pinned here alongside the prompts rather than trusted.
    """
    assert DEFAULT_GUIDE_PATH == "/guide.md"
    assert DEFAULT_GUIDE_PATH in ORCHESTRATOR_PROMPT
    assert DEFAULT_GUIDE_PATH in DOC_WRITER_PROMPT


# --------------------------------------------------------------------------- #
# Compiled-graph level: did the library honour any of the above?
# --------------------------------------------------------------------------- #


def _tool_node_names(graph: Any) -> set[str]:
    node = graph.nodes["tools"]
    return set(getattr(node, "bound", node).tools_by_name)


def _task_tool(graph: Any) -> Any:
    node = graph.nodes["tools"]
    return getattr(node, "bound", node).tools_by_name["task"]


def _menu_names(description: str) -> set[str]:
    """The agent names the `task` tool offers, from its "Available agent types" list.

    Scoped to the menu rather than matched against the whole description, because
    the description's fixed usage notes mention `general-purpose` by name whether or
    not it is enabled — a substring check on the full text can never pass. The menu
    is the bulleted block before the "Specify subagent_type" line.
    """
    menu = description.split("Specify subagent_type")[0]
    return {
        line.removeprefix("- ").split(":", 1)[0]
        for line in menu.splitlines()
        if line.startswith("- ")
    }


@pytest.fixture(scope="module")
def compiled() -> Any:
    from repo_cartographer.agent import agent

    return agent


@needs_model
def test_compiled_orchestrator_sees_only_its_four_tools(compiled: Any) -> None:
    """What the *model* is offered, which is not what the graph contains.

    `RestrictToolsMiddleware` filters per model request rather than unbinding, so
    the tool node still holds `write_file` and friends — the model is simply never
    told about them. Asserting on the node alone would therefore pass while the
    filtering was broken, so the filter is applied here the way the graph applies
    it.
    """
    from repo_cartographer.middleware import RestrictToolsMiddleware

    node = compiled.nodes["tools"]
    bound = list(getattr(node, "bound", node).tools_by_name.values())

    class _Request:
        def __init__(self, tools: list[Any]) -> None:
            self.tools = tools

        def override(self, tools: list[Any]) -> Any:
            return _Request(tools)

    visible = RestrictToolsMiddleware()._filter(_Request(bound)).tools
    assert {t.name for t in visible} == EXPECTED["orchestrator"]


@needs_model
def test_compiled_orchestrator_can_see_shape_but_not_content(compiled: Any) -> None:
    """The 4b boundary, checked at the graph rather than the call site.

    The orchestrator needs to know a repository has a `src/` in order to fan out
    across it, and must not be able to read what is inside — leave a content tool
    here and it stops delegating, because delegating costs it a turn and a model
    takes the shorter path. The trace then comes out identical to Phase 3 and the
    phase measures nothing.
    """
    held = _tool_node_names(compiled)
    assert "get_repo_scopes" in held
    assert not held & CONTENT_TOOLS


@needs_model
def test_task_menu_lists_only_our_two_subagents(compiled: Any) -> None:
    """The general-purpose sub-agent is off, and its absence needs asserting.

    `create_deep_agent` adds one unless a harness profile disables it, and a
    profile key that fails to match does not raise — it silently leaves the default
    in place. So the only way to know the registration in `agent.py` worked is to
    read the menu the model is shown.
    """
    assert _menu_names(_task_tool(compiled).description) == {
        "explorer",
        "doc-writer",
        "link-checker",
    }


@needs_model
def test_compiled_subagents_got_the_tools_their_specs_asked_for(compiled: Any) -> None:
    """The claim the whole design rests on, checked where it is finally true.

    Everything above this asserts our intent. This asserts deepagents acted on it:
    that `"tools": []` really did leave the doc-writer without GitHub access, and
    that the per-spec `FilesystemMiddleware` really did keep `execute` out of the
    explorer. Reaching the compiled sub-agents means walking the `task` tool's
    closure, which is private structure — see this module's docstring on why that
    is worth doing once.
    """
    graphs: dict[str, Any] = {}
    for cell in (c.cell_contents for c in (_task_tool(compiled).func.__closure__ or ())):
        if isinstance(cell, dict) and all(hasattr(sub, "nodes") for sub in cell.values()):
            graphs = dict(cell)

    assert set(graphs) == {
        "explorer",
        "doc-writer",
        "link-checker",
    }, "could not reach the sub-agents"

    for name in MODEL_SUBAGENTS:
        assert _tool_node_names(graphs[name]) == EXPECTED[name]

    # The link-checker has no tool node at all, because it has no model to offer
    # tools to. Asserted rather than skipped: filtering it out and checking the
    # other two would pass just as happily if it had quietly become an ordinary
    # agent, which is the one change that would turn its verdicts into guesses.
    assert "tools" not in graphs["link-checker"].nodes
