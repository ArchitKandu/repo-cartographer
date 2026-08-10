"""Phase 5's tests, which are about the eval set rather than about the agent.

The score itself is not asserted here. A model's recall of specific file paths
is probabilistic and slow to measure, and turning it into a pass/fail at some
threshold would produce a test that goes red for reasons nobody can act on —
which trains you to ignore red, exactly as `test_tools.py` argues about spent
GitHub quota. The number lives in `scripts/run_evals.py`, where it is a
measurement you read.

What *is* assertable, cheaply and deterministically, is everything the number
depends on. Three layers, in ascending order of what they would catch:

**The dataset parses** (no network, no model). A mistyped `kind`, a duplicated
fact id, a term fact missing its `found_in` — none of these would raise during a
sweep. They would move the denominator, and the score would change for a reason
that has nothing to do with the agent.

**The dataset is true** (live GitHub, no model). Every path a fact expects must
exist in the real repository, and every term must really occur in the file that
claims it. This is the layer that earns its keep: the first draft of the dataset
expected `lib/router/index.js` from `expressjs/express` — a path every account
of Express describes, which the repository has not had since routing moved to
its own package. Unverified, that would have scored as a miss on every run
forever, and read as the agent's failure rather than the dataset's.

**The scorer scores** (no network, no model). Its two judgement calls — markdown
noise, word edges — are the difference between a measurement and a rubber stamp,
so both get their own test, including the near-miss each is there to reject.
"""

from __future__ import annotations

import pytest
import requests

from repo_cartographer.tools import GitHubError, get_file_contents, get_repo_tree
from tests.evals.dataset import KINDS, Case, DatasetError, Fact, load_cases, total_facts
from tests.evals.scoring import (
    CaseScore,
    fact_is_present,
    mentions,
    normalise,
    score_case,
    tally,
)

CASES = load_cases()
"""Loaded at import time on purpose: a dataset that cannot be parsed should stop
collection with the DatasetError naming the bad line, rather than fail 30 tests
identically."""

BY_ID = {case.id: case for case in CASES}


