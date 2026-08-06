"""Repo Cartographer's agent — Phase 3: the bare agent plus somewhere to put things.

Phase 2 was tools and a prompt. Phase 3 adds one variable and no more: a
workspace on disk the agent can write to, so what it learns about a repository
stops living exclusively in a message thread that is re-sent on every turn.
Still no sub-agents (Phase 4) and no skills (Phase 7). Everything the agent
knows about a repository, it learns at run time through `tools.py`.

What lives here is the wiring — model, tools, middleware, and the order they go
in. The instructions the agent works from are in `prompts.py`, and the model it
reasons with is in `models.py`, so neither is a reason to edit this file.
"""

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langgraph.graph.state import CompiledStateGraph

from repo_cartographer.middleware import RestrictToolsMiddleware
from repo_cartographer.models import model
from repo_cartographer.prompts import ORCHESTRATOR_PROMPT
from repo_cartographer.tools import get_file_contents, get_repo_tree, search_code

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
    backend = FilesystemBackend(root_dir=WORKSPACE)

    # Annotated rather than inferred: each middleware class parameterises
    # `AgentMiddleware` differently, so mypy joins the list to a type narrower
    # than the parameter accepts and rejects entries that are perfectly valid at
    # run time. The annotation states the type the call actually wants.
    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        # The planning tool (`write_todos`) is not part of deepagents' default
        # middleware stack as of 0.7.3 — the built-in suite is the filesystem
        # tools, `execute` and `task`. Watching the agent plan before it explores
        # is Phase 2's whole point, so planning is added explicitly.
        TodoListMiddleware(),
        # Replaces, rather than adds to, the FilesystemMiddleware
        # `create_deep_agent` installs by default: custom middleware is merged
        # into the stack by `.name`, so a same-named entry is substituted in
        # place. The eviction threshold is the only reason to restate it;
        # everything else here is the default.
        FilesystemMiddleware(
            backend=backend,
            tool_token_limit_before_evict=tool_result_token_limit,
        ),
        # ...and the built-ins this phase has no use for are taken away again.
        # Last in the list so it runs after everything that injects tools. See
        # middleware.py for why this is worth the four extra lines.
        RestrictToolsMiddleware(),
    ]

    return create_deep_agent(
        name="repo_cartographer",
        system_prompt=ORCHESTRATOR_PROMPT,
        model=model,
        # Passed as plain functions: LangChain derives each tool's schema and
        # description from its signature and docstring, so tools.py stays the
        # single source of truth for what the model knows about them.
        tools=[get_repo_tree, get_file_contents, search_code],
        # Files land in ./workspace instead of graph state. Note that this is not
        # what reduces context — a StateBackend keeps files out of the message
        # thread just as well. What disk buys is that the notes outlive the run
        # and can be read afterwards, which is the difference between claiming
        # the agent offloaded and showing the file it wrote.
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
