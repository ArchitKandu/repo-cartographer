"""Phase 5's definition of done: one command, one number.

    uv run scripts/run_evals.py                    # run every case, then score
    uv run scripts/run_evals.py --case flask       # one case
    uv run scripts/run_evals.py --score-only       # score what is recorded, free
    uv run scripts/run_evals.py --history          # every run ever recorded
    uv run scripts/run_evals.py --fail-under 70    # exit 1 below a threshold

It maps each repository in `tests/evals/known_repos.jsonl`, scores the guide
that comes back against the facts that case expects, and prints

    24 of 31 expected facts present (77%)

## What it costs

A full sweep is one complete mapping run per case — six repositories, each
several model requests deep, all of them drawing from the one shared bucket in
`models.py`. On Google's free tier that is roughly 150 requests of the daily 500
and the best part of fifteen minutes of wall clock, most of it spent waiting for
the rate limiter rather than for the model. `--case` exists because iterating on
a prompt does not need all six.

Every guide is written to `tests/evals/results/` the moment its case finishes,
as a growing log rather than a snapshot. Two consequences worth knowing:

- A sweep that dies at case four keeps the three it finished.
- `--score-only` re-scores those recorded guides against the *current* dataset,
  for free and in milliseconds. So editing a fact, adding a case, or fixing a
  matcher costs no model quota at all — only changing the agent does.

## Reading the number honestly

**It is one sample per case.** The agent chooses which files to open, and that
choice differs between runs of an unchanged system — `measure_context.py` has
carried the same warning since Phase 3, and `ARCHITECTURE.md` records two runs
of one architecture that differed by 80% on cost. Do not read a two-point move
as a regression. Run the sweep twice before believing a small difference, and
use `--history` to see what the noise floor actually looks like here.

**It measures recall of citations, not quality.** A guide naming every expected
file and explaining all of them wrongly scores 31 out of 31. See
`tests/evals/__init__.py` for why that trade was made deliberately rather than
reached for a model-graded alternative.

**A stale record is worse than no record**, because a number is quotable. Every
recorded run carries the model it used and a fingerprint of the three prompts,
and `--score-only` prints a warning for any record whose fingerprint no longer
matches the prompts on disk. That is the specific way this instrument could
mislead: rewrite `EXPLORER_PROMPT`, re-score without re-running, and read an
unchanged score as evidence the rewrite was neutral.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.evals.dataset import DATASET, Case, load_cases, total_facts
from tests.evals.scoring import CaseScore, score_case, tally

if TYPE_CHECKING:
    from collections.abc import Sequence

RESULTS = Path(__file__).resolve().parent.parent / "tests" / "evals" / "results"

# Enough history to see a noise floor, not enough to make the file unreadable.
# Older runs fall off the front; nothing here is worth keeping forever, since a
# guide recorded under prompts that no longer exist cannot be compared to one
# recorded under the current ones.
MAX_RECORDED_RUNS = 20


def prompt_fingerprint() -> str:
    """A short hash of every instruction the agents work from.

    The point is not integrity, it is staleness. `--score-only` is free and
    instant, which makes it exactly the thing you reach for after editing an
    instruction — and re-scoring a guide produced by the *previous* one would
    read as "the edit changed nothing."

    Which makes what is hashed a correctness question rather than a detail.
    Until Phase 7 the instructions were three strings in `prompts.py` and that
    was the whole set. They are now five kinds of thing: those prompts,
    `AGENTS.md`, each `SKILL.md`, and the sections `briefing.py` splices into the
    explorer's prompt before it starts. Hashing only the prompts would leave the
    rest changing behaviour without ever marking a record stale — and two of them
    are the likeliest to be edited of all, `AGENTS.md` and the skills because
    editing them needs no Python at all. That is the exact failure this function
    exists to prevent, so it covers all of them.

    Imported here rather than at module scope so `--history` still works with no
    provider key: `prompts.py` and `skills.py` pull in nothing heavy, but keeping
    every agent import inside a function makes that a property of the file rather
    than a fact to re-verify.
    """
    from repo_cartographer.briefing import briefing_sections
    from repo_cartographer.prompts import (
        DOC_WRITER_PROMPT,
        EXPLORER_PROMPT,
        ORCHESTRATOR_PROMPT,
    )
    from repo_cartographer.skills import SKILLS_DIR, available_skills, house_style

    parts = [ORCHESTRATOR_PROMPT, EXPLORER_PROMPT, DOC_WRITER_PROMPT, house_style()]
    # The fifth kind, and the least obvious: `briefing.py` splices two sections
    # into the explorer's prompt at run time — its file list, and the skill that
    # matched. Rewording them changes what the explorer was told just as surely
    # as editing `EXPLORER_PROMPT` does, and it is the kind of edit that looks
    # like a code change rather than an instruction change, so it would be the
    # easiest one to make while every recorded score kept reading as current.
    parts += briefing_sections()
    # Sorted, so the digest depends on the skills' content and not on the order
    # the filesystem happened to list them in.
    parts += [
        (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        for name in available_skills()
    ]

    # NUL-separated so that moving a sentence from one file to another changes the
    # digest — concatenating them plainly would not.
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# The record log
# --------------------------------------------------------------------------- #


def _record_path(case_id: str) -> Path:
    return RESULTS / f"{case_id}.json"


def read_runs(case_id: str) -> list[dict[str, Any]]:
    """Every run recorded for this case, oldest first. Missing file means none."""
    path = _record_path(case_id)
    if not path.exists():
        return []
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A half-written file is a recoverable annoyance, not a reason to lose a
        # whole sweep. Say so and treat the case as unrecorded.
        print(f"  ! {path.name} is not valid JSON — ignoring it", file=sys.stderr)
        return []
    runs = stored.get("runs", [])
    return runs if isinstance(runs, list) else []


def append_run(case: Case, run: dict[str, Any]) -> None:
    """Write one run to the log immediately, so a sweep that dies keeps its work."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    runs = [*read_runs(case.id), run][-MAX_RECORDED_RUNS:]
    _record_path(case.id).write_text(
        json.dumps({"case": case.id, "repo": case.slug, "runs": runs}, indent=2) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def clear_workspace() -> None:
    """Empty `./workspace` so a case cannot read the previous case's notes.

    Not housekeeping — without it the eval measures the wrong thing. The
    workspace is one directory shared by every run, and `DOC_WRITER_PROMPT`
    tells the doc-writer to `ls` it and read every notes file it finds rather
    than trusting the brief's list. That instruction is right, and it is exactly
    what makes a sweep contaminate itself: map `psf/requests`, then map
    `pallets/flask`, and flask's doc-writer opens `/notes/src.md` full of
    `requests` and writes a guide from both. Nothing errors, and the score moves
    for a reason that is not the agent.

    This is a real limitation of the system rather than a quirk of the eval — two
    consecutive `uv run main.py` invocations have the same problem, and a
    per-run workspace is the actual fix. Doing it here keeps Phase 5 to its one
    variable; see the known limitations in ARCHITECTURE.md.

    The name check is a deliberate seatbelt: this function deletes a directory
    tree, and it should be impossible for a future edit to point it somewhere
    that is not the agent's own scratch space.
    """
    from repo_cartographer.agent import WORKSPACE

    if WORKSPACE.name != "workspace":
        raise SystemExit(f"refusing to clear {WORKSPACE}, which is not the workspace")
    for child in WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def run_case(case: Case, fingerprint: str, model: str) -> dict[str, Any]:
    """Map one repository and return the run record. Never raises.

    A failed case is data — the eval's job is to report that a repository could
    not be mapped, not to abandon the other five. The exception text is recorded
    so a rate-limit death and a recursion-limit death stay distinguishable a week
    later.
    """
    from repo_cartographer.agent import ask

    started = time.monotonic()
    guide, error = "", None
    try:
        guide = ask(case.question)
    except Exception as exc:  # noqa: BLE001 — any failure is a scored outcome
        error = f"{type(exc).__name__}: {exc}"

    return {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "prompt_fingerprint": fingerprint,
        "seconds": round(time.monotonic() - started, 1),
        "guide": guide,
        "error": error,
    }


def sweep(cases: Sequence[Case], fingerprint: str) -> None:
    """Run every selected case, recording each as it finishes."""
    from repo_cartographer.models import MODEL_PROFILE_KEY

    print(f"model:   {MODEL_PROFILE_KEY}    prompts: {fingerprint}")
    print("         ./workspace is emptied before each case — see clear_workspace()\n")
    for number, case in enumerate(cases, start=1):
        print(f"[{number}/{len(cases)}] {case.id} ({case.slug}) … ", end="", flush=True)
        clear_workspace()
        run = run_case(case, fingerprint, MODEL_PROFILE_KEY)
        append_run(case, run)
        if run["error"]:
            print(f"FAILED after {run['seconds']}s — {run['error'][:90]}")
        else:
            score = score_case(case, run["guide"])
            print(f"{score.found}/{score.total} in {run['seconds']}s")
    print()


# --------------------------------------------------------------------------- #
# Scoring and reporting
# --------------------------------------------------------------------------- #


def latest_scores(cases: Sequence[Case]) -> tuple[list[CaseScore], list[str]]:
    """Score each case's most recent recorded run, and collect staleness warnings."""
    current = prompt_fingerprint()
    scores: list[CaseScore] = []
    warnings: list[str] = []

    for case in cases:
        runs = read_runs(case.id)
        if not runs:
            scores.append(score_case(case, None, error="never run"))
            continue
        run = runs[-1]
        scores.append(score_case(case, run.get("guide"), error=run.get("error")))
        if run.get("prompt_fingerprint") != current:
            warnings.append(
                f"{case.id}: recorded under prompts {run.get('prompt_fingerprint')}, "
                f"not the current {current}"
            )
    return scores, warnings


def report(cases: Sequence[Case], scores: Sequence[CaseScore], warnings: Sequence[str]) -> None:
    by_id = {case.id: case for case in cases}
    header = f"{'case':<12}{'repo':<22}{'facts':>7}{'present':>9}  {'missing'}"
    print(header)
    print("-" * len(header))
    for score in scores:
        case = by_id[score.case_id]
        missing = ", ".join(score.missing) if score.missing else "—"
        if score.error:
            missing = f"[{score.error[:60]}]"
        print(f"{case.id:<12}{case.slug:<22}{score.total:>7}{score.found:>9}  {missing}")
    print("-" * len(header))

    total = tally(scores)
    print(f"{'total':<12}{'':<22}{total.total:>7}{total.found:>9}\n")
    print(total)

    if total.failed_cases:
        print(
            f"\n{total.failed_cases} of {total.cases} cases produced no guide at all. "
            "Those facts count as missing — a run that died found nothing, and "
            "dropping it from the denominator would reward failing."
        )
    if warnings:
        print("\nStale records — these guides predate the prompts now on disk:")
        for warning in warnings:
            print(f"  ! {warning}")
        print("  Re-run without --score-only before reading anything into the number.")

    print(
        "\nOne sample per case. The agent chooses which files to open, and that "
        "choice moves between runs of an unchanged system — sweep twice before "
        "believing a small difference, and see --history for the noise floor."
    )


def show_history(cases: Sequence[Case]) -> None:
    """Every recorded run, so the spread is visible rather than asserted."""
    current = prompt_fingerprint()
    stale = False
    for case in cases:
        runs = read_runs(case.id)
        print(f"\n{case.id} ({case.slug}) — {len(runs)} recorded run(s)")
        if not runs:
            print("  never run")
            continue
        for run in runs:
            score = score_case(case, run.get("guide"), error=run.get("error"))
            fingerprint = run.get("prompt_fingerprint", "????????")
            mark = " " if fingerprint == current else "*"
            stale = stale or mark == "*"
            outcome = run["error"][:50] if run.get("error") else f"{score.found}/{score.total}"
            print(
                f" {mark}{run.get('recorded_at', '?'):<27}{fingerprint}  "
                f"{run.get('model', '?'):<38}{outcome:>8}  {run.get('seconds', '?')}s"
            )
    if stale:
        print("\n* recorded under prompts other than the ones currently on disk.")


# --------------------------------------------------------------------------- #


def select(cases: tuple[Case, ...], wanted: list[str]) -> list[Case]:
    if not wanted:
        return list(cases)
    known = {case.id for case in cases}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise SystemExit(
            f"no such case(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(known))}"
        )
    return [case for case in cases if case.id in set(wanted)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--case", action="append", default=[], metavar="ID",
        help="run/score one case only; repeatable",
    )
    parser.add_argument(
        "--score-only", action="store_true",
        help="score the recorded guides without calling a model",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="list every recorded run for each case, then exit",
    )
    parser.add_argument(
        "--fail-under", type=float, default=None, metavar="PCT",
        help="exit 1 if the percentage of facts present falls below PCT",
    )
    args = parser.parse_args()

    cases = load_cases()
    selected = select(cases, args.case)

    shown = DATASET.relative_to(Path.cwd()) if DATASET.is_relative_to(Path.cwd()) else DATASET
    print(f"dataset: {shown}")
    print(f"         {len(selected)} case(s), {total_facts(tuple(selected))} facts")

    if args.history:
        show_history(selected)
        return

    if not args.score_only:
        sweep(selected, prompt_fingerprint())
    elif not any(read_runs(case.id) for case in selected):
        raise SystemExit(
            "nothing recorded yet, so there is nothing to score. Run without "
            "--score-only first — that is the expensive half."
        )

    scores, warnings = latest_scores(selected)
    report(selected, scores, warnings)

    if args.fail_under is not None and tally(scores).percent < args.fail_under:
        raise SystemExit(f"below the {args.fail_under:.0f}% floor")


if __name__ == "__main__":
    main()
