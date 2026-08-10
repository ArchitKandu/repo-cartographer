"""Phase 4's definition of done: one thread became three, and here they are.

    uv run scripts/show_contexts.py
    uv run scripts/show_contexts.py "Explore pallets/flask and explain routing."

The guide asks you to open a LangSmith trace and see two or more sub-agent
invocations, each with its own smaller context window. This is the same
observation made locally, as numbers you can paste into a README — useful because
tracing needs an account and a key, and because a table survives in a commit
where a screenshot does not.

## Why `scripts/measure_context.py` cannot answer this

That script reads `state["messages"]` from a finished run, which is the
*orchestrator's* thread and nothing else. After Phase 4 the file reads happen
inside the explorer, in a thread that never appears there. Its cumulative-token
figure therefore fell sharply at Phase 4 for a reason that is not a saving: the
tokens moved somewhere it cannot see. Sub-agent usage is only reachable while the
run is happening, through `.stream(..., subgraphs=True)`, which is what this file
does.

That distinction is the whole point of the script. A multi-agent system looks
cheaper the moment your instrument stops seeing most of the work, and a number
that fell because you stopped measuring is worse than no number — you will quote
it.

## Reading the output honestly

- **Cumulative input tokens are the bill.** A message thread is the input to
  every turn, so a file that lands in it early is paid for again at each turn
  that follows. The total across all three agents is what the run cost.
- **Peak input tokens are the crowding.** This is the number quarantine is
  supposed to move: the largest single context any one agent had to hold. Phase 3
  reduced it by evicting large results after paying for them once; Phase 4
  reduces it by never putting them in the parent thread at all.
- **The total will not go down.** Delegation adds turns — a brief to write, a
  report to relay — so three agents cost *more* in total than one did. What you
  are buying is the peak, and the isolation that comes with it. Reporting the
  total honestly is the difference between measuring and marketing.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

# The script lives in scripts/, so the package root is one level up and is not on
# sys.path when the file is run directly. pyproject's `pythonpath = ["."]` covers
# pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_cartographer.agent import EXAMPLE_QUESTION, agent, run_config

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.messages import BaseMessage


class Thread(NamedTuple):
    """What one agent's message thread cost over a run."""

    label: str
    cumulative_input_tokens: int
    """Input tokens summed over this thread's turns — its share of the bill."""

    peak_input_tokens: int
    """The largest single context this agent had to hold. The number Phase 4 moves."""

    output_tokens: int
    turns: int
    tool_calls: int


def _label_for(namespace: tuple[str, ...], messages: Iterable[BaseMessage]) -> str:
    """Name the agent a stream namespace belongs to.

    The root namespace is empty and is always the orchestrator. Sub-agent
    namespaces are LangGraph-generated strings containing the node and a run id,
    which identify the *invocation* but not which sub-agent was invoked — so the
    name is recovered from the tools that appear in the thread, which is the one
    signal the split guarantees is distinct. Falls back to the raw namespace, since
    an unlabelled row is still a row and guessing wrong would be worse.
    """
    if not namespace:
        return "orchestrator"

    messages = list(messages)
    called = {
        call.get("name")
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    }
    if called & {"get_repo_tree", "get_file_contents", "search_code"}:
        return "explorer"
    if called <= {"ls", "read_file", "write_file"} and called:
        return "doc-writer"
    # The link-checker calls no tools at all — it *is* the tool — so the signal
    # every other branch here relies on is absent by construction. It is named
    # from the one thing it does emit. Worth the special case: without it Phase
    # 6's delegate shows up as an anonymous namespace, and a report that cannot
    # name an agent cannot be used to check the agent ran.
    if any(message.text.startswith("CITATION CHECK") for message in messages):
        return "link-checker"
    return f"sub-agent {namespace[-1].split(':')[0]}"


def _measure(label: str, messages: list[BaseMessage]) -> Thread:
    inputs: list[int] = []
    outputs = tool_calls = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if usage:
            inputs.append(usage["input_tokens"])
            outputs += usage["output_tokens"]
        tool_calls += len(getattr(message, "tool_calls", None) or [])
    return Thread(
        label=label,
        cumulative_input_tokens=sum(inputs),
        peak_input_tokens=max(inputs, default=0),
        output_tokens=outputs,
        turns=len(inputs),
        tool_calls=tool_calls,
    )


def run(question: str) -> tuple[list[Thread], str, list[int]]:
    """Stream one mapping, collecting messages per agent thread.

    `subgraphs=True` is the entire reason this works: without it the stream yields
    only the root graph's updates and the sub-agents' threads are invisible — which
    is exactly the blind spot `measure_context.py` has.
    """
    per_namespace: dict[tuple[str, ...], list[BaseMessage]] = defaultdict(list)
    seen: set[int] = set()

    for namespace, update in agent.stream(
        {"messages": [{"role": "user", "content": question}]},
        # Since Phase 8 the graph carries a checkpointer, so every invocation
        # needs a thread to write to. `run_config` is the one place that knows.
        config=run_config(),
        subgraphs=True,
        stream_mode="updates",
    ):
        # `stream(subgraphs=True)` is typed as yielding `Any`, so both the
        # namespace and the update are narrowed before use rather than trusted.
        if not isinstance(update, dict):
            continue
        key: tuple[str, ...] = tuple(namespace)
        for node_update in update.values():
            if not isinstance(node_update, dict):
                continue
            for message in node_update.get("messages", []) or []:
                # A message can be emitted by more than one node update; count each
                # one once or every token figure here is inflated.
                if id(message) not in seen:
                    seen.add(id(message))
                    per_namespace[key].append(message)

    # Since 4b there can be several explorers in one run, and three rows all
    # labelled "explorer" would be unreadable. Number repeats in the order the
    # threads first appeared, which is dispatch order.
    labels = [_label_for(ns, messages) for ns, messages in per_namespace.items()]
    seen_labels: dict[str, int] = {}
    numbered: list[str] = []
    for label in labels:
        total = labels.count(label)
        seen_labels[label] = seen_labels.get(label, 0) + 1
        numbered.append(f"{label} {seen_labels[label]}" if total > 1 else label)

    threads = [
        _measure(label, messages)
        for label, messages in zip(numbered, per_namespace.values(), strict=True)
    ]
    # Orchestrator first, then sub-agents in the order they were invoked.
    threads.sort(key=lambda t: (t.label != "orchestrator",))

    root = per_namespace.get((), [])
    answer = root[-1].text if root else ""
    return threads, answer, _dispatch_shape(root)


