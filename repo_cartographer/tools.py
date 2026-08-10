"""Repo Cartographer's GitHub layer: size a repo up, list its files, read one, search them.

The whole of the system's access to a repository, and deliberately free of any
LLM or agent code — four ordinary functions over the GitHub REST API, testable
on their own. Every failure mode raises `GitHubError` (the API said no, or could
not be reached at all) or `ValueError` (the argument pointed somewhere
unreadable), so a caller can tell an API problem from a bad path.

The functions are not all handed to the same agent, and the line between them is
deliberate: `get_repo_scopes` reports a repository's *shape* — how many top-level
areas and how big each is — while the other three read what it *says*. Only the
first goes to the orchestrator, which is what lets it divide the work across
explorers without being able to do the work itself. See `agent.py`.
"""

import base64
import os
import time
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

# The inline 1MB ceiling on the contents API. Past it GitHub returns metadata
# with an empty content field rather than an error.
_MAX_INLINE_BYTES = 1_000_000

# How long to wait on one attempt, how many attempts, and how long between them.
# Three attempts with a growing pause covers the failure this exists for — a
# connection dropped or a response that never arrives — without turning a real
# outage into a two-minute hang.
_TIMEOUT_SECONDS = 30
_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2


class GitHubError(Exception):
    """GitHub answered, but not with what was asked for — or did not answer.

    One type for every "the API said no" case — a rejected request, a tree it
    would not return whole, a connection that never completed. Callers that need
    to tell an API problem from a bad argument can catch this instead of a bare
    Exception; bad arguments raise ValueError.
    """


# Every call authenticates with GITHUB_TOKEN. Two reasons: the code-search
# endpoint rejects unauthenticated requests outright with a 401, and the
# anonymous budget is 60 requests/hour against 5000 with a token — an agent
# reading whole file trees exhausts 60 almost immediately.

def _headers() -> dict[str, str]:
    """Build request headers, including auth when GITHUB_TOKEN is available."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str) -> requests.Response:
    """GET a GitHub URL, retrying transport failures and only those.

    Added at Phase 5, because the eval set immediately found what single runs had
    hidden: four of six repositories failed a sweep on `ReadTimeout` and
    `RemoteDisconnected` — the connection dropping, not GitHub answering. That is
    the value of a fixed set run end to end, and it is worth naming as the first
    thing it caught.

    Two decisions here, and both are about which failures are *information*:

    **Only transport errors are retried, never an HTTP answer.** A 404 is not
    going to become a 200, and a 403 from the search quota gets worse if you ask
    again. `requests` raises for the first kind and returns a response for the
    second, so the distinction is the `except` clause and nothing more.

    **A retry that runs out raises `GitHubError`,** which matters more than it
    looks. Every failure this module already knows about — a rejected request, a
    directory passed as a file — surfaces as `GitHubError` or `ValueError`, and
    those reach the model as a tool error it can read and route around; the
    prompts tell it exactly that ("a failed tool call is information"). A raw
    `requests.ReadTimeout` did not: it escaped the agent loop and ended the run.
    A dropped connection is now the same kind of event as a 404 — a fact about
    one call, not the end of the job.
    """
    last: requests.RequestException | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return requests.get(url, headers=_headers(), timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last = exc
            if attempt + 1 < _ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    raise GitHubError(
        f"Could not reach the GitHub API after {_ATTEMPTS} attempts: {last}. "
        "This is a network problem rather than a bad request — the same call may "
        "well work later, but do not repeat it immediately."
    ) from last


def get_repo_tree(owner: str, repo: str, ref: str = "HEAD") -> list[str]:
    """
    Get the file paths in a GitHub repository at a specific reference (branch, tag, or commit).

    Only files are returned. GitHub's tree API also lists directories
    (type=tree) and submodules (type=commit), and neither can be read with
    get_file_contents — passing one through would hand the caller a path that
    looks readable and is not. Callers needing directory names can derive them
    from the paths (e.g. {p.split("/")[0] for p in tree if "/" in p}).

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        ref (str): The reference to get the tree from (default is "HEAD").

    Returns:
        list[str]: Repo-relative paths of every file in the tree.

    Raises:
        GitHubError: if GitHub rejects the request, cannot be reached, or
            truncated the tree.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
    response = _get(url)

    if response.status_code != 200:
        raise GitHubError(f"Failed to fetch repository tree: {response.status_code} - {response.text}")

    tree_data = response.json()

    # GitHub caps a recursive tree at ~100k entries / 7MB and flags the cut with
    # truncated=true. Returning the partial list would give the caller a tree
    # that looks complete, so fail loudly rather than silently under-report.
    if tree_data.get('truncated'):
        raise GitHubError(
            f"GitHub truncated the tree for {owner}/{repo}@{ref} — "
            f"{len(tree_data.get('tree', []))} entries returned, more exist. "
            "Reading the whole tree would need per-directory walking, which "
            "this tool does not do; explore a subdirectory instead."
        )

    return [item['path'] for item in tree_data.get('tree', []) if item.get('type') == 'blob']

