"""Phase 8: the one thing this system can do that cannot be undone.

Every capability up to here reads. `tools.py` is four GET requests;
`citations.py` compares strings; the workspace is a scratch directory nobody
else can see. The worst outcome of a bad run has been a wrong sentence in a
guide, and the fix for a wrong sentence is to run it again.

This module breaks that. `open_pull_request` writes a branch, a file and a draft
pull request into someone else's repository. It is visible to that project's
maintainers the moment it lands, it sends notifications, and closing it does not
unsend them. There is no version of "run it again" that helps.

So it is the phase's real subject, and the tool itself is almost beside the
point: *how do you stop an agent doing something irreversible?* The answer here
is two independent guards, and they stop different things.

## Guard 1 — `interrupt_on`, which stops the agent

Wired in `agent.py` as `interrupt_on={"open_pull_request": True}`. The graph
pauses **before** the tool runs, hands the pending call back to whoever invoked
it, and waits. Nothing resumes without an explicit decision.

That is a strong guarantee and it has a shape worth being precise about: it
protects against the *agent* acting unilaterally. It assumes a human is there to
answer. An unattended run does not get approval — it gets a paused graph, which
is the correct outcome and also not a running feature.

## Guard 2 — `ALLOW_PULL_REQUESTS`, which stops the deployment

An approval gate that has never been exercised is a gate nobody has tested, and
testing this one means running it. The obvious way to test it is to approve the
call and see what happens — which, without a second guard, means opening a real
draft pull request on a real repository to prove that a safety feature works.

So the tool refuses unless `ALLOW_PULL_REQUESTS=true` is set in the environment,
and it refuses by *returning* the refusal rather than raising, so the model reads
it as a tool result and can explain it. That makes the approve path fully
exercisable — the interrupt clears, the tool body runs, GitHub is never called —
and it survives the cases the interrupt does not: a script someone runs to see
what happens, a resume that auto-approves, a deployment nobody is watching.

Two guards, two different failure modes:

| | Stops | Fails open when |
|---|---|---|
| `interrupt_on` | the agent acting without a human | nobody is there to answer — it pauses |
| `ALLOW_PULL_REQUESTS` | the deployment acting at all | somebody deliberately sets it |

## Why the writes live here and not in `tools.py`

`tools.py` is four functions, all GET, and that is a property worth keeping
legible: *the module that talks to GitHub cannot change anything*. Adding a POST
to it would end that at the cost of one import saved. The write helpers are here,
next to the only thing that uses them.
"""

from __future__ import annotations

import base64
import os
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import requests

from repo_cartographer.tools import GitHubError, _get, _headers

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol

_API = "https://api.github.com"

# The branch a proposal is pushed to. Namespaced so it is obvious in a fork's
# branch list who created it, and fixed rather than random so a second run
# updates the same proposal instead of littering the repository with branches.
BRANCH = "repo-cartographer/onboarding-guide"

# What the pull request adds. A new file rather than an edit to an existing one:
# this system has never read the target's own README and has no business
# rewriting it.
GUIDE_FILENAME = "ONBOARDING.md"

# Matches the read side in tools.py, and for the same reason: a dropped
# connection is worth retrying, an answer from GitHub is not.
_ATTEMPTS = 3

ALLOW_ENV = "ALLOW_PULL_REQUESTS"

# The gated tool's name, in one place. `agent.py` keys `interrupt_on` off this
# and `tests/test_approval.py` checks the built tool answers to it — because the
# gate is a *string match* against the tool name, so renaming the function and
# forgetting the key would leave the capability wired in and completely ungated,
# with nothing anywhere raising.
PULL_REQUEST_TOOL = "open_pull_request"

_NOT_ALLOWED = (
    f"Refused: opening pull requests is switched off. This tool writes a branch, "
    f"a file and a draft pull request into a repository you may not own, and that "
    f"cannot be undone by running anything again. Nothing was sent to GitHub.\n\n"
    f"To enable it deliberately, set {ALLOW_ENV}=true in the environment and run "
    f"again — and point it at a repository you actually control. The human "
    f"approval you just gave covers this specific call; it does not switch the "
    f"capability on."
)


def _send(method: str, path: str, payload: dict[str, Any]) -> requests.Response:
    """POST or PUT to the GitHub API, with the same retry policy as the reads.

    `tools.py` explains the policy and the reason for it: transport failures are
    retried, HTTP answers never are. It matters more here than there. A retry
    that fires on a *response* would repeat a write, and "did that create one
    pull request or two" is exactly the question nobody wants after the fact.
    Only `requests.RequestException` is caught, and a request that was answered —
    422, 403, 404 — is returned to the caller untouched.
    """
    last = _ATTEMPTS - 1
    for attempt in range(_ATTEMPTS):
        try:
            return requests.request(
                method, f"{_API}{path}", headers=_headers(), json=payload, timeout=30
            )
        except requests.RequestException as exc:
            if attempt == last:
                raise GitHubError(
                    f"Could not reach the GitHub API to {method} {path}: {exc}. "
                    "Nothing is known about whether the write landed — check the "
                    "repository before retrying."
                ) from exc
            # A connection that never completed may still have been received, so
            # the message above deliberately does not promise nothing happened.
    raise AssertionError("unreachable")


