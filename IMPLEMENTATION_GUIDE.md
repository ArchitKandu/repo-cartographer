# Repo Cartographer — Phased Implementation Guide

A deep agent that reads any public GitHub repo and writes an onboarding guide: architecture overview, "where things happen," and a prioritized list of good first issues.

**The rule behind every phase below: change one variable at a time.** Each phase should end in something that actually runs, that you can point to and say "this works *because of* X." Build sub-agents, filesystem, skills, and memory all in one sitting and something breaks — you won't know which of the four new things caused it. Skip a phase only if you're already confident you understand exactly what it isolates.

---

## Phase 0 — Environment & scaffolding

**Build**
- `uv init repo-cartographer --python 3.11 && cd repo-cartographer`
- `uv add deepagents langchain-anthropic pygithub python-dotenv`
- `uv add --dev ruff mypy pytest`
- `.env` with `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` — get the token now. The unauthenticated GitHub API caps at 60 req/hour, and this agent reads whole file trees.
- `pyproject.toml`, `langgraph.json`, `.gitignore`, folder structure, first push to a real GitHub remote.

**Definition of done:** `uv run python -c "import deepagents; print(deepagents.__version__)"` prints something. `git push` succeeds against a real remote.

**Why this way:** every hour spent fighting your environment later is an hour not spent thinking about sub-agents or filesystems. Getting this fully working — and committed — before writing any agent logic means every later phase is pure logic, not logic tangled up with "is this a venv problem or a real bug." It also means the repo reads as professional from commit one, before anyone's read a line of code.

---

## Phase 1 — Tools layer (no agent yet)

**Build:** plain, boring Python functions. No LangChain, no agent — just functions and tests.

```python
def get_repo_tree(owner: str, repo: str, ref: str = "HEAD") -> list[str]: ...
def get_file_contents(owner: str, repo: str, path: str) -> str: ...
def search_code(owner: str, repo: str, query: str) -> list[dict]: ...
```

Write `tests/test_tools.py` against these, calling them for real against a small public repo, before any LLM is involved.

**Definition of done:** `uv run pytest tests/test_tools.py` passes with zero mocking — these should actually hit GitHub.

**Why this way:** this is the one deterministic, cheaply-verifiable layer in the whole system. An LLM's behavior is probabilistic and slow to check; a Python function's isn't. If `get_repo_tree` mishandles pagination or a rate-limit response, every later phase inherits that bug — wrapped in enough agent reasoning that you'll waste hours suspecting the prompt when the real problem is a function you never actually tested. Nail this first and you get to trust it for the rest of the build.

---

## Phase 2 — The bare agent (v0)

**Build:** the smallest possible `create_deep_agent` call — tools and a system prompt, nothing else.

```python
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-5-20250929",
    tools=[get_repo_tree, get_file_contents, search_code],
    system_prompt=ORCHESTRATOR_PROMPT,
)
```

Run it against a real repo with a genuinely multi-step ask: "explore this repo and explain its architecture."

**Definition of done:** in the transcript, it calls the built-in planning tool before calling any of yours — unprompted. If it doesn't, the ask wasn't multi-step enough; make it harder, don't add features yet.

**Why this way:** this is your control group. Before adding sub-agents, filesystem, or skills, see what the harness gives you for free over a bare tool-calling loop. Jump straight to the full-featured version and you'll never be able to say which piece caused which behavior — and "what specifically does the harness buy you" is exactly the kind of question a good interviewer asks.

---

## Phase 3 — Filesystem backend

**Build:** add `backend=FilesystemBackend(root_dir="./workspace")`. Have the orchestrator write findings to disk (`workspace/notes.md`) instead of only holding them in the running message thread.

**Definition of done:** run the same task with and without the backend; print the message history's token count both times. The number should visibly differ once file contents stop living in every turn.

**Why this way:** filesystem before sub-agents, deliberately, even though sub-agents are the more exciting feature. Add both at once and you can't tell whether an improvement came from isolated contexts or from simply not stuffing whole files into one growing thread. One variable at a time — and this is the simpler of the two mechanisms, so it goes first.

---

## Phase 4 — Sub-agents: explorer & doc-writer

**Build:** split the single agent into an orchestrator plus two sub-agents.

```python
explorer = {
    "name": "explorer",
    "description": "...",
    "system_prompt": EXPLORER_PROMPT,
    "tools": [get_repo_tree, get_file_contents, search_code],
}
doc_writer = {
    "name": "doc-writer",
    "description": "...",
    "system_prompt": DOC_WRITER_PROMPT,
    "tools": [],
}
```