def get_repo_scopes(owner: str, repo: str, ref: str = "HEAD") -> list[dict]:
    """
    List a repository's top-level directories with a file count for each, largest first.

    Use this to decide how to divide up a repository before exploring it. It
    reports the *shape* of the repo — how many top-level areas there are and how
    much is in each — and deliberately reveals nothing about what any file
    contains. Files sitting at the repository root are grouped under the scope
    ".".

    Costs a single GitHub request no matter how large the repository is.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        ref (str): The reference to inspect (default is "HEAD").

    Returns:
        list[dict]: One entry per top-level scope, each `{"scope": str, "files": int}`,
            sorted by file count descending then by name.

    Raises:
        GitHubError: if GitHub rejects the request, cannot be reached, or
            truncated the tree.
    """
    # Built on get_repo_tree rather than a second endpoint so both see exactly the
    # same repository: one source of truth for which paths exist, and the
    # truncation guard is inherited rather than reimplemented.
    counts: dict[str, int] = {}
    for path in get_repo_tree(owner, repo, ref):
        scope = path.split("/")[0] if "/" in path else "."
        counts[scope] = counts.get(scope, 0) + 1

    return [
        {"scope": scope, "files": files}
        for scope, files in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def get_file_contents(owner: str, repo: str, path: str) -> str:
    """
    Get the contents of a file in a GitHub repository.
    
    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        path (str): The path to the file in the repository.
        
    Returns:
        str: The contents of the file as a string.

    Raises:
        GitHubError: if GitHub rejects the request (a missing path answers 404)
            or cannot be reached.
        ValueError: if the path is not a readable UTF-8 text file — a directory,
            a symlink or submodule, a binary blob, or larger than 1MB.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    response = _get(url)

    if response.status_code != 200:
        raise GitHubError(f"Failed to fetch file contents: {response.status_code} - {response.text}")

    file_data = response.json()

    # A directory answers with a JSON array of its entries instead of a file
    # object. It is the likeliest wrong argument, so name the mistake rather
    # than letting the subscript below raise an anonymous TypeError.
    if isinstance(file_data, list):
        # Name one child path: an agent that took this wrong turn needs a right
        # one, and the response already contains the directory's entries.
        example = next(
            (entry["path"] for entry in file_data if isinstance(entry, dict) and "path" in entry),
            None,
        )
        hint = f"Pass a path inside it, such as '{example}'." if example else "It is empty."
        # ValueError, not the TypeError ruff suggests: `path` is a perfectly
        # good str, it just points somewhere unreadable.
        raise ValueError(  # noqa: TRY004
            f"'{path}' is a directory in {owner}/{repo}, not a file. {hint}"
        )

    # A symlink or submodule is a "file" only in the loosest sense: GitHub sends
    # a target or a git URL where the content would be, and no content at all.
    if file_data.get('type') != 'file':
        raise ValueError(
            f"'{path}' in {owner}/{repo} is a {file_data.get('type')}, not a "
            "regular file, so it has no contents to read."
        )

    # Past 1MB GitHub stops inlining and answers encoding="none" with an empty
    # content field — decoding that yields "" and reads as an empty file. Refuse
    # instead: a file this size is a lockfile, a generated bundle or a dataset,
    # which is exactly what an agent should skip rather than pull into context.
    if file_data.get('encoding') != 'base64':
        size = file_data.get('size', 0)
        raise ValueError(
            f"'{path}' in {owner}/{repo} is {size / 1_000_000:.1f} MB, over "
            f"GitHub's {_MAX_INLINE_BYTES // 1_000_000} MB inline limit, so the "
            f"API returned no content (encoding={file_data.get('encoding')!r}). "
            "Skip it, or fetch it directly from "
            f"{file_data.get('download_url')}"
        )

    try:
        return base64.b64decode(file_data['content']).decode('utf-8')
    except UnicodeDecodeError as exc:
        # Binary blobs decode to bytes that are not UTF-8. Raising is right,
        # but the bare codec error names no path, so an agent reading a tree
        # cannot tell which file to skip.
        raise ValueError(
            f"'{path}' in {owner}/{repo} is not UTF-8 text — likely a binary "
            f"file such as an image or archive ({exc})"
        ) from exc

def search_code(owner: str, repo: str, query: str) -> list[dict]:
    """
    Search for code in a GitHub repository.
    
    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        query (str): The search query.
        
    Returns:
        list[dict]: A list of search results, each represented as a dictionary.
            No matches is an empty list, not an error.

    Raises:
        GitHubError: if GitHub rejects the request or cannot be reached. The
            search endpoint has its own quota — 30 requests/minute
            authenticated — and answers 403 when it is spent.
    """
    # The query is percent-encoded: it travels inside a URL, so a raw space,
    # '&', '#' or '+' would truncate it or change its meaning. GitHub decodes
    # the parameter before parsing it, so qualifiers a caller passes in
    # (e.g. "language:python") still work encoded.
    url = f"https://api.github.com/search/code?q={quote(query, safe='')}+repo:{owner}/{repo}"
    response = _get(url)

    if response.status_code != 200:
        raise GitHubError(f"Failed to search code: {response.status_code} - {response.text}")
    
    search_results = response.json()
    return search_results.get('items', [])