def build_pull_request_tool(backend: BackendProtocol, guide_path: str):
    """Build `open_pull_request`, closed over the workspace the guide lives in.

    A closure for the same reason `build_link_checker` is one: the content this
    proposes comes from `/guide.md`, which is a file in the shared backend rather
    than something a model should retype into a tool argument. Passing the guide
    as a parameter would mean the model re-emitting a thousand tokens it already
    produced, and — worse — would let it send something *other* than the guide
    that was checked.
    """

    def open_pull_request(owner: str, repo: str, title: str) -> str:
        """
        Open a DRAFT pull request adding the onboarding guide to a GitHub repository.

        This is the only action available to you that changes anything outside this
        machine, and it cannot be undone: it creates a branch, commits a file, and
        opens a pull request visible to that repository's maintainers.

        Requires human approval. The run pauses before this executes and a person
        decides. Only call it when the user has explicitly asked for a pull request.

        The guide already written to the workspace is what gets proposed; you do
        not pass its content.

        Args:
            owner (str): The owner of the repository to open the pull request against.
            repo (str): The name of that repository.
            title (str): The pull request title. One line, describing what it adds.

        Returns:
            str: The URL of the draft pull request, or an explanation of why
                nothing was sent.
        """
        if os.environ.get(ALLOW_ENV, "").strip().lower() != "true":
            return _NOT_ALLOWED

        result = backend.read(guide_path)
        if result.error or not result.file_data:
            return (
                f"Refused: there is no guide at {guide_path} to propose "
                f"({result.error or 'the file was empty'}). Nothing was sent to "
                "GitHub. A pull request adding an empty file is worse than no "
                "pull request."
            )
        guide = result.file_data["content"]

        try:
            return _open(owner, repo, title, guide)
        except GitHubError as exc:
            return f"The pull request was not opened: {exc}"

    return open_pull_request


def _open(owner: str, repo: str, title: str, guide: str) -> str:
    """Branch, commit, draft PR. Four calls, in the only order that works."""
    repo_info = _get(f"{_API}/repos/{owner}/{repo}")
    if repo_info.status_code != HTTPStatus.OK:
        raise GitHubError(f"cannot read {owner}/{repo}: {repo_info.status_code} - {repo_info.text}")
    base = repo_info.json()["default_branch"]

    head = _get(f"{_API}/repos/{owner}/{repo}/git/ref/heads/{base}")
    if head.status_code != HTTPStatus.OK:
        raise GitHubError(f"cannot resolve {base}: {head.status_code} - {head.text}")
    base_sha = head.json()["object"]["sha"]

    # An existing branch answers 422, which is not a failure here: a second
    # proposal should update the one already open rather than refuse. Any other
    # non-2xx is real.
    created = _send(
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/heads/{BRANCH}", "sha": base_sha},
    )
    if created.status_code not in (HTTPStatus.CREATED, HTTPStatus.UNPROCESSABLE_ENTITY):
        raise GitHubError(f"cannot create {BRANCH}: {created.status_code} - {created.text}")

    # The contents API commits a file in one call, which is why it is used here
    # rather than the git data API's blob/tree/commit trio. `sha` is required when
    # replacing an existing file and forbidden when creating one, so the current
    # state of the branch decides.
    existing = _get(f"{_API}/repos/{owner}/{repo}/contents/{GUIDE_FILENAME}?ref={BRANCH}")
    payload: dict[str, Any] = {
        "message": f"docs: add {GUIDE_FILENAME}\n\nGenerated by Repo Cartographer.",
        "content": base64.b64encode(guide.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if existing.status_code == HTTPStatus.OK:
        payload["sha"] = existing.json()["sha"]

    written = _send("PUT", f"/repos/{owner}/{repo}/contents/{GUIDE_FILENAME}", payload)
    if written.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
        raise GitHubError(f"cannot commit {GUIDE_FILENAME}: {written.status_code} - {written.text}")

    opened = _send(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        {
            "title": title,
            "head": BRANCH,
            "base": base,
            # Draft, always, and not a parameter. A machine-written document about
            # someone else's codebase is a starting point for a person, and a
            # ready-for-review pull request asks reviewers for time on the
            # assumption that a human already spent some.
            "draft": True,
            "body": (
                "This adds an onboarding guide generated by "
                "[Repo Cartographer](https://github.com/ArchitKandu/repo-cartographer).\n\n"
                "It was written from files an automated explorer actually read, and "
                "every file path it cites was checked against this repository's real "
                "file tree before the pull request was opened. It is a draft, and it "
                "has not been reviewed by a person."
            ),
        },
    )
    if opened.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
        # A pull request from this branch is already open. Reporting that is more
        # useful than raising, and it keeps a second run idempotent.
        return (
            f"A pull request from `{BRANCH}` is already open on {owner}/{repo}; the "
            f"guide on that branch was updated instead. GitHub said: {opened.text[:200]}"
        )
    if opened.status_code != HTTPStatus.CREATED:
        raise GitHubError(f"cannot open the pull request: {opened.status_code} - {opened.text}")

    return f"Opened a draft pull request: {opened.json()['html_url']}"
