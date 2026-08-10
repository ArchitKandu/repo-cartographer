"""Repo Cartographer's agent — one orchestrator, three sub-agents.

Phase 3 was one agent with a workspace. Phase 4 changes one thing: the work is
split three ways. An `explorer` holds the reading tools and reads code; a
`doc-writer` holds no repository access at all and writes the guide from the
explorers' notes; the orchestrator holds only `get_repo_scopes`, which reports a
repository's shape and never its contents, and divides the work on that basis.
Everything the system knows about a repository it learns at run time through
`tools.py`.

4b added the fan-out: rather than one explorer over the whole repository, the
orchestrator reads the scope list and dispatches up to three explorers — one per
top-level directory — in a single message, so they run concurrently in separate
threads. The cap is a budget rather than a design preference; see
ORCHESTRATOR_PROMPT on why a fourth explorer makes a run fail instead of better.

The mechanism the phase is about is context quarantine. Each sub-agent runs in
its own message thread, so a file the explorer reads never enters the
orchestrator's context — only the explorer's short final report does. That is a
stronger version of what Phase 3's eviction bought: eviction takes a large tool
result out of the thread *after* paying for it once, while a sub-agent keeps it
out of the parent thread entirely.

Quarantine is not free, and the cost is worth naming: the parent can no longer
see what its children read. Everything that has to cross the boundary crosses it
through exactly two channels — a sub-agent's final message, and files in the
shared workspace — which is why the handoff conventions in `prompts.py` are
stated from all three sides.

Phase 6 added a third delegate, `link-checker`, and it is the one worth pausing
on: it holds no model. It is a `CompiledStateGraph` wrapping the plain functions
in `citations.py`, briefed through the same `task` tool and read back the same
way as the other two. What it demonstrates is that a sub-agent is a unit of
delegated work rather than a smaller model call — and what it *does* is the
first defence in this system against a wrong citation that is arithmetic rather
than instruction. The doc-writer now saves its guide to the workspace so there
is a file for it to check.

Phase 7 moved two kinds of instruction out of the prompts and into files a person
can edit without opening Python: `skills/*/SKILL.md`, which the explorer reads
only when the repository matches the ecosystem it describes, and `AGENTS.md`,
this project's house style, appended to the doc-writer on every run. `skills.py`
holds both, and the composite backend that makes the skills mount reachable by
the explorer's own `read_file`.

What lives here is the wiring — models, tools, middleware, and the order they go
in. The instructions each agent works from are in `prompts.py`, `skills/` and
`AGENTS.md`, and the model they reason with is in `models.py`, so none of them is
a reason to edit this file.
"""

from pathlib import Path
from typing import Any

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware import FilesystemMiddleware
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langgraph.graph.state import CompiledStateGraph

from repo_cartographer.link_checker import (
    LINK_CHECKER_DESCRIPTION,
    build_link_checker,
)
from repo_cartographer.middleware import RestrictToolsMiddleware
from repo_cartographer.models import MODEL_PROFILE_KEY, model
from repo_cartographer.prompts import (
    DOC_WRITER_PROMPT,
    EXPLORER_PROMPT,
    ORCHESTRATOR_PROMPT,
)
from repo_cartographer.skills import (
    HOUSE_STYLE_HEADER,
    SKILLS_MOUNT,
    build_backend,
    house_style,
)
from repo_cartographer.tools import (
    get_file_contents,
    get_repo_scopes,
    get_repo_tree,
    search_code,
)

# Anchored to this file, not the working directory, for the same reason models.py
# anchors its .env: a run started from a REPL or another directory should reach
# the same workspace.
WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
WORKSPACE.mkdir(exist_ok=True)

# Below the 20_000-token default, and deliberately so. Eviction is the mechanism
# that moves the number Phase 3 is measured on: a tool result over this limit is
# written to the workspace and replaced in the thread by a head-and-tail preview
# plus the path to read the rest from. At the default, nothing `get_repo_tree` or
# `get_file_contents` returns is anywhere near large enough to trigger it, so the
# feature would be installed and never fire. 2_000 tokens is roughly a 300-line
# source file — large enough that the agent's own notes and small files stay
# inline, small enough that the big reads offload.
TOOL_RESULT_TOKEN_LIMIT = 2_000

# Read once at import rather than per `build_subagents` call: the file does not
# change while the process runs, and reading it inside the spec builder would put
# a disk hit in the path `tests/test_wiring.py` calls repeatedly.
_HOUSE_STYLE = house_style()

