"""Phase 5: a tiny eval set, and one number to read off it.

Everything before this phase was checked by looking. Phase 2's plan-first
behaviour, Phase 3's token drop, Phase 4's separate contexts — each was verified
once, on one run, by a human reading a transcript. That works exactly until the
system is worth changing again, and the honest summary of Phase 4 was already
*"scope discipline is imperfect, in every run observed so far"*, which is a
sentence you can only write about runs you happened to watch.

So this package holds a fixed set of repositories, a fixed set of facts a good
guide about each one should contain, and a scorer. One command produces one
number:

    uv run scripts/run_evals.py

and every phase after this one — the link-checker, skills, approval gates — can
be judged against the same number instead of against a fresh impression.

## What is in here

| File | What it is |
|---|---|
| `known_repos.jsonl` | The dataset: one case per line — repo, question, expected facts |
| `dataset.py` | Loads and validates it into `Case` and `Fact` objects |
| `scoring.py` | Given a guide and a case, which facts are present |
| `results/` | Recorded runs, git-ignored — written by the runner, read by `--score-only` |

`tests/test_evals.py` checks two different things about all of this, and the
second is the one worth knowing about: it verifies the dataset **against the
live repositories**, so an expectation that has quietly stopped being true fails
as a bad expectation rather than as a bad guide.

## Why the facts are paths and identifiers, not prose

A fact here is one of two things: a repo-relative path, or a term that really
occurs in a named file. Both are checkable twice over — against the guide by
substring, and against GitHub by fetching the file — which is what keeps the
score from being an opinion.

The obvious alternative is to ask a model whether the guide "correctly explains
the architecture". That measures a second model's judgement as much as this
system's output, costs a request per fact, and moves when nobody changed
anything. A path either appears or it does not.

The cost of that choice is real and worth stating: **this scores recall of
specific citations, not whether the guide is good.** A guide that names every
expected file and explains all of them wrongly scores full marks here. What the
number is for is *movement* — the same dataset, the same questions, before and
after a change — not for grading a run in isolation.

## What it deliberately does not measure

Whether a cited path exists. That is the opposite direction — precision rather
than recall — and it is Phase 6's whole subject. Doing it here would fold two
phases into one measurement and leave neither attributable, which is the mistake
this build plan is organised to avoid.
"""
