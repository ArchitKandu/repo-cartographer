"""Which agent runs on which model, on whose budget, and what that buys.

    uv run scripts/show_models.py

No request is made and no repository is touched. Everything printed is a decision
`models.py` reaches from `.env` before anything is constructed, which is exactly
why it is worth printing: the routing is invisible in a transcript. A run whose
doc-writer never left the primary model looks identical to one where it did —
same guide, same tool calls, same everything — and the only difference is which
per-minute budget got spent, which you would notice as a 429 rather than as a
wrong answer.

So this is the counterpart to `show_contexts.py`. That one reports what a run
cost after the fact; this one reports what the run is *allowed* to cost, before
it starts. Both exist because on a free tier the binding constraint is requests
per minute, and a request is spent per model turn.

The per-run figures are estimates and say so. They come from the Phase 4 trace in
ARCHITECTURE.md — a two-explorer run on `psf/requests`, counted in model turns —
and they are here to show the *shape* of the split rather than to be quoted. The
real numbers come from `show_contexts.py` on a real run.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_cartographer.models import (
    _MAX_RETRIES,
    ASSIST,
    ASSIST_ROLES,
    PRIMARY,
    ROLES,
    ModelChoice,
    _rate_limiter,
    choice_for,
)

# Model turns per role, read off the recorded two-explorer run in ARCHITECTURE.md
# and rounded. Deliberately not computed from anything: there is no way to know
# what a run costs without making one, and a figure derived from a formula would
# read as more authoritative than the trace it came from.
_TYPICAL_TURNS = {"orchestrator": 9, "explorer": 15, "doc-writer": 5}


def main() -> None:
    print("\nRouting — which agent reasons with what")
    header = f"{'role':<16}{'provider':<12}{'model':<42}{'turns/run':>10}"
    print(header)
    print("-" * len(header))

    per_model: dict[ModelChoice, int] = {}
    for role in ROLES:
        choice = choice_for(role)
        turns = _TYPICAL_TURNS[role]
        per_model[choice] = per_model.get(choice, 0) + turns
        print(f"{role:<16}{choice.provider:<12}{choice.name:<42}{turns:>10}")

    print("-" * len(header))
    print(f"{'link-checker':<16}{'—':<12}{'no model at all (Phase 6)':<42}{0:>10}")

    print("\nBudgets — one bucket per model, because the limits are published per model")
    header = f"{'model':<42}{'req/min':>9}{'turns/run':>11}{'runs/min':>10}"
    print(f"\n{header}")
    print("-" * len(header))
    for choice, turns in per_model.items():
        per_minute = _rate_limiter(choice).requests_per_second * 60
        print(f"{choice.name:<42}{per_minute:>9.1f}{turns:>11}{per_minute / turns:>10.2f}")
    print("-" * len(header))

    if ASSIST is None:
        print(
            "\nOne model, one budget. Every agent shares it, which is what Phase 4b's\n"
            "fan-out ran into: three explorers in their own tool loops reach a\n"
            "per-minute ceiling in seconds. Set a key for the second provider and the\n"
            "doc-writer moves off this bucket on its own — see `.env.example`."
        )
    else:
        moved = ", ".join(sorted(ASSIST_ROLES)) or "nothing"
        print(
            f"\nTwo models, two budgets. On the assist model: {moved}.\n"
            "That is capacity rather than a saving — no request was removed, one was\n"
            "moved somewhere it is not competing with the fan-out."
        )
        if ASSIST_ROLES & {"orchestrator", "explorer"}:
            print(
                "\n  Note: an agent whose regressions nothing here would catch is on the\n"
                "  assist model. The orchestrator's parallel dispatch, its brief format\n"
                "  and its verbatim relay all fail silently; the explorer's notes are the\n"
                "  only source the guide has. Check it with `run_evals.py` rather than by\n"
                "  reading one answer — see `models.py` for the argument."
            )

    print(
        f"\nRetries: at most {_MAX_RETRIES} per request. Every retry is a real request the\n"
        "provider counts, against the per-minute limit that caused the rejection and\n"
        "the per-day limit that did not. The client's own default is 6.\n"
    )
    print(
        "The turns/run column is an estimate from the recorded Phase 4 trace, not a\n"
        "measurement of your run. For that: uv run scripts/show_contexts.py\n"
    )
    print(f"Primary provider: {PRIMARY.provider}. Change it with LLM_PROVIDER in .env.\n")


if __name__ == "__main__":
    main()