# `create_deep_agent` adds a `general-purpose` sub-agent to the `task` tool's menu
# unless told otherwise, described to the model as having "all tools as the main
# agent" — which, now that the orchestrator holds no tools at all, means none. It
# is a delegate that can do nothing, advertised as the one that can do anything,
# and Phase 2 already measured what an unusable tool costs: 8 of 16 calls spent on
# a workspace that had nothing in it. So it is switched off, and the `task` menu
# lists exactly the two agents this project defines.
#
# A harness profile is the library's own mechanism for this. Registration is keyed
# by model, and the key comes from models.py because its shape differs per
# provider. Two things to know if it ever stops working: a key that fails to match
# does not raise, it silently leaves the default sub-agent in place — so
# `tests/test_wiring.py` asserts the menu, rather than trusting this call — and
# `deepagents.profiles` is a documented beta API, so an upgrade could move it.
register_harness_profile(
    MODEL_PROFILE_KEY,
    HarnessProfileConfig(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)


def build_subagents(
    backend: BackendProtocol, *, tool_result_token_limit: int | None
) -> list[SubAgent | CompiledSubAgent]:
    """The three delegates, as specs rather than compiled graphs.

    Kept as a separate function, returning plain dicts, so the split can be
    checked without building an agent or spending a request: `tests/test_wiring.py`
    asserts against what this returns. That is the whole reason it is not inlined
    into `build_agent`.

    Two of the three are `SubAgent` — a prompt, a model and some tools. The
    third, added at Phase 6, is a `CompiledSubAgent`: a graph with no model in
    it at all. The union in the return type is the phase's argument stated in
    Python. The `task` tool builds its menu from this one list, briefs every
    entry the same way and reads every entry's final message the same way, so
    the orchestrator cannot tell from the outside which of its delegates thinks.
    A sub-agent is a unit of delegated work, not a smaller model call.

    Two things here are easy to get wrong and silent when you do:

    `tools` must be given explicitly, including the empty list. A spec that omits
    the key inherits the parent's tools — which used to be the GitHub three and is
    now nothing at all, so *both* mistakes are available depending on the phase.
    `doc_writer`'s `"tools": []` is what makes its lack of repository access a
    property of the graph instead of a promise in a prompt.

    Each spec restates `FilesystemMiddleware`, because a parent's middleware is
    not inherited by declarative sub-agents. Without it, both delegates get the
    full built-in suite — `execute`, `glob`, `grep`, `delete` — every tool
    `RestrictToolsMiddleware` is careful to keep away from the orchestrator, handed
    straight back to the agents doing the actual work. Naming `tools=` here is
    also stronger than hiding them: excluded tools are never constructed, so they
    cannot be dispatched even by a malformed tool call.
    """
    # Annotated, and pulled out of the dicts below, for the reason `build_agent`'s
    # middleware list is annotated: a bare list of one FilesystemMiddleware infers
    # to a type narrower than `SubAgent["middleware"]` accepts, and mypy rejects an
    # entry that is correct at run time.
    explorer_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        FilesystemMiddleware(
            backend=backend,
            # `write_file` for its own notes, and `read_file` because eviction
            # needs it: when a tool result crosses the threshold below, the
            # middleware replaces it with a preview plus a workspace path to read
            # the rest from, and an explorer without `read_file` cannot follow that
            # up. Eviction fires here and nowhere else, so this is the one agent for
            # which `read_file` is load-bearing rather than convenient.
            tools=["read_file", "write_file"],
            # Restated, or Phase 3's mechanism quietly dies here. Sub-agents get a
            # fresh FilesystemMiddleware at the library default of 20_000 tokens —
            # and after this refactor the explorer is the only agent making large
            # reads, so the tuned threshold would be installed on the two agents
            # that no longer need it and absent from the one that does.
            tool_token_limit_before_evict=tool_result_token_limit,
        ),
    ]
    doc_writer_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        FilesystemMiddleware(
            backend=backend,
            # `ls` on purpose: the orchestrator's brief lists the notes paths,
            # but a brief is a claim. Listing the directory is how the doc-writer
            # learns an explorer died half way, instead of writing a guide around
            # a file that is not there.
            #
            # `write_file` arrived at Phase 6 and is the one tool this agent
            # gained since it was created. It does not weaken the blindfold —
            # the workspace is not GitHub, and the doc-writer still has no way to
            # read a line of the repository — but it is what makes the guide a
            # file on disk rather than only a message in a thread, and a file is
            # the only thing a non-model checker can be pointed at.
            tools=["ls", "read_file", "write_file"],
        ),
    ]

    return [
        {
            "name": "explorer",
            # The orchestrator picks a delegate from this line and nothing else, so
            # it says what the agent needs handed to it, not just what it does.
            "description": (
                "Reads one scope of a public GitHub repository — a top-level "
                "directory, or the whole repo when it is small — and writes what it "
                "finds to a notes file in the workspace. Has the GitHub tools. "
                "Brief it with the owner, the repo, the scope, the question in "
                "full, and the exact workspace path to write its notes to."
            ),
            "system_prompt": EXPLORER_PROMPT,
            "tools": [get_repo_tree, get_file_contents, search_code],
            "middleware": explorer_middleware,
            # Phase 7, and it goes on this agent alone. The explorer is the only
            # one deciding which files to open, so ecosystem knowledge — read
            # `package.json` before guessing at `src/`, `lib/` is usually build
            # output — is worth something here and worth nothing anywhere else.
            # `SkillsMiddleware` shows it one line per skill and lets it read the
            # rest only if the repository matches, so a Node run never pays for
            # the Python conventions. See `skills.py` for how the mount is made
            # reachable by this agent's own `read_file`.
            "skills": [SKILLS_MOUNT],
        },
        {
            "name": "doc-writer",
            "description": (
                "Writes the onboarding guide from notes already in the workspace. "
                "Saves it to the workspace path you name and also returns it as "
                "its final message. Has no repository access, so anything it "
                "should mention must already be in the notes. Brief it with the "
                "question, the exact notes paths, and where to save the guide."
            ),
            # The house style is appended rather than woven in: everything in
            # DOC_WRITER_PROMPT is *how to do the job*, and everything in
            # AGENTS.md is *how this project's guides read*. Keeping the seam
            # visible is what lets a non-programmer change the second without
            # touching the first — which is the whole of Phase 7.
            "system_prompt": DOC_WRITER_PROMPT + HOUSE_STYLE_HEADER + _HOUSE_STYLE
            if _HOUSE_STYLE
            else DOC_WRITER_PROMPT,
            # Not an oversight, and not inheritance — the guarantee the whole
            # design rests on. An agent that cannot reach GitHub cannot cite a file
            # nobody read.
            "tools": [],
            "middleware": doc_writer_middleware,
        },
        {
            "name": "link-checker",
            "description": LINK_CHECKER_DESCRIPTION,
            # `runnable` instead of `system_prompt`/`tools`, and that difference
            # is the whole of Phase 6. This delegate holds no model: it is a
            # one-node graph running the plain functions in `citations.py`
            # against the repository's real file tree. See `link_checker.py` for
            # why the least intelligent agent here is the only one that cannot be
            # talked out of its job.
            "runnable": build_link_checker(backend),
        },
    ]


