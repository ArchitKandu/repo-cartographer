"""Work the explorer no longer spends a model request on.

Three of the explorer's turns were never about reading code. It called
`get_repo_tree` to find out which files were in its scope, `read_file` to fetch
the skill for the ecosystem it had just recognised, and then began. On a free
tier counted in requests per minute — 15 of them, shared by every agent in the
run — three explorers doing that is six requests spent before a single line of
the repository has been read.

None of that work needs a model. Which files are in a scope is a filter over a
list. Which skill matches is `"pyproject.toml" in paths`. So both are done here,
in Python, before the explorer's first turn, and the answers are spliced into its
system prompt: it wakes up already holding its file list and already following
the right reading order.

This is Phase 6's argument reaching a third place. A delegate with no model in it
beat a delegate with one because checking a citation is arithmetic. Filtering a
list and matching a filename are arithmetic too, and the only reason they were
model decisions is that the model was already there.

## What it costs, and why the listing is capped

A tool result and a system prompt are not billed the same way. Both are re-sent
on every turn — a tool message stays in the thread — but a tool result over
`TOOL_RESULT_TOKEN_LIMIT` is offloaded to the workspace by
`FilesystemMiddleware` and replaced with a preview, while a system prompt is not
evictable by anything. So injecting a very large listing would trade a request
for tokens on every turn, and on a repository whose scope holds two thousand
paths that is a bad trade.

Hence `MAX_INJECTED_PATHS`. Above it, nothing is injected and the explorer calls
`get_repo_tree` exactly as it did before, paying one request for a listing that
can then be evicted. Below it — which is most scopes — the listing is small
enough that the injected copy costs about what the tool message it replaces
would have, and the request is saved outright. The cap is the honest version of
the optimisation: it declines to apply where it would stop being one.

The alternative, injecting a truncated listing, was rejected for the reason this
codebase keeps rediscovering: it succeeds somewhere useless. An explorer handed
the first 300 of 2000 paths has no way to know that the file it needs was in the
1700, and its notes would read exactly like a complete map of a small scope.

## Reading the brief

The parse below is deliberately forgiving, because a brief is written by a model
and the fallback is silent. Nothing here raises and nothing here is required: an
unparseable brief, a repository GitHub will not answer for, an ecosystem neither
skill matches — each simply injects less, and `EXPLORER_PROMPT` still describes
the tools for doing the work by hand. The optimisation is allowed to miss. It is
not allowed to break a run, and it is not allowed to make an explorer *think* it
has a file list it does not have.

`ORCHESTRATOR_PROMPT` asks for a machine-readable first line —
`owner=… repo=… scope=… notes=…` — which is the same shape the link-checker's
brief has used since Phase 6, so the format is a convention here rather than a
new demand.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

from repo_cartographer.link_checker import parse_brief as _repo_from_brief
from repo_cartographer.skills import match_skill, skill_body
from repo_cartographer.tools import GitHubError, get_repo_tree

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
        ResponseT,
    )
    from langchain_core.messages import AIMessage

# Above this many paths in a scope, the listing is not injected — see the module
# docstring. 300 typical repository paths is roughly 3-4k tokens, which is the
# same order as the `get_repo_tree` tool message it stands in for, so below the
# cap this is a request saved at no real token cost.
MAX_INJECTED_PATHS = 300

# How long a fetched tree may be reused. The point is consistency *within* a run:
# three explorers fanned out over one repository should all be reading the same
# list of files, which is the same argument `get_repo_scopes` makes for building
# on `get_repo_tree` rather than a second endpoint. The TTL is what stops that
# from becoming a stale tree served forever by a long-lived deployment, since
# `HEAD` is a moving reference.
_TREE_TTL_SECONDS = 900
_TREE_CACHE_MAX = 8

# `scope="src"`, `scope = .`, `scope=src/requests`. Quotes because a model
# writes these and reaches for them around a value like `.`; whitespace because
# it sometimes pads the `=`.
#
# Only `scope` is matched here. Owner and repo come from `link_checker.py`, which
# has been reading exactly this shape of brief since Phase 6 and knows two things
# this pattern would have had to learn again: that a prose brief leaves sentence
# punctuation on a name, and that `psf/requests` written in prose is a usable
# fallback while `/notes/src.md` is not. Two regexes for one convention is how
# the two sides of it drift apart.
_SCOPE = re.compile(r"""scope\s*[=:]\s*["'`]?([^\s"'`,]+)["'`]?""", re.IGNORECASE)

_TREE_CACHE: dict[tuple[str, str, str], tuple[float, list[str]]] = {}


class Brief(NamedTuple):
    """The machine-readable half of an explorer's brief."""

    owner: str
    repo: str
    scope: str

    @property
    def is_whole_repo(self) -> bool:
        """Whether this brief's scope is the repository rather than a directory.

        `.` is `get_repo_scopes`' name for the files at the root, and a small
        repository is dispatched as one explorer over everything —
        `ORCHESTRATOR_PROMPT` says to call that scope the whole repository. Both
        spellings arrive here, and both mean "do not filter by directory".
        """
        return self.scope in {"", ".", "/", "*", "all", "whole", "repo", "repository"}


def parse_brief(text: str) -> Brief | None:
    """Read the repository and scope out of a brief. None if either is missing.

    Scanned out of the whole brief rather than the first line alone: the line is
    what `ORCHESTRATOR_PROMPT` asks for, and being strict about it would mean the
    saving disappears the first time a model writes the fields in prose instead.

    None rather than a partial answer, and that is the important half. A brief
    with no scope could be read as the whole repository, and a wrong guess there
    hands an explorer a listing for ground another explorer is already covering;
    a brief with no repository could be guessed from nothing at all. Both would
    put a file list in front of the model with the authority of a fetched fact
    behind it. Declining costs one turn — the explorer fetches its own — which is
    exactly the turn this module was written to save, and losing it is a much
    smaller thing than misleading the agent that does the reading.
    """
    owner, repo, _guide, _defaulted = _repo_from_brief(text)
    if not (owner and repo):
        return None

    scope = _SCOPE.search(text)
    if not scope:
        return None
    return Brief(owner=owner, repo=repo, scope=scope.group(1).strip().strip("/"))


def cached_tree(owner: str, repo: str, ref: str = "HEAD") -> list[str] | None:
    """`get_repo_tree`, memoised for the length of a run. None if GitHub refused.

    Every explorer on a run asks for the same tree, so the cache turns three
    identical GitHub requests into one. `None` rather than a raise: a caller here
    is preparing a prompt, not answering the model, and a repository that cannot
    be reached should degrade to an explorer fetching its own listing — and
    seeing the real error — rather than to a failed run.
    """
    key = (owner.lower(), repo.lower(), ref)
    now = time.monotonic()

    if (hit := _TREE_CACHE.get(key)) and now - hit[0] < _TREE_TTL_SECONDS:
        return hit[1]

    try:
        tree = get_repo_tree(owner, repo, ref)
    except (GitHubError, ValueError):
        return None

    # Bounded rather than unbounded: this lives for the life of the process, and
    # a server mapping repositories all day should not accumulate every tree it
    # has ever seen. Oldest out first.
    if len(_TREE_CACHE) >= _TREE_CACHE_MAX:
        del _TREE_CACHE[next(iter(_TREE_CACHE))]
    _TREE_CACHE[key] = (now, tree)
    return tree


def scoped_paths(tree: list[str], brief: Brief) -> list[str]:
    """The paths in `tree` that belong to this brief's scope.

    The filter matches the one in `get_repo_scopes`: a path's scope is its first
    segment, and a file at the root belongs to `.`. Keeping the two definitions
    identical is what makes the file count the orchestrator saw and the listing
    the explorer gets two views of one fact.
    """
    if brief.is_whole_repo and brief.scope != ".":
        return list(tree)
    if brief.scope == ".":
        return [path for path in tree if "/" not in path]
    prefix = f"{brief.scope}/"
    return [path for path in tree if path.startswith(prefix)]


# The three sections this module can add to a prompt, kept as templates rather
# than built inline for a reason that is not tidiness: they are instructions, and
# `scripts/run_evals.py` hashes every instruction the agents work from so that a
# recorded score cannot outlive the prompt that produced it. Named constants are
# what let `briefing_sections()` hand the fingerprint their exact wording without
# a repository to fetch or a skill to match.
_LISTING_TEMPLATE = """

## The files in your scope

This is every file in {where}, fetched from GitHub before you were started. It
is complete for your scope and it is the same tree the orchestrator sized the
repository up from, so **you do not need to call `get_repo_tree`** — spend the
request on reading code instead. These are the only paths you may cite.

```
{listing}
```
"""

_SKILL_TEMPLATE = """

## Your skill for this repository: `{name}`

The file list above was matched against the skills library before you started,
and this one fits. It is reproduced in full below, so **do not `read_file` it** —
you already have it, and the copy in `/skills/` is the same text. It tells you
which files answer which questions in this ecosystem and in what order to open
them; where it is more specific than the method above, it wins.

---

{body}
"""

# A scope with no files is worth saying out loud. The alternative is an explorer
# that calls `get_repo_tree`, filters to nothing, and has to work out on its own
# whether it was misbriefed or the directory is empty — two turns to reach a fact
# already known here, and a standing invitation to go and describe something else.
_EMPTY_SCOPE_TEMPLATE = """

## The files in your scope

**Scope `{scope}` holds no files** in {owner}/{repo}. The tree was fetched before
you started and nothing in it begins with that path, so there is nothing to read.
Say so in your report and stop — do not go looking elsewhere in the repository
for something to describe.
"""


def briefing_sections() -> list[str]:
    """The wording of every section this module can inject, for fingerprinting.

    Templates rather than rendered sections, so the digest tracks the
    instructions and not the repository that happened to be mapped. See
    `prompt_fingerprint` in `scripts/run_evals.py` for why an instruction that
    changes without moving a digest is the one failure that instrument has.
    """
    return [_LISTING_TEMPLATE, _SKILL_TEMPLATE, _EMPTY_SCOPE_TEMPLATE]


def _listing_section(brief: Brief, paths: list[str]) -> str:
    """The scope's file list, as a prompt section — or nothing if it is too big."""
    if not paths or len(paths) > MAX_INJECTED_PATHS:
        return ""

    where = (
        f"{brief.owner}/{brief.repo}"
        if brief.is_whole_repo
        else f"`{brief.scope}` in {brief.owner}/{brief.repo}"
    )
    return _LISTING_TEMPLATE.format(where=where, listing="\n".join(sorted(paths)))


def _skill_section(name: str) -> str:
    """The matched skill's markdown, as a prompt section."""
    body = skill_body(name)
    if not body:
        return ""

    # `.format` on the template only, never on the body: a skill is markdown a
    # person edits freely, and a stray brace in it would raise here rather than
    # in the file that put it there.
    return _SKILL_TEMPLATE.format(name=name, body=body)


def briefing_for(text: str) -> str:
    """Everything that can be worked out about a brief without a model.

    Returns the empty string when nothing can be — an unparseable brief, an
    unreachable repository, a scope too large to inject, an ecosystem neither
    skill claims. Every one of those is a normal outcome, and each leaves the
    explorer exactly as capable as it was before this module existed.
    """
    brief = parse_brief(text)
    if brief is None:
        return ""

    tree = cached_tree(brief.owner, brief.repo)
    if tree is None:
        return ""

    paths = scoped_paths(tree, brief)
    if not paths:
        return _EMPTY_SCOPE_TEMPLATE.format(
            scope=brief.scope, owner=brief.owner, repo=brief.repo
        )

    section = _listing_section(brief, paths)
    if (skill := match_skill(paths)) is not None:
        section += _skill_section(skill)
    return section


class BriefingMiddleware(AgentMiddleware[Any, Any, Any]):
    """Splice an explorer's file list and matched skill into its system prompt.

    Runs on every model call rather than only the first, because a system prompt
    is rebuilt per request and there is nowhere cheaper to put this: the brief is
    in the thread the whole time, and both answers are memoised, so a later turn
    costs a dictionary lookup.

    Attached to the explorer alone. The orchestrator holds no reading tools by
    design and the doc-writer holds none at all, so neither has any use for a
    repository listing — and handing the doc-writer one would quietly undo the
    blindfold Phase 4 rests on, by putting real repository paths in front of the
    one agent that must not cite a path nobody read.
    """

    def _augment(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        # The first human message is the brief: `task` starts a sub-agent with
        # `messages=[HumanMessage(description)]` and nothing else, so this is the
        # orchestrator's dispatch and not something the model wrote. `str()`
        # because `.text` returns a str subclass, and this value is concatenated.
        brief = next(
            (str(message.text) for message in request.messages if message.type == "human"),
            None,
        )
        if not brief:
            return request

        section = briefing_for(brief)
        if not section:
            return request

        base = request.system_message
        prompt = base.content if base else ""
        if not isinstance(prompt, str):
            # Gemini fills message content with typed blocks in some responses,
            # and a system prompt built that way cannot be concatenated. Leaving
            # the request alone is the safe branch: the explorer keeps
            # `get_repo_tree` and the skills mount, so it can still do the job.
            return request

        return request.override(system_message=SystemMessage(prompt + section))

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._augment(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        return await handler(self._augment(request))
