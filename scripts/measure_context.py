"""Phase 3's definition of done: the same task, with and without offloading.

    uv run scripts/measure_context.py
    uv run scripts/measure_context.py --repeats 3
    uv run scripts/measure_context.py "Explore pallets/flask and explain routing."

Two arms, one variable. Both use the same prompt, the same tools and the same
workspace; they differ only in `tool_result_token_limit`. With it set, a tool
result over the threshold is written to the workspace and replaced in the thread
by a head-and-tail preview plus the path to read the rest from. With it `None`,
the whole result stays inline and is re-sent on every subsequent turn.

That resending is the cost being measured. A message thread is not paid for once
— it is the input to every turn, so a file that lands in it early is billed again
at each of the turns that follow. Hence the headline number is the *sum* of input
tokens across turns rather than the size of the final thread: it is what the run
actually cost, and it is what offloading is supposed to reduce.

## Reading the output honestly

Two things to keep in mind before quoting the number anywhere:

- **The arms are not deterministic.** The model chooses which files to open, and
  two runs of the same arm will not open the same ones. A single run per arm is
  an anecdote. `--repeats` runs each arm N times and reports the spread; if the
  arms' ranges overlap, the honest conclusion is that this task is too small to
  show the effect, not that offloading does not work.
- **Offloading is not free.** Evicting a result costs a `write_file` and buys the
  agent a `read_file` it may choose to spend. On a task whose files all fit
  comfortably under the threshold, the "on" arm can legitimately cost *more*.
  That is a real result about task size, and it is worth reporting as one.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

# The script lives in scripts/, so the package root is one level up and is not on
# sys.path when the file is run directly. pyproject's `pythonpath = ["."]` covers
# pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_cartographer.agent import (
    EXAMPLE_QUESTION,
    TOOL_RESULT_TOKEN_LIMIT,
    build_agent,
    map_repo,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import BaseMessage

# The marker `FilesystemMiddleware` writes into a tool result it has evicted. Used
# only to count evictions for the report — matching on the message text is fragile
# if the library rewords it, so a miscount here is cosmetic and never affects the
# token figures, which come from provider usage metadata.
_EVICTION_MARKER = "was saved in the filesystem at this path:"


class Measurement(NamedTuple):
    """What one run of one arm cost."""

    cumulative_input_tokens: int
    """Input tokens summed over every model turn — the real bill for the thread."""

    final_input_tokens: int
    """Input tokens on the last turn — how large the thread had grown by the end."""

    output_tokens: int
    turns: int
    tool_calls: int
    evictions: int
    answer: str

    @property
    def usable(self) -> bool:
        """False when the provider returned no usage metadata, making tokens unknown."""
        return self.turns > 0


def measure(messages: Sequence[BaseMessage]) -> Measurement:
    """Extract per-run costs from a finished message list.

    Token counts come from each `AIMessage`'s `usage_metadata`, which both
    providers populate, rather than from a local tokeniser or a `countTokens`
    call. A local estimate would be wrong in a different way for each provider,
    and Gemini's `countTokens` is a live request against the same free-tier daily
    budget this project is trying to conserve.
    """
    inputs: list[int] = []
    outputs = tool_calls = evictions = 0

    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if usage:
            inputs.append(usage["input_tokens"])
            outputs += usage["output_tokens"]
        tool_calls += len(getattr(message, "tool_calls", None) or [])
        if message.type == "tool" and _EVICTION_MARKER in str(message.content):
            evictions += 1

    answer = messages[-1].text if messages else ""
    return Measurement(
        cumulative_input_tokens=sum(inputs),
        final_input_tokens=inputs[-1] if inputs else 0,
        output_tokens=outputs,
        turns=len(inputs),
        tool_calls=tool_calls,
        evictions=evictions,
        answer=answer,
    )


def run_arm(question: str, *, limit: int | None) -> Measurement:
    """Run the question once through an agent built with the given eviction limit."""
    state = map_repo(question, build_agent(tool_result_token_limit=limit))
    return measure(state["messages"])


def _fmt(values: list[int]) -> str:
    """One column of the report: the median, and the range when there is one."""
    if not values:
        return "n/a"
    median = int(statistics.median(values))
    if len(values) == 1:
        return f"{median:,}"
    return f"{median:,} ({min(values):,}–{max(values):,})"


def report(off: list[Measurement], on: list[Measurement]) -> None:
    """Print the comparison. Medians, with ranges when repeats > 1."""
    rows = (
        ("cumulative input tokens", lambda m: m.cumulative_input_tokens),
        ("final-turn input tokens", lambda m: m.final_input_tokens),
        ("output tokens", lambda m: m.output_tokens),
        ("model turns", lambda m: m.turns),
        ("tool calls", lambda m: m.tool_calls),
        ("results evicted", lambda m: m.evictions),
    )

    print(f"\n{'':<26}{'offload off':>22}{'offload on':>22}")
    print("-" * 70)
    for label, get in rows:
        print(f"{label:<26}{_fmt([get(m) for m in off]):>22}{_fmt([get(m) for m in on]):>22}")

    off_cost = [m.cumulative_input_tokens for m in off]
    on_cost = [m.cumulative_input_tokens for m in on]
    if not (off_cost and on_cost):
        return

    off_median, on_median = statistics.median(off_cost), statistics.median(on_cost)
    delta = off_median - on_median
    share = abs(delta) / off_median if off_median else 0
    print("-" * 70)
    if delta == 0:
        print("The arms cost the same.")
    else:
        direction = "cheaper" if delta > 0 else "more expensive"
        print(f"Offloading was {abs(int(delta)):,} input tokens {direction} ({share:.0%}).")

    # Overlapping ranges mean the difference is inside the noise of which files the
    # model happened to open. Saying so is the point of running repeats at all.
    if len(off_cost) > 1 and min(off_cost) <= max(on_cost) and min(on_cost) <= max(off_cost):
        print("Ranges overlap — this task is too small to separate the arms.")
    if not any(m.evictions for m in on):
        print(
            f"No result exceeded {TOOL_RESULT_TOKEN_LIMIT:,} tokens, so eviction never "
            "fired. Any difference above is run-to-run variance, not offloading."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="*", help="defaults to agent.EXAMPLE_QUESTION")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="runs per arm; >1 reports a range. Costs 2xN model runs against your quota.",
    )
    args = parser.parse_args()

    question = " ".join(args.question).strip() or EXAMPLE_QUESTION
    print(f"Question: {question}")
    print(f"Arms: eviction off vs. eviction at {TOOL_RESULT_TOKEN_LIMIT:,} tokens")
    print(f"Runs: {args.repeats} per arm ({args.repeats * 2} total)")

    off: list[Measurement] = []
    on: list[Measurement] = []
    for i in range(args.repeats):
        for label, limit, bucket in (
            ("off", None, off),
            ("on", TOOL_RESULT_TOKEN_LIMIT, on),
        ):
            print(f"  run {i + 1}, offload {label}...", flush=True)
            bucket.append(run_arm(question, limit=limit))

    report(off, on)


if __name__ == "__main__":
    main()