def build_agent(
    *, tool_result_token_limit: int | None = TOOL_RESULT_TOKEN_LIMIT
) -> CompiledStateGraph:
    """Build a cartographer. The one argument is Phase 3's variable.

    `tool_result_token_limit=None` disables eviction, which is the control arm:
    identical prompt, identical tools, identical workspace, but large tool
    results stay inline in the message thread instead of being written to the
    workspace and replaced by a preview. Comparing the two is what
    `scripts/measure_context.py` does.
    """
    # One instance, referenced twice below. `create_deep_agent` passes its
    # `backend` to the summarization middleware and the general-purpose sub-agent
    # as well as to the filesystem tools, so handing those a different instance
    # than the one the tools write through would split the workspace in two.
    #
    # Since Phase 7 it is a composite rather than a bare filesystem: the writable
    # workspace at `/`, plus a read-only mount of `./skills` at `/skills/`. That
    # is what lets `SkillsMiddleware` advertise a path the explorer's own
    # `read_file` can actually open — see `skills.py`.
    backend = build_backend(WORKSPACE)

    # Annotated rather than inferred: each middleware class parameterises
    # `AgentMiddleware` differently, so mypy joins the list to a type narrower
    # than the parameter accepts and rejects entries that are perfectly valid at
    # run time. The annotation states the type the call actually wants.
    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        # The planning tool (`write_todos`) is not part of deepagents' default
        # middleware stack as of 0.7.3 — the built-in suite is the filesystem
        # tools, `execute` and `task`. Watching the agent plan before it explores
        # is Phase 2's whole point, so planning is added explicitly. Note it lands
        # on the orchestrator alone: the sub-agent stack is filesystem,
        # summarization and tool-call patching, with no planning middleware, so
        # neither delegate can write todos. That is the right division — a delegate
        # is handed a plan, it does not make one — and it is why EXPLORER_PROMPT
        # has no planning step.
        TodoListMiddleware(),
        # Replaces, rather than adds to, the FilesystemMiddleware
        # `create_deep_agent` installs by default: custom middleware is merged
        # into the stack by `.name`, so a same-named entry is substituted in
        # place. The eviction threshold is the only reason to restate it;
        # everything else here is the default.
        FilesystemMiddleware(
            backend=backend,
            # Still worth setting, though the orchestrator no longer reads files:
            # what flows into this thread now is sub-agent reports, and an explorer
            # that returns something enormous gets offloaded the same way a large
            # file used to be.
            tool_token_limit_before_evict=tool_result_token_limit,
        ),
        # ...and the built-ins this phase has no use for are taken away again.
        # Last in the list so it runs after everything that injects tools. See
        # middleware.py for why this is worth the four extra lines — and note it
        # applies to the orchestrator only, which is why each sub-agent narrows its
        # own tools in `build_subagents`.
        RestrictToolsMiddleware(),
    ]

    return create_deep_agent(
        name="repo_cartographer",
        system_prompt=ORCHESTRATOR_PROMPT,
        model=model,
        # One tool, and the boundary it draws is the point. Leave the reading tools
        # here and the orchestrator keeps using them: delegating costs it a turn and
        # a leap of faith, and a model takes the shorter path. You would end up with
        # a `task` tool it never calls, a trace identical to Phase 3, and the
        # conclusion that sub-agents did not help.
        #
        # `get_repo_scopes` is the exception 4b requires, because fanning out per
        # top-level directory means knowing what the directories are, and an
        # orchestrator holding no GitHub tools at all cannot find out. It returns
        # directory names and file counts — the repository's *shape* — and no file
        # contents, so it buys the orchestrator the ability to divide the work
        # without the ability to do it. Everything about what the code says still
        # has to come back from an explorer.
        tools=[get_repo_scopes],
        # Passed as plain functions inside the specs: LangChain derives each tool's
        # schema and description from its signature and docstring, so tools.py stays
        # the single source of truth for what the model knows about them.
        subagents=build_subagents(backend, tool_result_token_limit=tool_result_token_limit),
        # One backend for all three agents, which is what makes the workspace a
        # channel between them rather than three private scratch spaces: the
        # explorer writes /notes/overview.md and the doc-writer reads that same
        # file. Sub-agents inherit this instance from here — it is not something
        # their specs set.
        backend=backend,
        middleware=middleware,
    )


