"""Phase 6's tests: does the link-checker actually catch an invented path?

The definition of done for this phase is a sentence about behaviour — *feed the
doc-writer a fake file path and confirm the link-checker flags it before a human
ever sees the output* — and the interesting thing about a delegate with no model
in it is that the sentence becomes a unit test. No sampling, no quota, no
waiting: plant a guide citing `src/requests/router.py`, run the checker, assert
the alarm. `test_the_definition_of_done` is that test, and it runs in
milliseconds.

`scripts/prove_link_checker.py` does the same thing the expensive way, with a
real doc-writer writing a real guide from poisoned notes, because "the checker
rejects a string I wrote" and "the checker rejects what the system actually
produced" are different claims and the phase deserves both.

Three layers here, and the middle one is the one that would be tempting to skip:

**The extractor** — what counts as a citation. Every false-positive class the
module claims to survive gets a test, because the failure mode of a safety net
that cries wolf is that someone switches it off.

**The verdict** — that a clean report and a failed report are distinguishable,
and that "could not check" never reads as "nothing wrong". A checker that
reports success when it found nothing to look at is worse than no checker.

**The sub-agent** — that the graph parses a brief a model plausibly wrote, reads
the workspace, and returns its verdict as a message the parent can read. Run
against a real `FilesystemBackend` in a temp directory, because the thing being
checked is the handoff, and a mocked backend would only confirm our own
assumptions about it.
"""

from __future__ import annotations

import pytest
import requests
from deepagents.backends import FilesystemBackend

from repo_cartographer.citations import (
    CitationReport,
    check_citations,
    cited_paths,
    is_file_citation,
)
from repo_cartographer.link_checker import (
    DEFAULT_GUIDE_PATH,
    build_link_checker,
    parse_brief,
)
from repo_cartographer.tools import GitHubError, get_repo_tree

# A stand-in for psf/requests, small enough to read and real enough to reason
# about. Every path here exists in that repository; none of the fakes below does.
TREE = [
    "src/requests/__init__.py",
    "src/requests/api.py",
    "src/requests/adapters.py",
    "src/requests/models.py",
    "src/requests/sessions.py",
    "tests/test_requests.py",
    "pyproject.toml",
    "README.md",
    ".github/workflows/run-tests.yml",
]

FAKE_PATH = "src/requests/router.py"


def _guide(*extra: str) -> str:
    """A guide in the shape DOC_WRITER_PROMPT asks for, plus whatever you add."""
    body = "\n".join(f"- {line}" for line in extra)
    return f"""### What this repository is
`psf/requests` is a Python HTTP library.

### Architecture
- **`src/requests/api.py`** — the module-level `requests.get` and friends.
- **`src/requests/sessions.py`** — the `Session` object, see `Session.request()`.
{body}

### Where things happen
| What to change | File | Function |
| :--- | :--- | :--- |
| Wire-level sending | `src/requests/adapters.py` | `HTTPAdapter.send()` |
"""


# --------------------------------------------------------------------------- #
# The definition of done.
# --------------------------------------------------------------------------- #


def test_the_definition_of_done() -> None:
    """A guide citing a file that does not exist must not pass the check.

    Phase 6's whole reason for existing, in six lines. Note what makes it
    assertable at all: the checker holds no model, so the outcome is the same
    every time it runs, and "the agent confidently cited a file that is not
    there" stops being a thing you hope you would notice.
    """
    report = check_citations("psf", "requests", _guide(f"**`{FAKE_PATH}`** — routing."), tree=TREE)

    assert not report.is_clean
    assert report.missing == (FAKE_PATH,)
    assert FAKE_PATH in report.summary()
    assert "NOT FOUND" in report.summary()


def test_the_same_guide_without_the_fake_path_passes() -> None:
    """The control arm. A check that flags everything has proved nothing."""
    report = check_citations("psf", "requests", _guide(), tree=TREE)

    assert report.is_clean
    assert not report.missing
    assert "src/requests/sessions.py" in report.verified


