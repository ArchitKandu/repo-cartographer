"""Phase 6: the `link-checker` sub-agent, which contains no model.

The other two delegates are language models with narrowed tools. This one is a
`CompiledStateGraph` holding a single node that runs ordinary Python — and it
sits in the same `task` menu, is briefed the same way, and reports back the same
way. That is the phase's whole argument: **a sub-agent is a unit of delegated
work, not a smaller model call.** Nothing about the orchestrator's side of the
handoff changes when the thing on the other end stops thinking.

It is easy to skip this step, because a plain function does not feel like a real
sub-agent. It is the step that proves the abstraction is about delegation rather
than about inference — and it is also the only defence in this system that cannot
be talked out of its job. Three prompts *ask* for verified citations; Phase 4
removed the doc-writer's ability to invent one. Neither stops the doc-writer
faithfully repeating an explorer's mistake. This does, because "that path is not
in the tree" is arithmetic.

## What it is briefed with, and what it does

    task("owner=psf repo=requests guide=/guide.md", "link-checker")

One node, four steps, no branching: parse the brief, read the guide out of the
shared workspace, check every path it cites against the repository's real tree
(`citations.py`), return the verdict as its final message. The orchestrator
cannot tell from the outside that no model was involved, which is exactly the
property worth demonstrating.

## Where the decisions are, and are not

Everything that judges anything lives in `citations.py`, which imports no agent
code and can be tested with a list of strings. This module is plumbing: brief
parsing, a backend read, and the LangGraph wrapper. The split is deliberate — the
interesting part of a non-model sub-agent should be testable without building a
graph at all.

## Parsing a brief written by a model

The brief is free text an orchestrator composed, so it is read leniently and the
leniency is reported rather than hidden:

- **owner/repo is required.** Without it there is no tree to check against, so
  the node returns an error verdict naming exactly what the brief should have
  said. A wrong repository would be worse than no check — every path would come
  back "not found" and the guide would be condemned wholesale.
- **The guide path falls back to `/guide.md`**, the location the prompts agree
  on, *and the verdict says it did so*. Failing the check because a model forgot
  to restate a path both sides already know would be pedantry; doing it silently
  would be the "succeeds somewhere useless" failure this codebase keeps running
  into. So it does neither.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from repo_cartographer.citations import CitationReport, check_citations

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langgraph.graph.state import CompiledStateGraph

# Where the prompts agree the finished guide is written. Used as the fallback
# when a brief names no path — see the module docstring on why that fallback
# announces itself.
DEFAULT_GUIDE_PATH = "/guide.md"

# `owner=psf repo=requests` first, because it is unambiguous and the
# orchestrator prompt asks for exactly this form.
_KEYED_OWNER = re.compile(r"owner\s*[=:]\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)
_KEYED_REPO = re.compile(r"repo(?:sitory)?\s*[=:]\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)

# ...and `psf/requests` as written in prose, as the fallback. Not preceded by a
# slash or a word character, so `/notes/src.md` and `src/requests` cannot be read
# as a repository slug.
_SLUG = re.compile(r"(?<![\w/])([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)(?![\w/])")

# A workspace path: absolute, and markdown-ish text around it. Restricted to
# `.md` because every path a brief legitimately names here is a notes or guide
# file, and a looser pattern would happily pick up a repository path instead.
_GUIDE_PATH = re.compile(r"(/[\w./-]*\.md)")

# Second parts of a slug that are certainly not repository names. `requests.get`
# is not a slug at all, but `psf/requests.md` would parse as one, and a brief
# that mentions a notes file before the repository would otherwise check the
# wrong thing entirely.
_NOT_A_REPO_NAME = re.compile(r"\.(md|py|js|ts|json|toml|txt|ya?ml)$", re.IGNORECASE)


class LinkCheckerState(MessagesState):
    """`MessagesState` unchanged, named for what the graph is.

    `CompiledSubAgent` requires only that the returned state carries `messages`;
    the parent reads the last non-empty `AIMessage` as the sub-agent's report.
    Nothing else needs to cross, so nothing else is declared — a wider schema
    would be state this node could accidentally write back into the parent.
    """


def _trim(name: str) -> str:
    """Strip the sentence punctuation a prose brief leaves on a name.

    "check psf/requests." yields a repository called `requests.`, which GitHub
    answers 404 for — and a 404 here condemns every path in a perfectly good
    guide. Dots and dashes are legal *inside* a repository name, so only the
    ends are trimmed.
    """
    return name.strip(".-")


def parse_brief(brief: str) -> tuple[str | None, str | None, str, bool]:
    """Pull owner, repo and guide path out of a brief a model wrote.

    Returns `(owner, repo, guide_path, path_was_defaulted)`. `owner` and `repo`
    are `None` together when the brief named no repository, which is the one
    unrecoverable case.
    """
    owner_match = _KEYED_OWNER.search(brief)
    repo_match = _KEYED_REPO.search(brief)
    owner = _trim(owner_match.group(1)) if owner_match else None
    repo = _trim(repo_match.group(1)) if repo_match else None

    if not (owner and repo):
        for candidate_owner, candidate_repo in _SLUG.findall(brief):
            trimmed = _trim(candidate_repo)
            if trimmed and not _NOT_A_REPO_NAME.search(trimmed):
                owner, repo = _trim(candidate_owner), trimmed
                break

    path_match = _GUIDE_PATH.search(brief)
    guide_path = path_match.group(1) if path_match else DEFAULT_GUIDE_PATH

    if not (owner and repo):
        return None, None, guide_path, path_match is None
    return owner, repo, guide_path, path_match is None


_NO_REPOSITORY = (
    "CITATION CHECK — could not run: the brief named no repository, so there is "
    "nothing to check the guide's paths against. Re-send it as "
    "'owner=<owner> repo=<repo> guide=<workspace path>', for example "
    "'owner=psf repo=requests guide=/guide.md'."
)


def build_link_checker(backend: BackendProtocol) -> CompiledStateGraph:
    """Compile the link-checker against the workspace the other agents share.

    The backend is closed over rather than pulled from graph state, for the same
    reason `agent.py` passes one instance everywhere: the guide this reads is the
    file the doc-writer wrote, and two backend instances would be two different
    workspaces with no error to say so.
    """

    def check(state: LinkCheckerState) -> dict[str, Any]:
        messages = state.get("messages") or []
        brief = messages[-1].text if messages else ""

        owner, repo, guide_path, defaulted = parse_brief(brief)
        if not (owner and repo):
            return {"messages": [AIMessage(content=_NO_REPOSITORY)]}

        result = backend.read(guide_path)
        if result.error or not result.file_data:
            # The doc-writer reported success and the file is not there, or is
            # somewhere else. Reported as an error rather than as a clean pass —
            # a check that finds nothing to check must never read as "nothing is
            # wrong", which is the whole shape of failure in a delegated system.
            report = CitationReport(
                owner,
                repo,
                (),
                (),
                0,
                error=(
                    f"no guide to read at {guide_path} in the workspace "
                    f"({result.error or 'the file was empty'}). The doc-writer "
                    "either wrote it elsewhere or did not write it at all."
                ),
            )
            return {"messages": [AIMessage(content=report.summary())]}

        report = check_citations(owner, repo, result.file_data["content"])
        note = (
            f"\n\n(The brief named no guide path, so {DEFAULT_GUIDE_PATH} was "
            "checked — the location the prompts agree on.)"
            if defaulted
            else ""
        )
        return {"messages": [AIMessage(content=report.summary() + note)]}

    graph = StateGraph(LinkCheckerState)
    graph.add_node("check", check)
    graph.add_edge(START, "check")
    graph.add_edge("check", END)
    return graph.compile(name="link-checker")


LINK_CHECKER_DESCRIPTION = (
    "Verifies that every file path a finished guide cites really exists in the "
    "repository. This delegate is NOT a language model — it is a plain function "
    "over the real file tree, so its verdict is a fact rather than an opinion, "
    "and it costs no model request. Brief it with the owner, the repo, and the "
    "workspace path of the guide, like "
    "'owner=psf repo=requests guide=/guide.md'."
)