def _dispatch_shape(root: list[BaseMessage]) -> list[int]:
    """How many `task` calls the orchestrator issued per message.

    4b's claim is that explorers run *concurrently*, and concurrency here is not
    something the library arranges — it is the model emitting several `task` calls
    in one assistant message. A run that dispatches them one per message does the
    same work in the same isolated contexts and takes as many round trips as it has
    explorers, and nothing else in this report would tell them apart. So the shape
    of the dispatch is measured rather than assumed: `[2]` is one message asking for
    two explorers, `[1, 1]` is two messages asking for one each.
    """
    return [
        count
        for message in root
        if (
            count := sum(
                1
                for call in (getattr(message, "tool_calls", None) or [])
                if call.get("name") == "task"
            )
        )
    ]


def report(threads: list[Thread], dispatch: list[int]) -> None:
    header = f"{'agent':<16}{'turns':>7}{'tool calls':>12}{'cumulative in':>16}{'peak in':>10}"
    print(f"\n{header}")
    print("-" * len(header))
    for t in threads:
        print(
            f"{t.label:<16}{t.turns:>7}{t.tool_calls:>12}"
            f"{t.cumulative_input_tokens:>16,}{t.peak_input_tokens:>10,}"
        )
    print("-" * len(header))

    if not threads:
        print("No usage metadata was returned — the provider reported no token counts.")
        return

    total_cumulative = sum(t.cumulative_input_tokens for t in threads)
    print(
        f"{'whole run':<16}{sum(t.turns for t in threads):>7}"
        f"{sum(t.tool_calls for t in threads):>12}{total_cumulative:>16,}"
        f"{max(t.peak_input_tokens for t in threads):>10,}"
    )

    subagents = [t for t in threads if not t.label.startswith("orchestrator")]
    if not subagents:
        print(
            "\nNo sub-agent threads appeared. The orchestrator answered alone, which "
            "means delegation did not happen — Phase 4's definition of done is not met."
        )
        return

    widest = max(threads, key=lambda t: t.peak_input_tokens)
    print(
        f"\n{len(subagents)} sub-agent invocation(s), each in its own thread. The "
        f"largest single context was {widest.peak_input_tokens:,} tokens, in "
        f"{widest.label}."
    )
    quarantined = sum(t.cumulative_input_tokens for t in subagents)
    print(
        f"{quarantined:,} of the run's {total_cumulative:,} input tokens "
        f"({quarantined / total_cumulative:.0%}) were carried in sub-agent threads and "
        "never entered the orchestrator's. That is the quarantine, in tokens."
    )

    # Phase 6, visible in the same table as everything else: one of the rows is a
    # delegate that spent nothing, because it has no model to spend it with. A
    # zero here is the claim "a sub-agent is a unit of delegated work, not a
    # smaller model call" reported as a measurement rather than asserted in a
    # doc — and its *absence* is the more useful signal, since it means the
    # orchestrator skipped a step its prompt says is not optional.
    checker = next((t for t in threads if t.label.startswith("link-checker")), None)
    if checker is None:
        print(
            "\nNo link-checker thread appeared. The orchestrator handed over a guide "
            "whose citations were never verified, which its prompt forbids — Phase 6's "
            "delegate is wired in but was not used on this run."
        )
    else:
        print(
            f"\nThe link-checker ran and cost {checker.cumulative_input_tokens} input "
            f"tokens over {checker.turns} model turn(s). It holds no model: the citation "
            "check is arithmetic over the repository's file tree, delegated through the "
            "same `task` tool as the agents that think."
        )

    if not dispatch:
        return
    concurrent = max(dispatch)
    print(
        f"\nDispatch shape: {dispatch} — {len(dispatch)} message(s) issuing "
        f"{sum(dispatch)} task call(s), at most {concurrent} at once."
    )
    if concurrent > 1:
        print(
            f"{concurrent} sub-agents were launched in a single message, so they ran "
            "concurrently. Note what that does and does not buy: separate contexts, "
            "yes — but with a per-minute request budget the run is no faster, because "
            "every agent draws from the same bucket (see models.py)."
        )
    else:
        print(
            "Every task call went out in its own message, so the sub-agents ran one "
            "after another. The contexts are still isolated and the token figures "
            "above still hold — but this run did not exercise 4b's fan-out. On a "
            "weaker model that is a prompt-adherence result worth recording, not a "
            "bug to hide."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="*", help="defaults to agent.EXAMPLE_QUESTION")
    parser.add_argument(
        "--answer",
        action="store_true",
        help="also print the guide the run produced",
    )
    args = parser.parse_args()

    question = " ".join(args.question).strip() or EXAMPLE_QUESTION
    print(f"Question: {question}")

    threads, answer, dispatch = run(question)
    report(threads, dispatch)

    if args.answer:
        print(f"\n{'=' * 70}\n{answer}")


if __name__ == "__main__":
    main()