# --------------------------------------------------------------------------- #
# The extractor: what counts as a citation, and what must not.
# --------------------------------------------------------------------------- #


def test_markdown_wrapping_does_not_hide_a_path() -> None:
    """Backticks and bold are how every guide writes a path, so they must not matter."""
    for wrapped in (f"`{FAKE_PATH}`", f"**`{FAKE_PATH}`**", f"| `{FAKE_PATH}` |"):
        assert FAKE_PATH in cited_paths(wrapped)


def test_a_directory_citation_is_verified_not_flagged() -> None:
    """`get_repo_tree` returns files only, but guides legitimately cite modules.

    Calling `src/requests/` a fake path would be the checker inventing its own
    alarm out of a correct citation — the single most damaging thing a safety net
    can do, because it is what gets the net taken down.
    """
    report = check_citations("psf", "requests", "The package lives in `src/requests/`.", tree=TREE)
    assert report.verified == ("src/requests",)
    assert not report.missing


def test_a_leading_dot_survives_extraction() -> None:
    """`.github/workflows/...` is a real path whose first character is a dot.

    Trimming punctuation off both ends is the obvious way to clean a token, and
    it would turn every dotfile citation into a fabricated miss.
    """
    report = check_citations(
        "psf", "requests", "CI runs from `.github/workflows/run-tests.yml`.", tree=TREE
    )
    assert report.verified == (".github/workflows/run-tests.yml",)
    assert not report.missing


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("The `req`/`res` pair is passed to each handler.", "a phrase, not a path"),
        ("It supports sync/async clients.", "a phrase, not a path"),
        ("`psf/requests` is a Python HTTP library.", "the repository's own name"),
        ("Dispatch happens in `Session.request()`.", "a method, not a file"),
        ("The `flask.json` module wraps the stdlib.", "a dotted module, not a file"),
        ("Requires Python 3.12 and urllib3 2.31.0.", "version numbers"),
        ("Docs at https://requests.readthedocs.io/en/latest/api.py", "a URL"),
    ],
)
def test_things_that_look_like_paths_are_never_accused(text: str, why: str) -> None:
    """Each of these is path-shaped and none is a claim that a file exists.

    `flask.json` is the sharpest of them: it is a real module reference whose
    last component is a real file extension, so a checker that only asked "does
    this end in something file-like" would confidently call a correct sentence a
    fabrication.
    """
    report = check_citations("psf", "requests", text, tree=TREE)
    assert not report.missing, f"{text!r} was flagged, but it is {why}"


def test_a_bare_filename_is_verified_but_never_flagged() -> None:
    """The blind spot the slash rule buys, asserted so it stays deliberate."""
    assert not is_file_citation("setup.py")
    assert is_file_citation("src/requests/setup.py")

    verified = check_citations("psf", "requests", "See `pyproject.toml`.", tree=TREE)
    assert verified.verified == ("pyproject.toml",)

    # Invented, and not caught — by design, and counted rather than silently
    # dropped, so the report's own margin of error is visible.
    unflagged = check_citations("psf", "requests", "See `setup.py`.", tree=TREE)
    assert not unflagged.missing
    assert unflagged.ignored == 1


def test_a_path_is_reported_once_however_often_it_is_cited() -> None:
    guide = f"`{FAKE_PATH}` does routing. See `{FAKE_PATH}` again, and `{FAKE_PATH}`."
    assert check_citations("psf", "requests", guide, tree=TREE).missing == (FAKE_PATH,)


# --------------------------------------------------------------------------- #
# The verdict: what the orchestrator reads.
# --------------------------------------------------------------------------- #


def test_an_unreachable_repository_is_not_a_clean_pass() -> None:
    """The failure this codebase keeps meeting: succeeding somewhere useless.

    If a check that could not run reported "no problems found", every guide
    produced during a GitHub outage would be handed over stamped as verified.
    """
    report = CitationReport("psf", "requests", (), (), 0, error="GitHub said 404")
    assert not report.is_clean
    assert "could not run" in report.summary()


