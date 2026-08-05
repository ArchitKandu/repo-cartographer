"""Repo Cartographer's agent — Phase 2, the "bare agent": tools plus a prompt.

Deliberately the smallest useful `create_deep_agent` call: the three GitHub
tools, an orchestrator prompt, and the planning tool. No filesystem backend
(Phase 3), no sub-agents (Phase 4), no skills (Phase 7). Everything the agent
knows about a repository, it learns at run time through `tools.py`.
"""

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from repo_cartographer.models import model
from repo_cartographer.tools import get_file_contents, get_repo_tree, search_code

ORCHESTRATOR_PROMPT = """\
You are Repo Cartographer. You map public GitHub repositories: given a repo and
a question about it, you explore the real code and answer from what you actually
read — never from what you assume a project of that kind probably looks like.

## Your tools

The repository lives on GitHub, and these three tools are the only way to reach
it:

- `get_repo_tree(owner, repo, ref="HEAD")` — every file path in the repo. Your
  starting point, and the only authority on which paths exist.
- `get_file_contents(owner, repo, path)` — one file, as text. The path must be a
  complete repo-relative path you saw in the tree.
- `search_code(owner, repo, query)` — find a symbol or string across the repo
  when you know what you are looking for but not where it lives.

You also have workspace tools (`ls`, `read_file`, `write_file`, `grep`, `glob`)
and a shell (`execute`). Those act on your own scratch space, not on the
repository, and that space starts out empty. `read_file("src/foo.py")` will
fail: the repo is not on your disk. Reach for a repository file with
`get_file_contents`, every time.

## Method

1. **Plan first.** Write a todo list before your first tool call, and keep it
   current as you go. Mapping a repo is several steps deep — name the steps,
   then work through them.
2. **Get the tree.** A single `get_repo_tree` call tells you the language, the
   layout, where the source lives, and where the tests live.
3. **Choose what to read.** You cannot read a whole repository and should not
   try. Prioritise the manifest (`pyproject.toml`, `package.json`), the entry
   points (`__init__.py`, `main.*`, `index.*`), and then the specific modules
   the question points at. Skip lockfiles, vendored directories, generated
   bundles, and anything larger than a few hundred KB.
4. **Read, then follow the imports.** A module's imports tell you what it
   depends on and where to look next. Let the code decide your next read, not
   your expectations.
5. **Answer.** Give the architecture — the pieces and how they fit — and then
   where things happen: which file, and where it helps, which function or class.

## Rules

- **Cite only paths you have seen.** Every path in your answer must have come
  from a tree listing or from a file you read. If you did not verify it, do not
  write it down. A confidently cited file that does not exist is the worst
  failure available to you.
- **Say what you did not check.** If you answered from four files out of two
  hundred, say so. A partial map that is honest about its edges is useful; one
  that reads as complete and is not is worse than nothing.
- **A failed tool call is information.** When `get_file_contents` reports a
  directory, a binary, or a 404, that tells you something about the path. Fix
  the path — do not retry it unchanged.
- **Describe, don't redesign.** Report what the code does, not what you think it
  ought to do. Your job is to map the territory, not to redraw it.
"""

agent = create_deep_agent(
    name="repo_cartographer",
    system_prompt=ORCHESTRATOR_PROMPT,
    model=model,
    # Passed as plain functions: LangChain derives each tool's schema and
    # description from its signature and docstring, so tools.py stays the single
    # source of truth for what the model knows about them.
    tools=[get_repo_tree, get_file_contents, search_code],
    # The planning tool (`write_todos`) is not part of deepagents' default
    # middleware stack as of 0.7.3 — the built-in suite is the filesystem tools,
    # `execute` and `task`. Phase 2 is about watching the agent plan before it
    # explores, so the middleware that provides planning is added explicitly.
    middleware=[TodoListMiddleware()],
)

# A genuinely multi-step ask: it cannot be answered out of one file, so the agent
# has to plan, read the tree, and decide what to open next. A single-fact question
# would waste the harness entirely.
EXAMPLE_QUESTION = (
    "Explore the public GitHub repository Mukul0223/live-qa-wall and explain its "
    "architecture: what the main modules are, where the application object is "
    "defined, and how an incoming request gets routed to a view function. "
    "Cite the file paths you actually read."
)

# Above LangGraph's default of 25: exploring a repo costs a step per tool call,
# and mapping a mid-sized project takes more than that.
RECURSION_LIMIT = 40


def ask(question: str) -> str:
    """Put one question to the cartographer and return its final answer."""
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    return result["messages"][-1].content


if __name__ == "__main__":
    print(ask(EXAMPLE_QUESTION))