agent = build_agent()

# A genuinely multi-step ask: it cannot be answered out of one file, so the agent
# has to plan, read the tree, and decide what to open next. A single-fact question
# would waste the harness entirely.
EXAMPLE_QUESTION = (
    "Explore the public GitHub repository cloudflare/computer and explain its "
    "architecture: what the main modules are, where the application object is "
    "defined, and how an incoming request gets routed to a view function. "
    "Cite the file paths you actually read."
)

# Above LangGraph's default of 25: a step is spent per model turn *and* per tool
# call, so the 9 calls a psf/requests map costs are already ~20 steps. The margin
# is for larger repos, not for thrash — a run that spirals here is a prompt
# problem, and raising this only hides it.
RECURSION_LIMIT = 60


def map_repo(question: str, cartographer: CompiledStateGraph | None = None) -> dict[str, Any]:
    """Run one mapping and return the final graph state.

    The whole state, not just the answer, because the message list is the subject
    of Phase 3's measurement — `scripts/measure_context.py` reads token usage off
    it. `ask` is the wrapper for callers who only want the prose.
    """
    return (cartographer or agent).invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": RECURSION_LIMIT},
    )


def ask(question: str, cartographer: CompiledStateGraph | None = None) -> str:
    """Put one question to the cartographer and return its final answer as prose."""
    result = map_repo(question, cartographer)
    # `.text`, not `.content`. Gemini fills `content` with a list of typed blocks —
    # the answer plus an encrypted thought signature in `extras` — so printing
    # `content` dumps a repr of that structure instead of the answer. OpenRouter
    # puts a plain string there. `.text` concatenates the text blocks in both
    # cases, which is the only shape a caller of this function wants.
    return result["messages"][-1].text


if __name__ == "__main__":
    print(ask(EXAMPLE_QUESTION))