def test_a_clean_verdict_says_so_in_words() -> None:
    """The orchestrator acts on this text, so it has to be readable as well as right."""
    summary = check_citations("psf", "requests", _guide(), tree=TREE).summary()
    assert "NOT FOUND" not in summary
    assert "exists in the repository" in summary


# --------------------------------------------------------------------------- #
# The sub-agent: brief in, verdict out, no model anywhere.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "brief",
    [
        "owner=psf repo=requests guide=/guide.md",
        "Check psf/requests. The guide is at /guide.md.",
        "owner: psf, repo: requests — verify the citations in /guide.md please",
    ],
)
def test_a_brief_a_model_might_plausibly_write_is_understood(brief: str) -> None:
    """Briefs are free text an orchestrator composed, not a validated payload."""
    assert parse_brief(brief)[:3] == ("psf", "requests", "/guide.md")


def test_a_notes_path_is_not_mistaken_for_a_repository() -> None:
    """`notes/src.md` has a slash and would parse as owner/repo.

    Checking the wrong repository is the worst available outcome here: every path
    would come back missing and a perfectly good guide would be condemned whole.
    """
    owner, repo, path, _ = parse_brief("read /notes/src.md then check psf/requests")
    assert (owner, repo) == ("psf", "requests")
    assert path == "/notes/src.md"


def test_a_brief_with_no_repository_returns_an_error_not_a_pass(tmp_path) -> None:
    backend = FilesystemBackend(root_dir=tmp_path)
    backend.write(DEFAULT_GUIDE_PATH, _guide())

    result = build_link_checker(backend).invoke({"messages": [("user", "check the guide")]})

    verdict = result["messages"][-1].text
    assert "could not run" in verdict
    assert "named no repository" in verdict


def test_a_missing_guide_file_returns_an_error_not_a_pass(tmp_path) -> None:
    """The doc-writer reported success and wrote nothing, or wrote it elsewhere.

    Exactly the `workspace/workspace/notes/` failure from Phase 4b, one layer
    along — and the reason this cannot be allowed to look like a clean check.
    """
    checker = build_link_checker(FilesystemBackend(root_dir=tmp_path))

    result = checker.invoke({"messages": [("user", "owner=psf repo=requests guide=/guide.md")]})

    verdict = result["messages"][-1].text
    assert "could not run" in verdict
    assert "did not write it" in verdict


def test_a_defaulted_guide_path_is_reported_rather_than_assumed(tmp_path) -> None:
    """Leniency is fine; silent leniency is how a handoff rots unnoticed."""
    backend = FilesystemBackend(root_dir=tmp_path)
    backend.write(DEFAULT_GUIDE_PATH, _guide())

    result = build_link_checker(backend).invoke(
        {"messages": [("user", "owner=psf repo=requests")]}
    )

    assert "named no guide path" in result["messages"][-1].text


# --------------------------------------------------------------------------- #
# Live: the checker and the explorers must agree on what exists.
# --------------------------------------------------------------------------- #


def test_against_the_real_repository_tree() -> None:
    """The end of the chain, with nothing stubbed but the guide itself.

    `check_citations` fetches through the same `get_repo_tree` the explorers read
    with, so the two cannot disagree about what a repository contains. Asserted
    live because that agreement is the reason the verdict is worth anything, and
    a fixture would only confirm our own idea of the tree.
    """
    try:
        get_repo_tree("psf", "requests")
    except (GitHubError, requests.RequestException) as exc:
        pytest.skip(f"cannot reach GitHub: {str(exc)[:120]}")

    guide = _guide(f"**`{FAKE_PATH}`** — routing.")
    report = check_citations("psf", "requests", guide)

    assert report.missing == (FAKE_PATH,)
    assert "src/requests/sessions.py" in report.verified
