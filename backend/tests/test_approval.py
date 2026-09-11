"""Phase 8's tests: is the irreversible action actually gated?

The definition of done for this phase is explicitly *not* "the parameter is set"
— it is that execution pauses. That needs a real run, so it lives in
`scripts/prove_approval_gate.py`. What is here is everything the pause depends
on, checked with no model and no network:

**The gate names the right tool.** `interrupt_on` is a dictionary keyed by tool
*name*. Rename the function and forget the key and the capability stays wired in,
completely ungated, with nothing anywhere raising — the run would simply open a
pull request and tell you it had. That is the worst failure available in this
phase and it is one string away, so the name is asserted from both ends.

**The gate is narrow.** A gate that interrupts on every tool trains whoever is
answering to approve without reading, and then the one call that mattered is
waved through with the rest. So the assertion is not "an approval exists" but
"exactly one tool has one".

**The checkpointer exists.** Without one `interrupt()` raises instead of pausing,
which fails in the loud direction — but only on the code path that was supposed
to be the safe one, and only when someone finally triggers it.

**The second guard holds on its own.** `ALLOW_PULL_REQUESTS` is what makes the
approve path exercisable at all, so it gets tested without any GitHub call: the
refusal has to come back *before* anything is sent.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from deepagents.backends import FilesystemBackend

from repo_cartographer.link_checker import DEFAULT_GUIDE_PATH
from repo_cartographer.prompts import ORCHESTRATOR_PROMPT
from repo_cartographer.pull_requests import (
    ALLOW_ENV,
    BRANCH,
    GUIDE_FILENAME,
    PULL_REQUEST_TOOL,
    build_pull_request_tool,
)


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """The real tool over a throwaway workspace, with the env guard left off."""
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    backend = FilesystemBackend(root_dir=tmp_path)
    backend.write(DEFAULT_GUIDE_PATH, "# Guide\n\nSee `src/x.py`.\n")
    return build_pull_request_tool(backend, DEFAULT_GUIDE_PATH)


# --------------------------------------------------------------------------- #
# Guard 1: the gate itself.
# --------------------------------------------------------------------------- #


def test_the_gate_names_the_tool_that_actually_exists(tool) -> None:
    """The one-string failure: `interrupt_on` keys on a name, not a reference.

    Renaming the function without updating the key leaves a working, ungated
    capability behind. Both ends are pinned to the same constant so the mistake
    cannot be made in only one of the two places.
    """
    assert tool.__name__ == PULL_REQUEST_TOOL


@pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")),
    reason="no provider key configured",
)
def test_the_compiled_graph_gates_exactly_one_tool() -> None:
    """Asserted on the compiled graph, because that is where it is finally true.

    Checking `agent.py`'s literal would only confirm our own intent. This reads
    the middleware the library actually installed — and checks the set is a set
    of one, because a gate on everything is a gate on nothing.
    """
    from repo_cartographer.agent import agent

    # The middleware compiles down to a graph node, and the node's callable is a
    # bound method of the middleware instance — so the instance, and the config it
    # resolved, is reachable through `__self__`. Private structure, and the same
    # trade `tests/test_wiring.py` documents for the `task` tool: if an upgrade
    # moves it this fails loudly, which is the outcome worth having for a claim
    # this load-bearing.
    node: Any = agent.nodes["HumanInTheLoopMiddleware.after_model"]
    middleware = getattr(node, "bound", node).func.__self__

    assert set(middleware.interrupt_on) == {PULL_REQUEST_TOOL}, (
        "exactly one tool should require approval — a gate on every tool trains "
        "whoever answers it to approve without reading, and then the one call "
        "that mattered gets waved through with the rest"
    )
    # `True` in the spec resolves to every decision being available. Reject is the
    # one that matters: an approval flow offering only "approve" is a dialog box.
    assert "reject" in middleware.interrupt_on[PULL_REQUEST_TOOL]["allowed_decisions"]


@pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")),
    reason="no provider key configured",
)
def test_the_graph_can_actually_pause() -> None:
    """A checkpointer is what makes `interrupt()` a pause instead of an exception.

    Without one the safety feature fails by crashing, on the exact path that was
    supposed to be the careful one, and only when someone finally triggers it.
    """
    from repo_cartographer.agent import agent

    assert agent.checkpointer is not None


def test_every_run_gets_its_own_thread() -> None:
    """A checkpointer means state persists, and persistence has a sharp edge.

    Reuse one thread id and the second question arrives as a follow-up to the
    first — the agent would answer about the previous repository and be right to.
    A fresh id per run is what keeps `ask()` stateless from the caller's side.
    """
    from repo_cartographer.agent import run_config

    first = run_config()["configurable"]["thread_id"]
    second = run_config()["configurable"]["thread_id"]
    assert first != second
    assert run_config("fixed")["configurable"]["thread_id"] == "fixed"


def test_a_paused_run_is_not_mistaken_for_an_answer() -> None:
    """The trap Phase 8 introduces, and the reason `pending_approval` exists.

    A run that stops at the gate returns *normally*, carrying `__interrupt__`.
    Every caller that reaches for `messages[-1]` then gets the assistant's tool
    call and reads it as prose. Nothing raises; a paused run just looks like a
    finished one that happened to be brief.
    """
    from repo_cartographer.agent import pending_approval

    assert pending_approval({"messages": []}) is None

    class _Interrupt:
        value = "approve this"

    assert pending_approval({"__interrupt__": [_Interrupt()]}) is not None


# --------------------------------------------------------------------------- #
# Guard 2: the environment switch, tested without touching GitHub.
# --------------------------------------------------------------------------- #


def test_the_tool_refuses_before_it_sends_anything(tool, monkeypatch) -> None:
    """The refusal has to come back without a request, not after a failed one.

    Any outbound call is patched to explode, so a tool that checked the guard
    second — or not at all — fails this test loudly rather than opening a real
    pull request during a test run.
    """
    import repo_cartographer.pull_requests as prs

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the tool reached GitHub despite the guard being off")

    monkeypatch.setattr(prs, "_send", explode)
    monkeypatch.setattr(prs, "_get", explode)

    result = tool("psf", "requests", "docs: add an onboarding guide")

    assert "Refused" in result
    assert ALLOW_ENV in result, "the refusal should say how to enable it deliberately"
    assert "cannot be undone" in result


def test_approval_does_not_switch_the_capability_on(tool) -> None:
    """The two guards are independent, and the refusal says so in words.

    Someone reading it mid-run needs to know that clicking approve again will not
    help — otherwise the obvious next move is to retry, which is exactly what
    ORCHESTRATOR_PROMPT forbids.
    """
    assert "does not switch the capability on" in tool("psf", "requests", "t")


def test_a_missing_guide_is_refused_rather_than_proposed(tmp_path, monkeypatch) -> None:
    """A pull request adding an empty file is worse than no pull request.

    The tool proposes whatever is at the guide path, so a run that never got that
    far has nothing to send — and "nothing to send" must not become "send
    nothing".
    """
    monkeypatch.setenv(ALLOW_ENV, "true")
    empty = build_pull_request_tool(FilesystemBackend(root_dir=tmp_path), DEFAULT_GUIDE_PATH)

    result = empty("psf", "requests", "docs: add an onboarding guide")

    assert "Refused" in result
    assert DEFAULT_GUIDE_PATH in result


@pytest.mark.parametrize("value", ["", "false", "0", "yes", "True ", "TRUE"])
def test_only_an_exact_opt_in_counts(tool, monkeypatch, value: str) -> None:
    """`"TRUE"` and `"True "` should pass; `"yes"` and `"0"` should not.

    Parametrised because the check is a string comparison, and a guard that
    accepts anything truthy would treat `ALLOW_PULL_REQUESTS=false` as consent —
    the single most embarrassing way for this to go wrong.
    """
    monkeypatch.setenv(ALLOW_ENV, value)
    refused = "Refused" in tool("psf", "requests", "t")
    assert refused is (value.strip().lower() != "true")


# --------------------------------------------------------------------------- #
# What the tool proposes, and what the prompt says about it.
# --------------------------------------------------------------------------- #


def test_it_proposes_a_new_file_on_its_own_branch() -> None:
    """Two choices that keep a machine's proposal from damaging anything.

    A new file, because this system has never read the target's own README and
    has no business rewriting it. A namespaced branch, because a maintainer
    scanning a branch list should be able to tell instantly where it came from.
    """
    assert GUIDE_FILENAME == "ONBOARDING.md"
    assert BRANCH.startswith("repo-cartographer/")


def test_the_prompt_makes_it_opt_in_and_forbids_retrying() -> None:
    """The behaviours the gate cannot enforce, so the prompt has to.

    `interrupt_on` stops a call; it cannot stop the model deciding to make one on
    every run, and it cannot stop it rephrasing a rejected call and trying again.
    Both would technically respect the gate and defeat the point of it.
    """
    assert "Only when the user asked for it" in ORCHESTRATOR_PROMPT
    assert "do not rephrase the call and try again" in ORCHESTRATOR_PROMPT.lower()
    assert "cannot be undone" in ORCHESTRATOR_PROMPT
