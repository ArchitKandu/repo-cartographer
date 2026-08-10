"""Load `known_repos.jsonl` into checked `Case` objects, or refuse to.

The validation here is not ceremony. This file is the one input to the eval that
nobody runs — a typo in a fact's `kind`, a missing `found_in`, two facts sharing
an id — would not raise anywhere; it would quietly change the denominator, and
the score would move for a reason that has nothing to do with the agent. So the
loader is strict, raises `DatasetError` naming the line, and `tests/test_evals.py`
runs it with no model and no network as its first assertion.

## The shape of a case

    {"id": "requests", "owner": "psf", "repo": "requests",
     "question": "Explore … and explain its architecture …",
     "facts": [
       {"id": "sessions-module", "kind": "path",
        "any_of": ["src/requests/sessions.py"], "why": "…"},
       {"id": "http-adapter", "kind": "term", "any_of": ["HTTPAdapter"],
        "found_in": "src/requests/adapters.py", "why": "…"}
     ]}

`any_of` is a list because a fact is a claim, not a spelling. Flask's
application class genuinely lives in two files since the sansio refactor, so
`["src/flask/app.py", "src/flask/sansio/app.py"]` is one fact with two correct
citations — not two facts, and not a reason to pick a favourite.

`why` is required, and it is the field that keeps the set honest. A fact you
cannot justify in a sentence is a fact you added because it was easy to check,
and a dataset made of those measures convenience.

## Why two kinds

Both are verifiable against the real repository, and that is the entire reason
the split exists rather than a free-text `expect` field:

- **`path`** — every alternative must be a real file in the repo tree.
- **`term`** — every alternative must really occur in the file named by
  `found_in`.

`tests/test_evals.py` checks exactly that, live. It is not hypothetical
bookkeeping: the first draft of this dataset expected `lib/router/index.js` from
`expressjs/express`, which every account of Express describes and which the
repository has not contained since routing moved out to its own package. An
unverified dataset would have scored that as a miss forever, and the miss would
have read as the agent's failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "known_repos.jsonl"

PATH = "path"
TERM = "term"
KINDS = (PATH, TERM)


class DatasetError(Exception):
    """`known_repos.jsonl` says something that cannot be scored."""


@dataclass(frozen=True)
class Fact:
    """One thing a guide about this repository should contain.

    Scored as present if *any* alternative in `any_of` appears in the guide —
    see `scoring.py` for what "appears" means, which is not quite substring.
    """

    id: str
    kind: str
    any_of: tuple[str, ...]
    why: str
    found_in: str | None = None
    """For `term` facts: the repo file the term must really occur in. Not a
    claim about the guide — the guide need not cite this path — but the evidence
    that the term is a fact about this repository rather than a memory."""

    @property
    def is_path(self) -> bool:
        return self.kind == PATH


@dataclass(frozen=True)
class Case:
    """One repository, one question, and what a good answer would mention."""

    id: str
    owner: str
    repo: str
    question: str
    facts: tuple[Fact, ...]

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def paths_to_verify(self) -> set[str]:
        """Every repo path this case asserts exists, from both kinds of fact.

        `found_in` is in here too: a term fact whose evidence file has been
        renamed is as broken as a path fact pointing at a deleted one, and it
        breaks more quietly, because nothing about the term itself looks wrong.
        """
        paths = {alt for fact in self.facts if fact.is_path for alt in fact.any_of}
        return paths | {f.found_in for f in self.facts if f.found_in}


def _require(condition: bool, where: str, message: str) -> None:
    if not condition:
        raise DatasetError(f"{where}: {message}")


def _parse_fact(raw: object, where: str) -> Fact:
    _require(isinstance(raw, dict), where, "a fact must be an object")
    assert isinstance(raw, dict)  # narrowing for mypy; _require already raised

    for key in ("id", "kind", "any_of", "why"):
        _require(key in raw, where, f"fact is missing {key!r}")

    fact_id = raw["id"]
    _require(isinstance(fact_id, str) and bool(fact_id), where, "fact id must be a non-empty string")
    where = f"{where} fact {fact_id!r}"

    kind = raw["kind"]
    _require(kind in KINDS, where, f"kind must be one of {KINDS}, not {kind!r}")

    alternatives = raw["any_of"]
    _require(
        isinstance(alternatives, list) and bool(alternatives),
        where,
        "any_of must be a non-empty list",
    )
    _require(
        all(isinstance(a, str) and a.strip() for a in alternatives),
        where,
        "every any_of entry must be a non-empty string",
    )

    why = raw["why"]
    _require(isinstance(why, str) and bool(why.strip()), where, "why must say something")

    found_in = raw.get("found_in")
    if kind == TERM:
        _require(
            isinstance(found_in, str) and bool(found_in),
            where,
            "a term fact needs found_in — the repo file the term really occurs in",
        )
    else:
        # Not pedantry: a path fact carrying found_in is almost certainly a term
        # fact whose kind was mistyped, and it would be scored as a path.
        _require(found_in is None, where, "a path fact must not carry found_in")
        for alternative in alternatives:
            _require(
                not alternative.startswith("/") and " " not in alternative,
                where,
                f"{alternative!r} is not a repo-relative path",
            )

    return Fact(
        id=fact_id,
        kind=kind,
        any_of=tuple(alternatives),
        why=why,
        found_in=found_in,
    )


def _parse_case(raw: object, where: str) -> Case:
    _require(isinstance(raw, dict), where, "a case must be an object")
    assert isinstance(raw, dict)  # narrowing for mypy; _require already raised

    for key in ("id", "owner", "repo", "question", "facts"):
        _require(key in raw, where, f"case is missing {key!r}")

    case_id = raw["id"]
    _require(isinstance(case_id, str) and bool(case_id), where, "case id must be a non-empty string")
    where = f"{where} case {case_id!r}"

    for key in ("owner", "repo", "question"):
        value = raw[key]
        _require(isinstance(value, str) and bool(value.strip()), where, f"{key} must say something")

    facts = raw["facts"]
    _require(isinstance(facts, list) and bool(facts), where, "facts must be a non-empty list")

    parsed = tuple(_parse_fact(fact, where) for fact in facts)
    ids = [fact.id for fact in parsed]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    _require(not duplicates, where, f"repeated fact ids: {duplicates}")

    return Case(
        id=case_id,
        owner=raw["owner"],
        repo=raw["repo"],
        question=raw["question"],
        facts=parsed,
    )


def load_cases(path: Path = DATASET) -> tuple[Case, ...]:
    """Read the dataset, or raise `DatasetError` naming the line at fault.

    Blank lines are skipped so the file can be spaced out by hand; anything else
    that does not parse is an error rather than a skipped case, because a case
    silently dropped from the denominator raises the score.
    """
    if not path.exists():
        raise DatasetError(f"no dataset at {path}")

    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}:{number}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{where}: not valid JSON — {exc}") from exc
        cases.append(_parse_case(raw, where))

    _require(bool(cases), str(path), "the dataset is empty")

    ids = [case.id for case in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    _require(not duplicates, str(path), f"repeated case ids: {duplicates}")

    return tuple(cases)


def total_facts(cases: tuple[Case, ...]) -> int:
    """The denominator of the score, in one place so it is never recounted."""
    return sum(len(case.facts) for case in cases)
