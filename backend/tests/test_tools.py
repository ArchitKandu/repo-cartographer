"""Phase 1 tests: the tools layer, against the real GitHub API.

Deliberately unmocked (see IMPLEMENTATION_GUIDE.md, Phase 1) — a mock of the
GitHub API would only ever confirm our own assumptions about it. The point of
this layer is to be the one part of the system we can cheaply verify for real.

Auth: the tools read GITHUB_TOKEN from .env, so these tests run against the
5000/hour authenticated quota rather than the 60/hour anonymous one, and code
search works at all (that endpoint 401s without a token).
test_tools_authenticate_with_the_token verifies the token is really being sent.

Cost control: the two repo trees are module-scoped fixtures, so a full run is
~21 API calls against 5000, plus 5 against the separate 30/minute search pool.

Run:
    uv run pytest tests/test_tools.py                       # pass/fail only
    uv run pytest tests/test_tools.py -v --log-cli-level=INFO   # stepwise logs
    uv run pytest tests/test_tools.py -m slow               # truncation probe
"""

import json
import logging
import os
import pprint

import pytest
import requests
from dotenv import load_dotenv

from repo_cartographer.tools import (
    GitHubError,
    _get,
    _headers,
    get_file_contents,
    get_repo_scopes,
    get_repo_tree,
    search_code,
)

log = logging.getLogger("phase1")

# --- Logging helpers -------------------------------------------------------
#
# Every test narrates what it asked GitHub for and what came back, so a run
# with --log-cli-level=INFO reads as a transcript rather than a row of dots.
# Visible only with live logging on (or on failure, in the captured-log
# section) — a default run stays quiet.

_PREVIEW_CHARS = 700


def step(message: str) -> None:
    """Narrate one action inside a test."""
    log.info("  → %s", message)


def show(label: str, value: object) -> None:
    """Log the real value a tool returned, shaped for reading and truncated."""
    if isinstance(value, str):
        body = f"str, {len(value)} chars\n{_clip(repr(value))}"
    elif isinstance(value, list):
        head = value[:5]
        rendered = "\n".join(f"      [{i}] {_clip(_render(v))}" for i, v in enumerate(head))
        more = f"\n      … {len(value) - len(head)} more" if len(value) > len(head) else ""
        body = f"list, {len(value)} items\n{rendered}{more}"
    else:
        body = f"{type(value).__name__}\n{_clip(_render(value))}"
    log.info("  ← %s: %s", label, body)


