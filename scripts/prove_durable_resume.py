"""Proof that a paused run survives the death of the process that paused it.

    uv run scripts/prove_durable_resume.py pause
    uv run scripts/prove_durable_resume.py resume <thread-id>

`scripts/prove_approval_gate.py` already shows that the gate stops execution and
that the two answers differ. It proves that inside one process, against an
`InMemorySaver`, which is the honest scope of Phase 8 — and it is exactly the
assumption a web deployment removes. There, the pause and the answer are two HTTP
requests with a person in between, and nothing guarantees the same process, or
the same container, is there to receive the second one.

So this script asks the narrower question that the Postgres checkpointer exists
to answer: *if the process that paused the run is gone, can the run still be
answered?* It cannot be shown in one program, because a single program proves
nothing about what outlives it. Hence two commands. `pause` runs until the gate
and exits, taking its interpreter, its connection pool and its entire heap with
it. `resume` starts a new interpreter that has never seen the run, is told only a
thread id, and answers the interrupt.

If the state is really in Postgres, the second process picks up a conversation it
did not have. If it is not, LangGraph has no checkpoint for that thread and the
resume starts a new run instead — which is the failure this is looking for, and
why `resume` checks the recovered history rather than just checking for a crash.

Nothing reaches GitHub on either half: `open_pull_request` refuses unless
`ALLOW_PULL_REQUESTS=true`, and this script refuses to run if it is set.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles hand Python a cp1252 stdout, which cannot encode the box
# drawing characters used to quote tool output below. Without this the script
# does its whole job and then dies in the last five lines with a
# UnicodeEncodeError, reporting a failure that did not happen.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

from langgraph.types import Command

from repo_cartographer.agent import (
    WORKSPACE,
    build_agent,
    pending_approval,
    run_config,
)
from repo_cartographer.link_checker import DEFAULT_GUIDE_PATH
from repo_cartographer.persistence import SCHEMA, checkpointer
from repo_cartographer.pull_requests import ALLOW_ENV, PULL_REQUEST_TOOL

TARGET = "chalk/chalk"

# Deliberately a much smaller errand than the one in `prove_approval_gate.py`.
# That script is about the gate, so it earns the full mapping run that leads to
# one. This script is about what happens to a run *after* it pauses, and every
# exploration turn before the pause is pure cost: more tokens, more minutes, and
# more chances for the orchestrator to wander into the recursion limit and never
# reach the gate at all — which is a way for this script to fail while saying
# nothing whatsoever about persistence. The shortest honest route to a real
# pending `open_pull_request` is the right one here.
QUESTION = (
    f"Read only the README of the public GitHub repository {TARGET}. Do not "
    "explore any other file and do not delegate. Write a two-sentence onboarding "
    f"note from it, save that to {DEFAULT_GUIDE_PATH}, and then open a draft pull "
    "request proposing it to that repository, titled 'docs: add an onboarding "
    "guide'."
)


def refuse_if_live() -> None:
    if os.environ.get(ALLOW_ENV, "").strip().lower() == "true":
        raise SystemExit(
            f"{ALLOW_ENV} is set, so resuming with an approval would open a real "
            f"pull request on {TARGET}. Unset it and run again."
        )


def clear_workspace() -> None:
    """Same seatbelt as every other script here: only ever the workspace."""
    if WORKSPACE.name != "workspace":
        raise SystemExit(f"refusing to clear {WORKSPACE}, which is not the workspace")
    WORKSPACE.mkdir(exist_ok=True)
    for child in WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def tool_messages(state: dict[str, Any]) -> list[Any]:
    return [
        message
        for message in state.get("messages", [])
        if getattr(message, "name", None) == PULL_REQUEST_TOOL
    ]


async def pause() -> int:
    """Run until the approval gate, then exit and take the process with us."""
    clear_workspace()
    thread = str(uuid4())

    print(f"Target: {TARGET}   thread: {thread}")
    print(f"Checkpointing to Postgres, schema `{SCHEMA}`.\n")
    print("running until the gate …")

    async with checkpointer() as saver:
        cartographer = build_agent(checkpointer=saver)
        state = await cartographer.ainvoke(
            {"messages": [{"role": "user", "content": QUESTION}]},
            config=run_config(thread),
        )

    interrupt = pending_approval(state)
    if interrupt is None:
        print("\nNO PAUSE — the model never asked for a pull request, so there is")
        print("nothing paused to resume. Re-run; this is a model decision.")
        return 2

    if tool_messages(state):
        raise SystemExit("FAIL — the tool produced a result before anyone approved it.")

    print(f"\nPAUSED with {len(state.get('messages', []))} messages in the thread.")
    print("This process is about to exit. Its heap, its pool and its copy of the")
    print("run go with it; the only thing left anywhere is the Postgres checkpoint.\n")
    print(f"   uv run scripts/prove_durable_resume.py resume {thread}")
    return 0


async def resume(thread: str) -> int:
    """Answer a run this process has never seen."""
    print(f"Fresh process. Nothing in memory about thread {thread}.")
    print("Loading it from Postgres …\n")

    async with checkpointer() as saver:
        cartographer = build_agent(checkpointer=saver)
        config = run_config(thread)

        # The first question is not whether the resume crashes — it is whether
        # there is anything there at all. A thread id with no checkpoint behind
        # it does not raise: LangGraph treats it as a new conversation, and a
        # `Command(resume=...)` against nothing would quietly start over. Reading
        # the state back *before* answering is what separates "recovered the run"
        # from "began a second one that looks similar".
        snapshot = await cartographer.aget_state(config)
        recovered = len(snapshot.values.get("messages", []))
        print(f"recovered {recovered} messages from the checkpoint")

        if recovered == 0:
            print("\nFAIL — no checkpoint for that thread. Nothing was persisted,")
            print("or the resume is looking in a different schema than the pause.")
            return 1

        if not snapshot.next:
            print("\nFAIL — the recovered run is not paused at anything, so there")
            print("is no interrupt to answer.")
            return 1

        print(f"the recovered run is waiting at: {snapshot.next}\n")
        print("answering it with a REJECTION …")
        state = await cartographer.ainvoke(
            Command(resume={"decisions": [{"type": "reject", "message": "Not this time."}]}),
            config=config,
        )

    after = tool_messages(state)
    if not after:
        print("\nFAIL — the resume produced no tool message, so the pending call")
        print("was never answered.")
        return 1

    print(f"\ntool message the model received  [status={after[-1].status}]:")
    for line in str(after[-1].content).splitlines():
        print(f"   │ {line}")

    print()
    print("PASS — a run paused by a process that no longer exists was recovered")
    print("from Postgres by one that never saw it, and answered. The approval gate")
    print("now survives a restart, which is what a deployment needs it to do.")
    return 0


def main() -> int:
    refuse_if_live()
    match sys.argv[1:]:
        case ["pause"]:
            return asyncio.run(pause())
        case ["resume", thread]:
            return asyncio.run(resume(thread))
        case _:
            print(__doc__)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
