r"""Does this guide mention this fact? The whole of the eval's judgement.

Deliberately the dullest module in the project: no model, no network, no state.
Given a string and a `Case`, it returns which facts are present. That dullness
is the point — a scorer that is itself probabilistic cannot tell you whether a
change to the agent moved anything, because both sides of the comparison move.

## What "mentions" means

Not a bare `in`. Two adjustments, and both exist because a guide is markdown
written by a language model, not a data structure:

**Markdown noise is removed first.** Paths and identifiers arrive wrapped —
`` `src/flask/app.py` ``, `**HTTPAdapter**` — and Gemini in particular escapes
underscores in prose, so `add_url_rule` reaches the page as `add\_url\_rule`.
Backticks, asterisks and backslashes are stripped from both sides before
matching, so the fact is scored on what the reader sees rather than on how it
was marked up. Whitespace is collapsed for the same reason: a term can be broken
across a line.

**The match respects word edges.** A plain substring test would credit the term
`Session` to a guide that only ever says "session management", which is the
failure mode that turns an eval into a rubber stamp. `(?<!\w)…(?!\w)` rather
than `\b…\b` because a needle can end in punctuation — `app.use` does — and
`\b` is defined against the character next to it, so it silently means different
things depending on the needle.

That still leaves the choice of terms doing real work, and the dataset's job is
to pick identifiers distinctive enough to survive it: `HTTPAdapter` and
`add_url_rule`, never a bare `Client` or `Command`. Where a concept has no
distinctive spelling, it belongs in the dataset as a path.

## What a score is not

`CaseScore` reports presence, and nothing about correctness. A guide that names
`src/requests/adapters.py` and describes it as a JSON parser scores the same as
one that gets it right. This measures citation recall, which is one property of
a good guide and not the only one — see this package's docstring for why that
trade was made on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tests.evals.dataset import Case, Fact

# Backticks, asterisks and backslashes carry markdown emphasis and escaping, and
# none of them is ever part of a path or an identifier — so removing them can
# only remove markup. Underscores are deliberately NOT in here: stripping them
# would make `_client.py` and `client.py` the same string, and httpx's leading
# underscores are a real fact about that repository's layout.
_MARKUP = re.compile(r"[`*\\]")
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower-case, unwrap markdown, and collapse whitespace.

    Applied to the guide and to each expected string alike — comparing a
    normalised haystack against a raw needle would fail on any fact containing a
    capital letter, which is most of them.
    """
    return _WHITESPACE.sub(" ", _MARKUP.sub("", text.lower()))


def mentions(guide: str, expected: str) -> bool:
    """Is `expected` present in `guide` as its own word rather than inside one?"""
    needle = normalise(expected)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", normalise(guide)) is not None


def fact_is_present(guide: str, fact: Fact) -> bool:
    """Any one alternative is enough — `any_of` spells one claim several ways."""
    return any(mentions(guide, alternative) for alternative in fact.any_of)


@dataclass(frozen=True)
class CaseScore:
    """How one guide did against one case's expectations."""

    case_id: str
    present: tuple[str, ...]
    missing: tuple[str, ...]
    error: str | None = None
    """Set when there is no guide to score — the run failed. The case still
    counts against the denominator: a run that died produced no facts, and
    quietly dropping it would raise the score for failing."""

    @property
    def total(self) -> int:
        return len(self.present) + len(self.missing)

    @property
    def found(self) -> int:
        return len(self.present)


def score_case(case: Case, guide: str | None, error: str | None = None) -> CaseScore:
    """Score one guide. `None` or empty means the run produced nothing."""
    if not guide:
        return CaseScore(
            case_id=case.id,
            present=(),
            missing=tuple(fact.id for fact in case.facts),
            error=error or "no guide was recorded for this case",
        )

    present = tuple(fact.id for fact in case.facts if fact_is_present(guide, fact))
    missing = tuple(fact.id for fact in case.facts if fact.id not in present)
    return CaseScore(case_id=case.id, present=present, missing=missing, error=error)


@dataclass(frozen=True)
class Total:
    """The number the phase is defined by, plus enough context to read it."""

    found: int
    total: int
    cases: int
    failed_cases: int

    @property
    def percent(self) -> float:
        return 100.0 * self.found / self.total if self.total else 0.0

    def __str__(self) -> str:
        return f"{self.found} of {self.total} expected facts present ({self.percent:.0f}%)"


def tally(scores: Iterable[CaseScore]) -> Total:
    scores = list(scores)
    return Total(
        found=sum(score.found for score in scores),
        total=sum(score.total for score in scores),
        cases=len(scores),
        failed_cases=sum(1 for score in scores if score.error),
    )
