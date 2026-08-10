"""Phase 8's definition of done: trigger the gate and watch execution stop.

    uv run scripts/prove_approval_gate.py

The implementation guide is specific about what counts here: *confirm execution
actually pauses for approval — not just that the parameter is set.* Those are
different claims, and only one of them is worth anything. `interrupt_on` is a
dictionary key; whether the graph honours it, whether a rejection really prevents
the call, and whether an approval really reaches the tool body are facts about a
running system.

So this drives one real run three times over the same paused state:

1. **Run until the gate.** Ask for a pull request. The graph stops *before*
   `open_pull_request` executes and hands back the pending call.
2. **Reject it.** Resume with a rejection and confirm the tool never ran — the
   model gets a message saying the human declined, and nothing left this machine.
3. **Approve it.** Resume the *same* paused state again, this time approving, and
   confirm the call reaches the tool body.

Step 3 is the one that needs care, because "prove the safety feature works" must
not mean "open a real pull request on a repository you do not own to see if it
would". It does not, here: `open_pull_request` refuses unless
`ALLOW_PULL_REQUESTS=true`, and this script never sets it. So the approve path is
exercised all the way into the function, and the function's first act is to
decline and say why. What that demonstrates is precisely what step 3 is for —
that approval releases the call — while the second guard keeps GitHub out of it.

## Reading the result

The outcome to look for is that steps 2 and 3 *differ*. A gate that pauses and
then behaves identically whatever you answer is theatre. The rejection has to
stop the tool and the approval has to reach it, and the script prints both tool
messages so the difference is visible rather than asserted.

## What it does not prove

That a pull request would be well-formed. `open_pull_request` is real code
against the real GitHub API, and no run in this repository has ever executed its
network half. That is a deliberate gap, and the honest way to close it is a
throwaway repository you own, not a test suite that writes to other people's.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.types import Command

from repo_cartographer.agent import WORKSPACE, agent, pending_approval, run_config
from repo_cartographer.pull_requests import ALLOW_ENV, PULL_REQUEST_TOOL

# A repository small enough to map quickly. It is never written to — see the
# module docstring — but it is named honestly rather than faked, because the
# whole question is what the system does when pointed at something real.
TARGET = "chalk/chalk"

QUESTION = (
    f"Map the public GitHub repository {TARGET} and explain its architecture "
    "briefly. Then open a draft pull request proposing the guide to that "
    f"repository, titled 'docs: add an onboarding guide'."
)


def clear_workspace() -> None:
    """Same seatbelt as every other script here: only ever the workspace."""
    if WORKSPACE.name != "workspace":
        raise SystemExit(f"refusing to clear {WORKSPACE}, which is not the workspace")
    WORKSPACE.mkdir(exist_ok=True)
    for child in WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def tool_messages(state: dict[str, Any]) -> list[Any]:
    """Every `open_pull_request` result in the thread so far."""
    return [
        message
        for message in state.get("messages", [])
        if getattr(message, "name", None) == PULL_REQUEST_TOOL
    ]


# Strings only `open_pull_request` itself produces. Whether the *tool body ran* is
# the question this script is actually asking, and the tool's own words are the
# only honest signature for it — the middleware's rejection message is whatever
# the human typed, so looking for a word like "rejected" in it tests the script's
# own literal rather than the system.
TOOL_RAN = ("Refused:", "Opened a draft pull request", "already open on")


def tool_body_ran(message: Any | None) -> bool:
    if message is None:
        return False
    return any(marker in str(message.content) for marker in TOOL_RAN)


def quote(label: str, text: str) -> None:
    print(f"   {label}")
    for line in text.splitlines():
        print(f"   │ {line}")


def show(label: str, message: Any | None) -> None:
    """Print one tool message, with its status.

    The status is half the evidence: a rejection reaches the model as a
    synthetic `ToolMessage` with `status="error"` that the tool never produced,
    while a real result comes back as `status="success"`.
    """
    if message is None:
        quote(label, "(none — the tool was never invoked)")
        return
    quote(label, f"[status={message.status}]\n{message.content}")


def main() -> int:
    if os.environ.get(ALLOW_ENV, "").strip().lower() == "true":
        raise SystemExit(
            f"{ALLOW_ENV} is set, so an approval in step 3 would open a real pull "
            f"request on {TARGET}. Unset it and run again — this script is a "
            "demonstration of the gate, not a way to use the feature."
        )

    clear_workspace()
    thread = str(uuid4())
    config = run_config(thread)

    from repo_cartographer.models import MODEL_PROFILE_KEY

    print(f"Asking for something irreversible against {TARGET}.")
    print(f"{ALLOW_ENV} is not set, so nothing can reach GitHub even if approved.")
    # Named because whether the model *asks* for a pull request at all is a model
    # decision, and a reader comparing this run to another needs to know which one
    # made it.
    print(f"Model: {MODEL_PROFILE_KEY}\n")

    print("1. running until the gate …")
    state = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]}, config=config)

    interrupt = pending_approval(state)
    if interrupt is None:
        print("\nNO PAUSE. The run finished without ever asking.")
        called = tool_messages(state)
        print(
            f"   {PULL_REQUEST_TOOL} was called {len(called)} time(s).\n"
            "   If it was called at all, the gate is broken and this is the failure "
            "the phase exists to prevent.\n"
            "   If it was not, the model simply never asked for a pull request — "
            "the gate was never tested, so re-run it."
        )
        return 1 if called else 2

    print("   PAUSED. The graph stopped before the tool ran.\n")
    quote("what it is asking permission for:", str(interrupt.value))
    print()
    assert not tool_messages(state), "the tool ran before approval — the gate did nothing"
    print(f"   {PULL_REQUEST_TOOL} results so far: 0 — nothing has executed.\n")

    print("2. resuming with a REJECTION …")
    rejected = agent.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "Not this time."}]}),
        config=config,
    )
    after_reject = tool_messages(rejected)
    show("tool message the model received:", after_reject[-1] if after_reject else None)
    print()

    # The same paused state, answered the other way. A fresh thread replays the
    # run from the gate, which is what makes the two branches comparable: same
    # question, same pending call, different decision.
    print("3. replaying the same run and APPROVING …")
    replay = run_config(str(uuid4()))
    state2 = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]}, config=replay)
    if pending_approval(state2) is None:
        print("   the replay did not reach the gate; step 3 is inconclusive.")
        return 2
    approved = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=replay)
    after_approve = tool_messages(approved)
    show("tool message the model received:", after_approve[-1] if after_approve else None)

    print()
    reject_blocked = not tool_body_ran(after_reject[-1] if after_reject else None)
    approve_reached = tool_body_ran(after_approve[-1] if after_approve else None)
    print(
        f"   tool body ran on rejection: {not reject_blocked}   "
        f"on approval: {approve_reached}"
    )
    print()

    if reject_blocked and approve_reached:
        print(
            "PASS — execution really pauses, and the two answers really differ.\n"
            "Rejecting stopped the call; approving released it into the tool, whose\n"
            f"own first act was to refuse because {ALLOW_ENV} is not set. Nothing\n"
            f"was sent to {TARGET} on either branch."
        )
        return 0

    print(
        "FAIL — the gate paused, but the decision did not change what happened.\n"
        "A gate that behaves the same whatever you answer is theatre."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