def _render(value: object) -> str:
    if isinstance(value, dict):
        try:
            return json.dumps(value, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            pass
    return pprint.pformat(value, width=100)


def _clip(text: str) -> str:
    if len(text) <= _PREVIEW_CHARS:
        return text
    return f"{text[:_PREVIEW_CHARS]}… [{len(text) - _PREVIEW_CHARS} chars elided]"


@pytest.fixture(autouse=True)
def announce(request):
    """Bracket each test with its name, so steps are unambiguously attributed."""
    log.info("┌─ %s", request.node.name)
    yield
    log.info("└─ done: %s", request.node.name)


# --- Fixtures: two real, deliberately stable repos -------------------------
#
# TINY is octocat's canonical demo repo: one file, one line, unchanged for
# over a decade. Good for exact-value assertions.
# NESTED is flask: real directory depth, a pyproject.toml, and a binary file,
# without being big enough to make the tree call slow.

TINY_OWNER, TINY_REPO = "octocat", "Hello-World"
TINY_FILE, TINY_CONTENTS = "README", "Hello World!\n"
# A commit that exists on octocat/Hello-World's master branch.
TINY_COMMIT = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"

NESTED_OWNER, NESTED_REPO = "pallets", "flask"
NESTED_FILE = "src/flask/__init__.py"
NESTED_BINARY = "docs/_static/debugger.png"

# LARGE is TypeScript's type checker: ~3MB of source, well past GitHub's 1MB
# inline limit, at a path that has been stable for years. Reading it is cheap
# precisely because GitHub omits the content of a file this size.
LARGE_OWNER, LARGE_REPO = "microsoft", "TypeScript"
LARGE_FILE = "src/compiler/checker.ts"


def _probe_quota() -> dict[str, int]:
    """Read the live core quota from a real request's X-RateLimit-* headers.

    Deliberately NOT the /rate_limit endpoint: with this token that endpoint
    reports a frozen `used: 0, remaining: 5000` no matter how many calls have
    been made, while a real endpoint's response headers count down accurately.
    Trusting the endpoint's body would give a budget guard that can never fire.

    Costs one core request (of 5000). Uses tools._headers() so it observes the
    same pool the tools spend — anonymous reports a limit of 60, a token 5000.

    `status` is returned so callers can tell a spent quota (a reason to skip)
    from a rejected credential (a reason to fail): a 401 carries no
    X-RateLimit-* headers at all, which would otherwise read as -1 remaining
    and quietly skip the suite over what is really a broken token.

    Goes through tools._get rather than requests.get so it inherits the same
    retry the tools have carried since Phase 5. It used to call requests.get
    directly and was, after that change, the last thing in the suite that a
    single dropped connection could fail — which is a bad property in a helper
    two tests call and a session fixture depends on.
    """
    response = _get(f"https://api.github.com/repos/{TINY_OWNER}/{TINY_REPO}")
    return {
        "status": response.status_code,
        "limit": int(response.headers.get("x-ratelimit-limit", -1)),
        "remaining": int(response.headers.get("x-ratelimit-remaining", -1)),
    }


@pytest.fixture(scope="session", autouse=True)
def require_api_budget() -> None:
    """Skip the whole module rather than fail it when GitHub's budget is spent.

    A drained rate limit is an environment problem, not a bug in our tools —
    failing here would train us to ignore red.

    With GITHUB_TOKEN the pool is 5000/hour and a ~20-call run is free; without
    one it is 60/hour and this guard does real work. Lower the floor with
    PHASE1_MIN_BUDGET=4 to run a subset on a nearly-spent quota.
    """
    load_dotenv()  # pytest does not read .env on its own; tools.py does the same
    floor = int(os.environ.get("PHASE1_MIN_BUDGET", "20"))
    try:
        quota = _probe_quota()
    except (GitHubError, requests.RequestException, ValueError) as exc:
        # GitHubError since Phase 5: an unreachable API now surfaces as one,
        # after the retry in tools._get has given up. Still an environment
        # problem, so it still skips.
        pytest.skip(f"cannot reach the GitHub API: {exc}")
    if quota["status"] == 401:
        pytest.fail(
            "GitHub rejected GITHUB_TOKEN with 401 — the token is expired, "
            "malformed, or revoked. Failing rather than skipping: a broken "
            "credential is a real problem, not a busy-API problem."
        )
    log.info(
        "GitHub core budget: %s/%s left — %s",
        quota["remaining"],
        quota["limit"],
        "authenticated" if quota["limit"] > 60 else "ANONYMOUS (60/hour)",
    )
    if quota["remaining"] < floor:
        pytest.skip(
            f"core quota nearly spent ({quota['remaining']} left, need {floor})"
        )


def search_or_skip(owner: str, repo: str, query: str) -> list[dict]:
    """Run a code search, skipping if GitHub's search quota is exhausted.

    Search has its own pool — 30/minute authenticated, 10 anonymous — and it
    answers 403 when spent. That is an environment limit, not a defect, so it
    skips. A 401 is a real failure and propagates: it means the token stopped
    reaching the endpoint, which is exactly what these tests exist to catch.
    """
    try:
        return search_code(owner, repo, query)
    except Exception as exc:
        message = str(exc)
        if "403" in message or "rate limit" in message.lower():
            pytest.skip(f"search quota spent: {message[:120]}")
        raise


def test_tools_authenticate_with_the_token():
    """The tools must actually send GITHUB_TOKEN, not merely have it in .env.

    Checked at two levels, neither relying on quota arithmetic:
      1. _headers() attaches a Bearer credential at all;
      2. GitHub accepts it — an authenticated request is granted the 5000/hour
         pool, where an anonymous one is capped at 60.

    The end-to-end proof lives in the search tests: /search/code answers 401
    without a token, so any passing search is proof the token made it out.
    """
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("no GITHUB_TOKEN in the environment; nothing to verify")

    step("checking _headers() carries an Authorization header")
    headers = _headers()
    show("header names", sorted(headers))
    assert "Authorization" in headers, "GITHUB_TOKEN is set but never attached"
    assert headers["Authorization"].startswith("Bearer "), "expected a Bearer credential"

    step("checking GitHub grants the authenticated quota for those headers")
    quota = _probe_quota()
    show("quota", quota)
    assert quota["status"] != 401, "GitHub rejected the token outright (401)"
    assert quota["limit"] > 60, (
        f"GitHub granted a {quota['limit']}/hour limit — that is the anonymous "
        "pool, so the token was sent but rejected (expired? wrong scope?)"
    )


@pytest.fixture(scope="module")
def tiny_tree() -> list[str]:
    log.info("[fixture] get_repo_tree(%s, %s) — one API call, reused", TINY_OWNER, TINY_REPO)
    tree = get_repo_tree(TINY_OWNER, TINY_REPO)
    show("tiny_tree", tree)
    return tree


@pytest.fixture(scope="module")
def nested_tree() -> list[str]:
    log.info("[fixture] get_repo_tree(%s, %s) — one API call, reused", NESTED_OWNER, NESTED_REPO)
    tree = get_repo_tree(NESTED_OWNER, NESTED_REPO)
    show("nested_tree", tree)
    return tree


# --- get_repo_tree ---------------------------------------------------------


def test_tree_returns_plain_strings(tiny_tree):
    step("checking the return type is a non-empty list of str")
    assert isinstance(tiny_tree, list)
    assert tiny_tree, "a real repo should never yield an empty tree"
    assert all(isinstance(path, str) for path in tiny_tree)
    step(f"checking the whole tree equals [{TINY_FILE!r}]")
    assert tiny_tree == [TINY_FILE]


def test_tree_is_recursive_and_repo_relative(nested_tree):
    """Paths must be usable verbatim as get_file_contents() arguments."""
    step("looking for a root-level file: pyproject.toml")
    assert "pyproject.toml" in nested_tree
    step(f"looking for a deep file, proving recursive=1: {NESTED_FILE}")
    assert NESTED_FILE in nested_tree, "recursive=1 should reach nested files"
    step("checking no path is absolute or './'-prefixed")
    show("absolute-looking paths", [p for p in nested_tree if p.startswith(("/", "./"))])
    assert not any(path.startswith("/") for path in nested_tree)
    assert not any(path.startswith("./") for path in nested_tree)


def test_tree_has_no_duplicate_paths(nested_tree):
    """Duplicates would mean an explorer sub-agent reads the same file twice."""
    step(f"counting duplicates across {len(nested_tree)} entries")
    duplicates = {p for p in nested_tree if nested_tree.count(p) > 1}
    show("duplicates", sorted(duplicates))
    assert not duplicates, f"repeated paths in tree: {sorted(duplicates)}"


def test_tree_contains_files_only(nested_tree):
    """Every entry must be readable as a file, so directories are filtered out.

    GitHub's tree API returns type=tree (directories) and type=commit
    (submodules) alongside blobs; get_repo_tree keeps only blobs. Without this
    the agent gets paths that look readable and are not, and Phase 6's
    link-checker would accept a citation of "src/" as valid.
    """
    step("asserting known directories are absent from the tree")
    assert "src" not in nested_tree, "'src' is a directory and should be filtered"
    assert "src/flask" not in nested_tree, "'src/flask' is a directory too"
    assert "docs" not in nested_tree

    step("checking files inside those directories survived the filter")
    assert NESTED_FILE in nested_tree

    step("checking the directory names are still derivable from the paths")
    top_level = sorted({p.split("/")[0] for p in nested_tree if "/" in p})
    show("directories derived from file paths", top_level)
    assert "src" in top_level, "callers must still be able to recover directories"


def test_tree_accepts_a_branch_ref():
    step(f"calling get_repo_tree(ref='master') on {TINY_OWNER}/{TINY_REPO}")
    tree = get_repo_tree(TINY_OWNER, TINY_REPO, ref="master")
    show("tree at ref=master", tree)
    assert tree == [TINY_FILE]


def test_tree_accepts_a_commit_sha():
    """A pinned ref makes an explorer's findings reproducible."""
    step(f"calling get_repo_tree(ref={TINY_COMMIT[:12]}…)")
    tree = get_repo_tree(TINY_OWNER, TINY_REPO, ref=TINY_COMMIT)
    show("tree at pinned commit", tree)
    assert tree == [TINY_FILE]


def test_tree_raises_on_unknown_repo():
    step("requesting a repo that does not exist; expecting a 404 in the message")
    with pytest.raises(GitHubError, match="404") as caught:
        get_repo_tree(TINY_OWNER, "this-repo-does-not-exist-9c1f2b")
    show("raised", str(caught.value))


def test_tree_raises_on_unknown_ref():
    step("requesting a branch that does not exist; expecting a 404")
    with pytest.raises(GitHubError, match="404") as caught:
        get_repo_tree(TINY_OWNER, TINY_REPO, ref="no-such-branch-9c1f2b")
    show("raised", str(caught.value))


@pytest.mark.slow
def test_tree_raises_rather_than_truncating():
    """A partial tree must announce itself instead of looking complete.

    GitHub truncates recursive trees at ~100k entries / 7MB and sets
    truncated=true. get_repo_tree raises on that flag: the honest failure, and
    cheaper than the per-directory walking a full answer would need. Anything
    downstream — especially Phase 6's link-checker, which calls a cited path
    fake when it is merely missing from a partial tree — depends on the tree
    being complete or absent, never quietly short.

    Downloads a multi-megabyte response; excluded from the default run via the
    `slow` marker.
    """
    step("calling get_repo_tree on torvalds/linux, which GitHub cannot return whole")
    with pytest.raises(GitHubError, match="truncated") as caught:
        get_repo_tree("torvalds", "linux")
    show("raised", str(caught.value))


# --- get_repo_scopes -------------------------------------------------------
#
# Phase 4b's input. The orchestrator has no way to read a repository, so this is
# how it learns there is a `src/` worth one explorer and a `docs/` worth none.
# Every property below is deterministic, which is the point: the fan-out is only
# as sensible as the scope list it is handed, and that list should never be a
# judgement call.


@pytest.fixture(scope="module")
def nested_scopes() -> list[dict]:
    log.info("[fixture] get_repo_scopes(%s, %s) — one API call, reused", NESTED_OWNER, NESTED_REPO)
    scopes = get_repo_scopes(NESTED_OWNER, NESTED_REPO)
    show("nested_scopes", scopes)
    return scopes


def test_scopes_have_the_shape_the_prompt_promises(nested_scopes):
    step("checking every entry is {'scope': str, 'files': int}")
    assert nested_scopes
    for entry in nested_scopes:
        assert set(entry) == {"scope", "files"}
        assert isinstance(entry["scope"], str)
        assert isinstance(entry["files"], int)
        assert entry["files"] > 0


def test_scopes_are_top_level_only(nested_scopes):
    """A scope containing a slash would be a subdirectory, not a fan-out unit."""
    step("checking no scope name contains a path separator")
    show("scope names", [e["scope"] for e in nested_scopes])
    assert not any("/" in e["scope"] for e in nested_scopes)


def test_scopes_account_for_every_file_in_the_tree(nested_tree, nested_scopes):
    """The invariant that makes the fan-out safe: no file belongs to no explorer.

    Compared against the tree fixture rather than a hardcoded number, so the test
    keeps working as flask changes. If these ever disagree, some file is in a scope
    nobody was told to explore — and it would go unread with no error anywhere.
    """
    counted = sum(e["files"] for e in nested_scopes)
    step(f"checking scope counts sum to the tree size: {counted} vs {len(nested_tree)}")
    assert counted == len(nested_tree)


def test_root_files_get_their_own_scope(nested_tree, nested_scopes):
    """flask has a root-level pyproject.toml, so "." must be present and correct."""
    step('checking root-level files are grouped under "."')
    expected = sum(1 for path in nested_tree if "/" not in path)
    by_name = {e["scope"]: e["files"] for e in nested_scopes}
    show("root-level files", [p for p in nested_tree if "/" not in p])
    assert by_name["."] == expected


def test_scopes_are_ordered_largest_first(nested_scopes):
    """Ordering is load-bearing: the orchestrator reads the top of the list first."""
    step("checking the sort is (-files, name)")
    keys = [(-e["files"], e["scope"]) for e in nested_scopes]
    assert keys == sorted(keys)


def test_scopes_cost_one_request(nested_scopes):
    """A tool the orchestrator calls on every run should not walk the repository.

    Asserted through the quota probe the module already uses, because the cheap
    mistake here is a per-directory implementation that works fine on flask and
    quietly costs hundreds of requests on something larger.
    """
    before = _probe_quota()["remaining"]
    get_repo_scopes(TINY_OWNER, TINY_REPO)
    after = _probe_quota()["remaining"]
    # Two probe calls bracket one scopes call; the probes cost 1 each.
    step(f"quota went {before} → {after} across one get_repo_scopes call plus probes")
    assert before - after <= 3


# --- get_file_contents ----------------------------------------------------


def test_file_contents_are_decoded_text():
    step(f"reading {TINY_OWNER}/{TINY_REPO}:{TINY_FILE}")
    contents = get_file_contents(TINY_OWNER, TINY_REPO, TINY_FILE)
    show("contents", contents)
    assert isinstance(contents, str)
    step("checking base64 was decoded, not passed through")
    assert contents == TINY_CONTENTS, "base64 must be decoded, not passed through"


def test_file_contents_from_a_nested_path():
    step(f"reading {NESTED_OWNER}/{NESTED_REPO}:{NESTED_FILE}")
    contents = get_file_contents(NESTED_OWNER, NESTED_REPO, NESTED_FILE)
    show("contents", contents)
    assert "flask" in contents.lower()
    step("checking newlines survived decoding")
    assert "\n" in contents, "line structure should survive decoding"


def test_every_tree_path_is_a_valid_argument(tiny_tree):
    """The contract between the two tools: tree output feeds file input."""
    step(f"feeding all {len(tiny_tree)} tree paths back into get_file_contents")
    for path in tiny_tree:
        contents = get_file_contents(TINY_OWNER, TINY_REPO, path)
        log.info("  ← %s → %s chars", path, len(contents))
        assert contents


def test_file_contents_raises_on_missing_file():
    step("reading a path that does not exist; expecting a 404")
    with pytest.raises(GitHubError, match="404") as caught:
        get_file_contents(TINY_OWNER, TINY_REPO, "does/not/exist-9c1f2b.md")
    show("raised", str(caught.value))


def test_file_contents_rejects_binary_files():
    """The agent hits this on any repo with images, so the error must be legible.

    Binary blobs decode to bytes that are not UTF-8. Refusing them is the safe
    failure; the error must also name the path, since the message is what the
    agent reads before deciding which file to skip.
    """
    step(f"reading a PNG: {NESTED_BINARY}")
    with pytest.raises(ValueError, match=NESTED_BINARY) as caught:
        get_file_contents(NESTED_OWNER, NESTED_REPO, NESTED_BINARY)
    show("raised", f"{type(caught.value).__name__}: {caught.value}")
    step("checking the message says why, not just that decoding failed")
    assert "binary" in str(caught.value).lower()


def test_file_contents_rejects_a_directory_path():
    """A directory is the likeliest wrong argument, so say so plainly.

    GitHub answers with a JSON array instead of an object. Indexing ['content']
    on that raised a TypeError naming neither the path nor the mistake; the tool
    now detects the array and explains it.
    """
    step("reading a directory path ('src') as if it were a file")
    with pytest.raises(ValueError, match="directory") as caught:
        get_file_contents(NESTED_OWNER, NESTED_REPO, "src")
    show("raised", f"{type(caught.value).__name__}: {caught.value}")
    step("checking the message names the offending path")
    assert "src" in str(caught.value)


def test_file_contents_rejects_a_file_over_the_inline_limit():
    """A 3MB file must not come back as an empty string.

    Past 1MB GitHub stops inlining: it answers `encoding: "none"` with
    `content: ""`, which decodes to "" and reads as a legitimately empty file —
    the worst kind of wrong answer, since nothing raises and the agent would
    conclude a real file is blank. Refusing is also the right call on its own:
    a file this size is a lockfile or a generated bundle, not something to pull
    into a context window.

    If this ever fails with DID NOT RAISE, checker.ts shrank below 1MB; point
    LARGE_FILE at another big file.
    """
    step(f"reading {LARGE_OWNER}/{LARGE_REPO}:{LARGE_FILE} — roughly 3MB")
    with pytest.raises(ValueError, match="inline limit") as caught:
        get_file_contents(LARGE_OWNER, LARGE_REPO, LARGE_FILE)
    show("raised", f"{type(caught.value).__name__}: {caught.value}")
    step("checking the message names the path and reports the size")
    assert LARGE_FILE in str(caught.value)
    assert "MB" in str(caught.value)


# --- search_code ----------------------------------------------------------


def test_search_returns_dicts_with_paths():
    step(f"searching {NESTED_OWNER}/{NESTED_REPO} for 'Blueprint'")
    results = search_or_skip(NESTED_OWNER, NESTED_REPO, "Blueprint")
    show("result paths", [item["path"] for item in results])
    assert isinstance(results, list)
    assert results, "'Blueprint' certainly appears in flask"
    step("checking every result is a dict carrying a 'path'")
    assert all(isinstance(item, dict) for item in results)
    assert all("path" in item for item in results)


def test_search_stays_inside_the_requested_repo():
    """The repo: qualifier must actually scope results, or an explorer will
    happily cite files from someone else's project."""
    step("searching, then collecting the distinct repos in the results")
    results = search_or_skip(NESTED_OWNER, NESTED_REPO, "Blueprint")
    repos = {item["repository"]["full_name"] for item in results}
    show("repos represented", sorted(repos))
    assert repos == {f"{NESTED_OWNER}/{NESTED_REPO}"}


def test_search_with_no_matches_returns_empty_list():
    """No matches is an ordinary outcome, not an error."""
    step("searching for a string that cannot occur")
    results = search_or_skip(NESTED_OWNER, NESTED_REPO, "zzqqxx9c1f2bnotpresent")
    show("results", results)
    assert results == []


def test_search_query_is_url_escaped():
    """Queries are percent-encoded before reaching the URL.

    Anything needing encoding — a space, '+', '&', '#' — would otherwise
    truncate the query or change its meaning. Two probes: a multi-word query
    that must still match, and a '&' that must not silently end the parameter
    and hand back unscoped results.
    """
    step("searching for 'def create_app' — a space that must survive encoding")
    results = search_or_skip(NESTED_OWNER, NESTED_REPO, "def create_app")
    show("result paths", [item["path"] for item in results])
    assert isinstance(results, list)
    assert results, "'def create_app' certainly appears in flask's examples"

    step("searching for a term containing '&', which must not cut the query short")
    ampersand = search_or_skip(NESTED_OWNER, NESTED_REPO, "zzqq&9c1f2bnotpresent")
    show("results for the '&' query", ampersand)
    assert ampersand == [], (
        "a raw '&' would end the q parameter early and search for nothing at "
        "all, which GitHub answers with unscoped or unrelated results"
    )