For a larger repo, let the orchestrator invoke `explorer` once per top-level directory — in parallel, the same pattern the library's own `deep_research` example uses to cap concurrent researchers.

**Definition of done:** open the LangSmith or LangGraph Studio trace. You should see two or more distinct sub-agent invocations, each with its own smaller context window — not one long, growing thread.

**Why this way:** this is a refactor of something that already works, which means you can directly observe the effect: same task, same intent, watch the trace and see separate windows instead of one growing one. That's context quarantine made visible instead of just asserted in a doc.

---

## Phase 5 — A tiny eval set

**Build:** `tests/evals/known_repos.jsonl` — 5-10 well-known repos (flask, requests, fastapi) with a few facts the doc-writer's output should contain. A pytest check or LangSmith eval run scoring actual output against expected.

**Definition of done:** a single command produces a number — e.g. "7/10 expected facts present."

**Why this way:** introduce this right after sub-agents produce something worth scoring — not at the end, when you're tired and tempted to skip it. Every phase after this one (link-checker, skills, human-in-the-loop) can now be regression-tested against the same fixed set instead of you eyeballing output and hoping nothing broke.

---

## Phase 6 — The non-LLM sub-agent: link-checker

**Build:** a `link-checker` sub-agent that is deliberately *not* an LLM call — a plain function or small `CompiledStateGraph` that checks every file path the doc-writer cited against the real repo tree from Phase 1.

**Definition of done:** deliberately feed the doc-writer a fake file path and confirm `link-checker` flags it before a human ever sees the output.

**Why this way:** this is the step people skip, because a plain function feels like "not a real sub-agent." It's the one that actually proves a sub-agent is a unit of delegated work, not a smaller LLM call — and it's a genuine safety net against the exact failure mode this task invites: an agent confidently citing a file that doesn't exist.

---

## Phase 7 — Skills & memory

**Build:** `skills/python-repo/SKILL.md` and `skills/node-repo/SKILL.md` — what to look for differs by ecosystem (`pyproject.toml` + `tests/` vs. `package.json` + `src/`). `AGENTS.md` for your own house style (e.g. "always end with a good-first-issue section").

**Definition of done:** run against a Python repo and a JS repo back to back. Output structure should visibly follow the matching skill, without you touching any code.

**Why this way:** by now the system prompt is trying to hold "how to explore any repo," "Python conventions," "JS conventions," and "your doc style" all at once, and it's sprawling. Skills let each concern live in its own file, loaded only when relevant, instead of the orchestrator prompt growing every time you add a new ecosystem.

---

## Phase 8 — Human-in-the-loop (stretch)

**Build:** if you add a "propose a fix, open a draft PR" capability, gate it: `interrupt_on={"open_pull_request": True}`.

**Definition of done:** trigger the guarded tool and confirm execution actually pauses for approval — not just that the parameter is set.

**Why this way:** worth doing even if the PR-opening feature never ships, because "how do you stop the agent from doing something irreversible" is the single most common question a hiring manager asks about agent projects. "I gated every write action behind `interrupt_on`" is a complete answer.

---

## Phase 9 — Ship it

**Build:** a GitHub Actions workflow running `ruff` + `pytest` on push. Deploy via LangGraph Platform (using the `langgraph.json` from Phase 0) or containerize behind a thin front end. A README with an architecture diagram, real output from a repo people recognize, and one sentence on why an agent was necessary here.

**Definition of done:** a green CI badge, and a link someone can click without cloning anything.

**Why this way:** the learning is banked by now. This phase is entirely about packaging it so a recruiter can engage with it in thirty seconds instead of needing to clone and run a script.

---

## At a glance

| Phase | Deliverable | Concept it isolates |
|---|---|---|
| 0 | Env, repo, config | — |
| 1 | Tools + tests | Deterministic foundation |
| 2 | Bare agent | The harness vs. a bare loop |
| 3 | Filesystem backend | Context offloading |
| 4 | Explorer + doc-writer | Context quarantine |
| 5 | Eval set | Measurable regressions |
| 6 | Link-checker | Sub-agents ≠ smaller LLMs |
| 7 | Skills + memory | Prompt decomposition |
| 8 | `interrupt_on` | Approval gates |
| 9 | CI + deploy | Packaging |
