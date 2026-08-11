"""Phase 6: does every file path this guide cites actually exist?

The failure this whole project invites is a confidently cited file that is not
there. A reader trusts a path, opens their editor, and finds nothing — and the
guide was more useful before that sentence than after it.

Four phases attacked it with instructions. Three prompts say *cite only what you
verified*, in various words, and Phase 4 added the one structural guarantee: the
doc-writer holds no GitHub tools, so it cannot describe a file nobody read. That
is a real defence and it is not the whole one, because it stops the doc-writer
*inventing* a path and does nothing about it faithfully repeating an explorer's
mistake.

This module is the check that closes that gap, and it contains **no AI code at
all** — the same property `tools.py` has, for the same reason. "This path exists"
is a fact about a repository, cheaply and exactly decidable from the file tree
Phase 1 already returns. Asking a model to decide it would replace a fact with an
opinion, cost a request, and be wrong occasionally in a way nothing downstream
could detect.

`link_checker.py` wraps these functions in a `CompiledStateGraph` so the
orchestrator can delegate to them through the ordinary `task` tool. Everything
that decides anything is here; that module only handles the plumbing.

## What counts as a citation

The input is markdown a language model wrote, so extraction is a heuristic and
worth stating plainly. Every maximal run of path characters is a candidate, and
each one lands in exactly one of three buckets:

| Bucket | When |
|---|---|
| **verified** | in the tree; a directory prefix of one; or a bare name matching an entry |
| **not found** | not verified, **and** has a slash, **and** ends in a known extension |
| **not a citation** | everything else — counted, never named |

That third bucket is what keeps the check from crying wolf, and both conditions
guarding the flag are there because of a specific false positive:

- **A recognised extension** separates a citation from a phrase with a slash in
  it. A guide says "the `req`/`res` pair", "sync/async", "GET/POST", and names
  the repository itself as `psf/requests`. All are path-shaped; none claims a
  file exists.
- **A slash** separates a path from a dotted module reference. `flask.json` and
  `httpx._models` look exactly like filenames whose extension happens to be a
  real one, and a checker that called `flask.json` a fake file would be wrong in
  the worst possible way — confidently, about a guide that was right. A bare name
  can still *verify* against any tree entry's basename; it is simply never
  accused.

Those rules buy their accuracy with two stated blind spots. **An invented
directory is not flagged** — `src/flask/nonexistent/` has no extension. **An
invented root-level filename is not flagged** — `setup.py` alone has no slash.
Both are the less damaging error, since neither promises a specific location to
open, and a check that quietly missed them would be worse than one that says so
here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from repo_cartographer.tools import GitHubError, get_repo_tree

# Extensions that make a token a claim about a file rather than a phrase with a
# slash in it. Deliberately an allowlist: the alternative — "anything after the
# last dot" — would call `Session.request` a file named `request` and flag it.
# Files with no extension at all (Makefile, Dockerfile, LICENSE) are therefore
# never flagged, only ever verified; that is the same trade as directories.
SOURCE_EXTENSIONS = frozenset(
    {
        # Python
        "py", "pyi", "pyx", "ipynb",
        # JavaScript and TypeScript
        "js", "mjs", "cjs", "jsx", "ts", "tsx", "vue", "svelte",
        # Other languages this is likely to be pointed at
        "go", "rs", "rb", "java", "kt", "kts", "scala", "swift", "php", "cs",
        "c", "h", "cc", "cpp", "hpp", "m", "mm", "sh", "bash", "zsh", "ps1",
        "sql", "r", "pl", "lua", "ex", "exs", "erl", "clj", "hs", "dart",
        # Manifests, config and docs — where an onboarding guide starts
        "json", "toml", "yaml", "yml", "ini", "cfg", "conf", "properties",
        "xml", "md", "rst", "txt", "lock", "gradle", "cmake", "mk",
        "css", "scss", "sass", "less", "html", "htm", "jinja", "j2",
    }
)

# A run of characters that could be part of a path. Backticks, asterisks,
# brackets and whitespace are all outside the class, so markdown wrapping
# (`` `src/x.py` ``, `**src/x.py**`, a table cell) delimits a candidate for free
# and needs no stripping pass.
_CANDIDATE = re.compile(r"[A-Za-z0-9_.\-/]+")

# Removed before candidates are extracted. A URL is full of slashes and dots and
# would otherwise produce a handful of confident nonsense, and the path inside a
# link target is a link, not a citation of a local file.
_URL = re.compile(r"https?://\S+|www\.\S+")

# A path segment that is only digits and dots is a version, not a filename —
# `2.31.0` and `3.12` turn up in every dependency table.
_VERSION = re.compile(r"^[\d.]+$")


@dataclass(frozen=True)
class CitationReport:
    """The verdict on one guide. `missing` being non-empty is the alarm."""

    owner: str
    repo: str
    verified: tuple[str, ...]
    missing: tuple[str, ...]
    ignored: int
    """Path-shaped strings that were not claims about files — counted, not named.

    Reported as a number because it is the check's own margin of error, and a
    check that hides how much it declined to look at is asking to be trusted
    more than it has earned."""

    error: str | None = None
    """Set when the check could not run at all — an unreachable repository, an
    unreadable guide. Distinct from `missing` being empty, which means the check
    ran and found nothing wrong."""

    @property
    def is_clean(self) -> bool:
        return not self.missing and self.error is None

    @property
    def cited(self) -> int:
        return len(self.verified) + len(self.missing)

    def summary(self) -> str:
        """The verdict as the orchestrator will read it.

        Written to be short when clean and specific when not: this text lands in
        the orchestrator's context, and the only part it has to act on is the
        list of paths that do not exist.
        """
        if self.error:
            return f"CITATION CHECK — could not run: {self.error}"

        # Shouted only when there is something to shout about. The header used to
        # read "0 NOT FOUND" on a clean run, which put the alarm phrase in front
        # of an orchestrator told to act on seeing it — a verdict that reports
        # success in the vocabulary of failure.
        verdict = "NOT FOUND" if self.missing else "not found"
        head = (
            f"CITATION CHECK — {self.owner}/{self.repo}\n"
            f"{self.cited} file path(s) cited · {len(self.verified)} verified against "
            f"the real repository tree · {len(self.missing)} {verdict}"
        )
        tail = (
            f"\n\n{self.ignored} other path-like string(s) were not file citations "
            "(no recognised file extension) and were not checked."
            if self.ignored
            else ""
        )

        if not self.missing:
            return f"{head}\n\nEvery file path the guide cites exists in the repository.{tail}"

        listed = "\n".join(f"  - {path}" for path in self.missing)
        return (
            f"{head}\n\n"
            f"NOT FOUND — these paths do not exist in {self.owner}/{self.repo}:\n"
            f"{listed}\n\n"
            "Every path above is either wrong or invented, and a reader will "
            f"trust it. Say so when you hand the guide over.{tail}"
        )


def cited_paths(guide: str) -> list[str]:
    """Every path-shaped candidate in the guide, in order, without duplicates.

    Order is preserved rather than sorted so a report reads in the same sequence
    as the document it is about — the first flagged path is the first one a
    reader would have hit.
    """
    text = _URL.sub(" ", guide)
    seen: dict[str, None] = {}

    for match in _CANDIDATE.finditer(text):
        candidate = _clean(match.group())
        if candidate and _is_path_shaped(candidate):
            seen.setdefault(candidate, None)

    return list(seen)


def _clean(token: str) -> str:
    """Trim the punctuation markdown and prose leave attached to a path.

    A trailing slash goes with it: `src/flask/` and `src/flask` are the same
    claim, and keeping both spellings would report one directory twice.

    A *leading* dot deliberately survives, which is the whole reason this is not
    a symmetric `strip`. `.github/workflows/ci.yml` and `.env.example` are real
    files whose first character is a dot, and trimming it would turn each into a
    path the repository does not contain — the checker manufacturing its own
    false alarm out of a correct citation.
    """
    return token.rstrip("./-").lstrip("/-")


def _is_path_shaped(candidate: str) -> bool:
    """Could this be a path at all? The cheap filter, before touching the tree."""
    if "/" not in candidate and "." not in candidate:
        return False
    return not all(_VERSION.match(segment) for segment in candidate.split("/"))


def is_file_citation(candidate: str) -> bool:
    """Is this an unambiguous claim that a file exists at a specific path?

    The rule separating "not found" from "not a citation", and the only thing
    standing between this check and a false accusation. Both halves matter — see
    the module docstring for the `flask.json` case that the slash requirement
    exists to survive.
    """
    if "/" not in candidate:
        return False
    _, _, extension = candidate.rpartition("/")[2].rpartition(".")
    return extension.lower() in SOURCE_EXTENSIONS


def check_citations(
    owner: str,
    repo: str,
    guide: str,
    tree: list[str] | None = None,
) -> CitationReport:
    """Check every path a guide cites against the repository's real file tree.

    `tree` is injectable so the whole check can be exercised with no network at
    all — which is how `tests/test_citations.py` proves the definition of done,
    in milliseconds, with no GitHub quota and no model. Left as `None`, it is
    fetched through the same `get_repo_tree` the explorers use, so the checker
    and the agents that read the repository agree on what exists by construction
    rather than by coincidence.
    """
    if tree is None:
        try:
            tree = get_repo_tree(owner, repo)
        except (GitHubError, ValueError) as exc:
            # Not raising: the caller is a sub-agent whose job is to return a
            # verdict, and "I could not check" is a verdict. Reported as an
            # error rather than as a clean pass, because an unreachable
            # repository must never read as "every path is fine".
            return CitationReport(owner, repo, (), (), 0, error=str(exc))

    files = set(tree)
    # Every directory implied by the tree. `get_repo_tree` returns files only —
    # deliberately, see tools.py — but a guide legitimately cites `src/flask/`
    # as a module, and calling that a fake path would be the checker's own
    # invention.
    directories = {
        "/".join(path.split("/")[:depth])
        for path in tree
        for depth in range(1, path.count("/") + 1)
    }
    basenames = {path.rpartition("/")[2] for path in tree}

    verified: list[str] = []
    missing: list[str] = []
    ignored = 0

    for candidate in cited_paths(guide):
        if candidate in files or candidate in directories:
            verified.append(candidate)
        elif "/" not in candidate and candidate in basenames:
            # A bare filename is a weaker claim than a full path — "the
            # `app.py` module" says a file by that name exists somewhere, not
            # where. Matching it anywhere in the tree is the honest reading, and
            # the alternative flags a true statement as a lie.
            verified.append(candidate)
        elif is_file_citation(candidate):
            missing.append(candidate)
        else:
            ignored += 1

    return CitationReport(
        owner=owner,
        repo=repo,
        verified=tuple(verified),
        missing=tuple(missing),
        ignored=ignored,
    )