def _fact(**overrides: object) -> Fact:
    """A minimal valid Fact, for the scorer tests that need one to hand."""
    fields: dict = {"id": "f", "kind": "path", "any_of": ("src/x.py",), "why": "because"}
    fields.update(overrides)
    return Fact(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The dataset parses — no network, no model.
# --------------------------------------------------------------------------- #


def test_the_dataset_loads_and_is_not_trivially_small() -> None:
    """Phase 5 asks for 5-10 repositories; fewer makes the number too coarse to move."""
    assert 5 <= len(CASES) <= 10
    assert total_facts(CASES) >= 20


def test_case_ids_are_unique_and_usable_as_filenames() -> None:
    """Case ids name both a `--case` argument and a file in results/."""
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    assert all(id.replace("-", "").replace("_", "").isalnum() for id in ids)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_fact_is_shaped_the_way_the_scorer_expects(case: Case) -> None:
    for fact in case.facts:
        assert fact.kind in KINDS
        assert fact.any_of and all(alternative.strip() for alternative in fact.any_of)
        assert fact.why.strip(), f"{fact.id} has no justification"
        # The kind/found_in pairing is what the live checks below dispatch on, so
        # a mismatch here silently skips a fact's verification rather than
        # failing it.
        assert (fact.found_in is None) == fact.is_path


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_question_names_the_repository_it_is_about(case: Case) -> None:
    """The brief has to stand alone; the agent is given no other context.

    `ask()` passes the question through as the only user message, so a question
    that says "explore this repo" has nothing to explore. Cheap to get wrong when
    adding a case by copying an existing line.
    """
    assert case.slug in case.question, f"{case.id}'s question never names {case.slug}"


def test_a_term_fact_without_found_in_is_rejected(tmp_path) -> None:
    """The loader's strictness is the point, so one negative case pins it.

    Chosen because it is the mistake that would do the most damage quietly: the
    fact would still be scored against the guide, but nothing would ever check
    that the term is true of the repository at all.
    """
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id": "x", "owner": "o", "repo": "r", "question": "q", "facts": '
        '[{"id": "f", "kind": "term", "any_of": ["Thing"], "why": "w"}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="found_in"):
        load_cases(bad)


# --------------------------------------------------------------------------- #
# The dataset is true — live GitHub, no model.
# --------------------------------------------------------------------------- #
#
# Costs one tree call per case plus one file read per distinct `found_in`, all
# module-scoped and cached, against the 5000/hour authenticated pool. Network
# failures skip rather than fail: an unreachable GitHub says nothing about
# whether the dataset is right.

_trees: dict[str, set[str]] = {}
_files: dict[tuple[str, str], str] = {}


def _tree(case: Case) -> set[str]:
    if case.slug not in _trees:
        try:
            _trees[case.slug] = set(get_repo_tree(case.owner, case.repo))
        except (GitHubError, requests.RequestException) as exc:
            pytest.skip(f"cannot read {case.slug} from GitHub: {str(exc)[:120]}")
    return _trees[case.slug]


def _file(case: Case, path: str) -> str:
    key = (case.slug, path)
    if key not in _files:
        try:
            _files[key] = get_file_contents(case.owner, case.repo, path)
        except (GitHubError, ValueError, requests.RequestException) as exc:
            pytest.skip(f"cannot read {case.slug}:{path}: {str(exc)[:120]}")
    return _files[key]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_path_the_dataset_expects_really_exists(case: Case) -> None:
    """Including each term fact's `found_in`, which fails the most quietly."""
    tree = _tree(case)
    missing = sorted(path for path in case.paths_to_verify if path not in tree)
    assert not missing, (
        f"{case.slug} does not contain {missing} — the dataset expects a path the "
        "repository no longer has, so the agent would be scored against a fiction"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_term_the_dataset_expects_really_occurs(case: Case) -> None:
    """A term must be a fact about the repo, not a recollection of the ecosystem.

    Matched case-insensitively because the dataset's job is to name a real thing,
    not to pin its casing — `WSGI` in a docstring and `wsgi_app` in a signature
    are both adequate evidence that the term belongs to this repository.
    """
    for fact in case.facts:
        if fact.found_in is None:
            continue
        body = _file(case, fact.found_in).lower()
        present = [a for a in fact.any_of if a.lower() in body]
        assert present, (
            f"{case.slug}:{fact.found_in} contains none of {list(fact.any_of)} "
            f"(fact {fact.id!r}) — the expectation is stale or was never true"
        )


# --------------------------------------------------------------------------- #
# The scorer scores — no network, no model.
# --------------------------------------------------------------------------- #


def test_a_cited_path_counts() -> None:
    guide = "| Session state | `src/requests/sessions.py` | `Session.request()` |"
    assert fact_is_present(guide, _fact(any_of=("src/requests/sessions.py",)))


def test_a_partial_path_does_not_count() -> None:
    """"sessions.py" alone is not the citation the guide is supposed to carry.

    The whole value of the where-things-happen table is that a reader can open
    the file, so a bare basename is a different and weaker claim.
    """
    guide = "Session lives in sessions.py somewhere under src."
    assert not fact_is_present(guide, _fact(any_of=("src/requests/sessions.py",)))


def test_any_of_means_either_spelling_is_correct() -> None:
    """Flask's app class really does live in two files; both citations are right."""
    fact = _fact(any_of=("src/flask/app.py", "src/flask/sansio/app.py"))
    assert fact_is_present("see `src/flask/sansio/app.py`", fact)
    assert fact_is_present("see `src/flask/app.py`", fact)
    assert not fact_is_present("see `src/flask/blueprints.py`", fact)


def test_markdown_wrapping_and_escaping_do_not_hide_a_term() -> None:
    """Gemini escapes underscores in prose; every model wraps code in backticks.

    Without normalisation this fact would be scored as missing on a guide that
    states it perfectly, which is the failure that makes an eval untrustworthy in
    the direction nobody checks.
    """
    fact = _fact(kind="term", any_of=("add_url_rule",), found_in="src/flask/sansio/app.py")
    assert fact_is_present(r"routes register through `add\_url\_rule`", fact)
    assert fact_is_present("routes register through **add_url_rule**", fact)


def test_a_term_broken_across_a_line_still_counts() -> None:
    fact = _fact(kind="term", any_of=("web server gateway interface",), found_in="src/flask/app.py")
    assert fact_is_present("Flask is a web server\ngateway interface framework.", fact)


def test_a_term_inside_a_longer_word_does_not_count() -> None:
    """The rubber-stamp failure: a plain `in` credits `Session` to "sessions".

    Note what this test does *not* claim. The matcher rejects the substring case
    and nothing more — a guide saying "session handling" still scores the term
    `Session`, because at that point the two really are the same word. That gap
    is why the dataset prefers identifiers no English sentence contains
    (`HTTPAdapter`, `add_url_rule`) and expresses everything else as a path.
    """
    fact = _fact(kind="term", any_of=("Session",), found_in="src/requests/sessions.py")
    assert not fact_is_present("It persists cookies across sessions.", fact)
    assert fact_is_present("The `Session` object persists cookies.", fact)


def test_punctuated_terms_match_at_their_edges() -> None:
    """`app.use` ends in a letter but contains a dot, which `\\b` handles wrongly."""
    fact = _fact(kind="term", any_of=("app.use",), found_in="lib/application.js")
    assert fact_is_present("Middleware is registered with `app.use()`.", fact)
    assert not fact_is_present("Middleware is registered with app.user().", fact)


def test_normalise_is_applied_to_both_sides() -> None:
    assert normalise("**`Foo Bar`**") == "foo bar"
    assert mentions("**`Foo   Bar`**", "foo bar")


# --------------------------------------------------------------------------- #
# The tally — the arithmetic behind the one number.
# --------------------------------------------------------------------------- #


def test_a_failed_run_scores_zero_rather_than_dropping_out() -> None:
    """Dropping a dead case from the denominator would reward failing.

    A sweep where four cases die and two score full marks must not report 100%.
    """
    case = CASES[0]
    score = score_case(case, None, error="ChatGoogleGenerativeAIError: 429")
    assert score.found == 0
    assert score.total == len(case.facts)
    assert score.error

    total = tally([score, CaseScore(case_id="other", present=("a", "b"), missing=())])
    assert total.total == len(case.facts) + 2
    assert total.failed_cases == 1


def test_the_total_is_the_number_the_phase_is_defined_by() -> None:
    total = tally(
        [
            CaseScore(case_id="a", present=("1", "2", "3"), missing=("4",)),
            CaseScore(case_id="b", present=("1", "2", "3"), missing=()),
        ]
    )
    assert (total.found, total.total) == (6, 7)
    assert str(total) == "6 of 7 expected facts present (86%)"
