# Repo Cartographer

An AI agent that reads a public GitHub repository and explains it — architecture,
where things happen, and how the pieces fit together.

Instead of cloning a project and reading it top to bottom yourself, you point the
agent at `owner/repo` and ask a question. It walks the file tree, decides which
files are worth opening, reads them, and answers from what it actually found.

Built on [deepagents](https://pypi.org/project/deepagents/) (a LangGraph agent
harness) with a small, deliberately boring GitHub tools layer underneath.

**New here?** [ARCHITECTURE.md](ARCHITECTURE.md) explains the whole system from
scratch — the agents (one of which contains no AI at all), how the files connect,
and one real run traced step by step, with no prior knowledge of the codebase
assumed.

---

## Status

This project is being built in phases, one concept at a time — see
[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) for the full plan and the
reasoning behind the ordering.

| Phase | What it adds | State |
|---|---|---|
| 0 | Environment, packaging, config | Done |
| 1 | GitHub tools layer + tests | Done — 26 tests, no mocking |
| 2 | Bare agent (tools + prompt) | Done |
| 3 | Filesystem backend | Done |
| 4a | Explorer + doc-writer sub-agents | Done |
| 4b | Parallel fan-out per directory | **Done — trace verified, this is what runs today** |
| 5 | Eval set: six repos, one score | Done |
| 6 | Link-checker: a sub-agent with no model in it | Done |
| 7+ | Skills, approval gates, packaging | Planned |

Phase 2's definition of done — *"in the transcript, it calls the built-in planning
tool before calling any of yours, unprompted"* — is met. On `psf/requests` with
`gemini-3.5-flash-lite`:

```
 0. write_todos       (4 todos, unprompted, before any repository access)
 1. get_repo_tree     psf/requests
 2. write_todos       (tree step → completed)
 3. get_file_contents src/requests/__init__.py
 4. write_todos
 5. get_file_contents src/requests/adapters.py
 6. get_file_contents src/requests/sessions.py
 7. write_todos
 8. write_todos       → then answered in prose
```

Nine tool calls, no wasted ones. The answer correctly located wire-level sending
in `adapters.py` (`HTTPAdapter.send`) and session state in `sessions.py`.

Phase 3's definition of done is a number: run the same task with and without
offloading and watch the message history shrink. `uv run
scripts/measure_context.py` runs both arms and prints the comparison. On
`psf/requests` with `gemini-3.5-flash-lite`:

```
                             offload off            offload on
cumulative input tokens          241,196               211,386
final-turn input tokens           37,786                21,443
results evicted                        0                     2
```

The final-turn figure is the one to read: by the end of the run the thread had
grown to 37.8k tokens without offloading and 21.4k with it, because two file
reads had been written to `workspace/` and replaced in the thread by a preview.
Cumulative input — what the run actually cost, since the thread is re-sent every
turn — fell 12%.

Both numbers are a single run per arm, and the arms are not deterministic: the
model chooses which files to open. `--repeats N` reports the spread instead, and
the script says so itself when the ranges overlap. It also refuses to take credit
it hasn't earned — when nothing crossed the eviction threshold it prints that
fact rather than the difference, which is exactly what happened on the first repo
tried, whose only large file was a lockfile the prompt tells the agent to skip.

### Phase 4: one thread became four, and two of them ran at once

The definition of done is a trace showing two or more sub-agent invocations, each
in its own context window rather than one long growing thread.
`scripts/show_contexts.py` reports the same split locally, by streaming with
`subgraphs=True` — one run against `psf/requests`:

```
agent             turns  tool calls   cumulative in   peak in
-------------------------------------------------------------
orchestrator         10          10          53,739     6,997
explorer 1            5           4          29,329     8,789
explorer 2            9          12          87,689    13,564
doc-writer            3           3           7,011     3,888
-------------------------------------------------------------
whole run            27          29         177,768    13,564

Dispatch shape: [2, 1] — 2 message(s) issuing 3 task call(s), at most 2 at once.
```

70% of the run's input tokens were carried in sub-agent threads and never entered
the orchestrator's. Its own peak context was 6,997 tokens while an explorer's was
13,564 — that gap is the quarantine, and on one thread all of it would have been
the same thread.

**The trace confirms it independently.** With `LANGSMITH_TRACING=true`, the same run
appears in LangSmith as one root run with the sub-agents nested inside it — 31 runs
tagged `ls_agent_type=subagent`, and three `task` invocations with their own token
totals:

```
start 11:49:45.338  end 11:50:56.041   explorer     89,426 tokens   70.7s
start 11:49:45.348  end 11:50:20.851   explorer     30,595 tokens   35.5s
start 11:51:15.864  end 11:51:32.711   doc-writer    7,863 tokens   16.8s

explorer wall-clock overlap: 35.5s → CONCURRENT
```

The two explorers started **10 milliseconds apart** and ran together for 35.5
seconds. That is Phase 4's definition of done, met twice over: locally as tokens
per thread, and server-side as wall-clock overlap. Concurrency here is not
something the library arranges — it is the model choosing to emit two `task` calls
in *one* message. A run that sent them one per message would do identical work in
identically isolated contexts, and no token figure would tell the two apart, which
is why both the dispatch shape and the overlap are measured rather than assumed.

**On cost, one run proves nothing, and an earlier draft of this README overclaimed.**
This run totalled 177,768 input tokens — *below* Phase 3's 211,386. An earlier 4b
run on the identical question totalled 324,559. The spread between two runs of the
same architecture is far wider than the gap between architectures, because the
dominant variable is how many files the explorers happened to open. So: no cost
claim in either direction from single runs. `scripts/measure_context.py` has warned
about exactly this since Phase 3 (`--repeats N`, and it says so itself when ranges
overlap); the same caution applies here.

What *is* robust is the split, because it is structural rather than a function of
file choices: the orchestrator stays small no matter how much reading happens, and
the doc-writer composed that entire guide inside 3,888 tokens because all it ever
saw was two notes files.

One more thing worth noticing: `scripts/measure_context.py` can no longer see any
of this. It reads a finished run's `state["messages"]`, which is the orchestrator's
thread alone. Its numbers fell sharply at Phase 4 for a reason that is not a
saving, and its docstring now says so.

#### The fan-out has a hard cost, and it is not tokens

The first attempt at 4b died mid-run:

```
ChatGoogleGenerativeAIError: (RESOURCE_EXHAUSTED) 429 ...
Quota exceeded for metric: generate_content_free_tier_requests, limit: 15
Please retry in 1.010311967s.
```

Two explorers and an orchestrator stepping through their own tool loops reached
15 requests/minute in seconds. The API's own advice — *retry in 1 second* — is the
tell: this is a pacing problem, not a capacity one, so the fix is a client-side
rate limiter rather than fewer explorers.
[`models.py`](repo_cartographer/models.py) attaches one `InMemoryRateLimiter` to
the single chat model at 80% of the tier's published limit. Because every
sub-agent inherits that model *instance*, all four agents draw from one bucket —
which is the only correct arrangement, since the provider's limit is per project,
not per agent.

**So on a per-minute request budget, concurrency buys no wall-clock speed at all.**
The trace above shows it: two explorers overlapping for 35.5s, and the slower one
still taking 70.7s because it spent much of that waiting for the queue. What the
fan-out actually buys is the smaller peak — 13,564 tokens across two threads
instead of one thread holding everything — and that is the honest claim to make for
it. `max_bucket_size`
is deliberately 1: a bucket that banked unused capacity would let both explorers
start at once and spend the whole minute's budget immediately, which is the failure
it exists to prevent.

One more real failure, caught from the same wreckage. That dead run left a file at
`workspace/workspace/notes/overview.md` — the orchestrator had briefed a path
*including* the `workspace/` prefix, and since the workspace is the backend root,
the write succeeded into a second directory inside it. The explorer reported
success. Nothing errored. The doc-writer would simply have found nothing. Both
prompts now state the path rule explicitly, because this is the shape of every
handoff bug in a delegated system: it does not fail, it succeeds somewhere
useless.

### Phase 5: a number instead of an impression

Everything above was verified by looking — once, on one run, by a human reading a
transcript. That establishes a mechanism exists; it does not notice when the
mechanism stops working. So Phase 5 fixes six repositories, writes down the facts
a good guide about each one should contain, and prints a single number:

```console
$ uv run scripts/run_evals.py

case        repo                    facts  present  missing
-----------------------------------------------------------
requests    psf/requests                5        5  —
flask       pallets/flask               5        5  —
click       pallets/click               5        3  types-module, parser-module
httpx       encode/httpx                5        5  —
express     expressjs/express           6        6  —
chalk       chalk/chalk                 5        5  —
-----------------------------------------------------------
total                                  31       29

29 of 31 expected facts present (94%)
```

Six repositories, four Python and two JavaScript, chosen small enough to map in a
few minutes each and stable enough that the facts stay true. The two misses are
real and readable: mapping `pallets/click`, the explorers covered `core.py` and
`decorators.py` and never opened `types.py` or `parser.py`, so the guide could not
mention them. That is the scope-coverage question Phase 4 could only describe
anecdotally, now attached to a number.

**Nothing here is graded by a model.** Every fact is either a repo-relative path
or a term that really occurs in a named file, matched against the guide with
markdown stripped and word edges respected. A model-graded eval would measure a
second model's judgement as much as this system's output and move when nobody
changed anything; a path either appears or it does not.

**The eval set is itself verified, against the live repositories.**
`tests/test_evals.py` checks that every expected path exists and every expected
term really occurs in the file that claims it — no model involved. This is not
bookkeeping. The first draft of the dataset expected `lib/router/index.js` from
`expressjs/express`: a path every account of Express describes, and which the
repository has not contained since routing moved out to its own package.
Unverified, it would have scored as a miss on every run forever, and read as the
agent's failure rather than the dataset's.

**Two honest caveats.** The score is *recall of citations*, not quality — a guide
naming every expected file and explaining all of them wrongly scores 31 of 31.
And it is one sample per case; the agent chooses which files to open, and that
choice differs between runs of an unchanged system, so a two-point move is not a
regression. `--history` shows every run recorded so far, which is where the noise
floor becomes visible instead of assumed.

Recorded guides live in `tests/evals/results/` as a growing log, so `--score-only`
re-scores them against the current dataset instantly and for free — editing a
fact or adding a case costs no model quota, only changing the agent does. Each
record carries the model and a fingerprint of all three prompts, and any record
whose fingerprint no longer matches the prompts on disk is flagged as stale.
That is the specific way this instrument could mislead: rewrite a prompt,
re-score without re-running, and read an unchanged number as evidence the
rewrite was neutral.

#### The first thing it caught was not the agent

The first full sweep failed four of six cases:

```
[1/6] requests (psf/requests) … FAILED after 77.4s — ReadTimeout: api.github.com
[3/6] click (pallets/click) …   FAILED after 193.8s — ReadTimeout: api.github.com
[4/6] httpx (encode/httpx) …    FAILED after 101.0s — ConnectionError: RemoteDisconnected
[5/6] express (expressjs/express) … FAILED after 85.9s — ReadTimeout: api.github.com
```

Not the model, and not a rate limit — the connection to GitHub dropping. What
made it fatal was an asymmetry nobody had noticed in four phases of single runs:
a `GitHubError` or `ValueError` from the tools layer reaches the model as a tool
error it can read and route around, exactly as the prompts promise ("a failed
tool call is information"), but a raw `requests.ReadTimeout` escaped the agent
loop and ended the run. One dropped packet, and a whole mapping was gone.

[`tools.py`](repo_cartographer/tools.py) now routes every call through one
helper that retries transport failures three times with a growing pause — and
only transport failures, never an HTTP answer, because a 404 will not become a
200 and a 403 from the search quota gets worse if you ask again. When the retries
run out it raises `GitHubError`, so a dropped connection is now the same kind of
event as a 404: a fact about one call, not the end of the job. Re-running the
same four cases unchanged: 5/5, 3/5, 5/5, 6/6.

A bug that survives four phases of watching runs and dies in the first sweep of a
fixed set is the argument for the phase, made better than any claim about it
would have been.

### Phase 6: the delegate with no model in it

The worst thing this project can do is cite a file that does not exist. A reader
trusts the path, opens their editor, finds nothing — and the guide was more useful
before that sentence than after it.

Five phases attacked that with instructions. Three prompts say *cite only what you
verified*; Phase 4 added the one structural guarantee, that the doc-writer holds no
GitHub tools and so cannot invent a path it never read. Neither stops it faithfully
repeating an explorer's mistake.

Phase 6 adds a third delegate that closes the gap, and the interesting thing about
it is what it is not:

```python
{
    "name": "link-checker",
    "description": LINK_CHECKER_DESCRIPTION,
    "runnable": build_link_checker(backend),   # not system_prompt, not tools
}
```

No model. A `CompiledStateGraph` with one node running plain Python: read the guide
the doc-writer saved, read the repository's real file tree, report every cited path
that is not in it. It sits in the same `task` menu as the explorer and the
doc-writer, is briefed the same way, and returns a final message the same way — the
orchestrator cannot tell from the outside that nothing thought.

That is the phase's argument. **A sub-agent is a unit of delegated work, not a
smaller model call.** And "does this path exist" is decidable from a file tree, so
asking a model would cost a request, take seconds, and be wrong occasionally in a
way nothing downstream could detect. The least intelligent agent in the system is
the only one that cannot be talked out of its job.

**The definition of done — feed the doc-writer a fake path and confirm it gets
flagged — is a script.** It plants notes about `psf/requests` that are accurate
except for one invented `src/requests/router.py`, runs the real doc-writer over
them, and runs the real checker over what it produced:

```console
$ uv run scripts/prove_link_checker.py

1. running the real doc-writer over the poisoned notes …
   guide returned: 2544 chars
   guide on disk:  yes

2. did the doc-writer repeat the fake path?
   YES — it cites src/requests/router.py, exactly as an explorer's mistake would carry.

3. running the real link-checker over the guide it wrote …

   │ CITATION CHECK — psf/requests
   │ 7 file path(s) cited · 6 verified against the real repository tree · 1 NOT FOUND
   │
   │ NOT FOUND — these paths do not exist in psf/requests:
   │   - src/requests/router.py

PASS — src/requests/router.py was flagged before a human ever saw the guide,
by a delegate that made no model call to do it.
```

`tests/test_citations.py` asserts the same thing in milliseconds with no model and
no network at all — which is the other thing a non-AI delegate buys you: its
definition of done is a unit test rather than a run you hope to be watching.

**The trace confirms it costs nothing, on every run.** Across the six-repository
eval sweep, LangSmith recorded:

```
                runs   tokens            avg duration
explorer          48   6,783 – 440,016        94.77s
doc-writer        20   7,075 –  30,185        24.89s
link-checker      16       0 –       0         4.02s
```

Six of those link-checker runs are nested inside a `repo_cartographer` root run —
one per mapping, so the orchestrator really did dispatch it every time, which is
the part no local report can establish. The rest are the direct invocations from
the tests and the proof script. **The zero is the phase**: a delegate that sits in
the same trace beside the two that think, briefed the same way, read back the same
way, and spending nothing because there is nothing in it to spend.

**Not crying wolf is half the work.** A guide is markdown a language model wrote,
and it is full of things shaped like paths that are not claims about files: "the
`req`/`res` pair", "sync/async", `Session.request()`, the repository's own name
`psf/requests`, `flask.json` as a module reference. A path is only ever *accused*
if it contains a slash and ends in a recognised file extension. Both conditions
were bought with a specific false positive in mind, each has its own test, and the
price is two named blind spots — an invented directory and an invented root-level
filename go uncaught. A safety net that raises false alarms gets switched off; one
that condemns a correct guide is worse than none.

**And it reports rather than repairs.** When a path is flagged the orchestrator
prefixes a warning naming it and hands the guide over unchanged. Deleting the line
would produce a clean-looking document nobody can verify, and the orchestrator has
never read the repository, so it cannot know what the right path was.

#### This is the first change Phase 5 got to referee

Phase 6 touched two prompts, added a delegate and a step, and gave the doc-writer a
tool it never had. Before the eval set, the only way to ask *did any of that break
the guides?* was to read a few and form an impression. Now:

```console
$ uv run scripts/run_evals.py

requests  5/5 · flask 5/5 · click 5/5 · httpx 5/5 · express 6/6 · chalk 5/5

31 of 31 expected facts present (100%)
```

Against 29 of 31 before the change — so: no regression, which is the claim worth
making. The two facts that moved were `pallets/click`'s `types.py` and `parser.py`,
which nobody had opened last time and someone did this time. That is a coverage
decision the model makes afresh on every run, and Phase 5's own warning applies to
it: **a two-point move on one sample per case is sampling, not causation.** Runs did
get measurably slower — 249s against 214s on `psf/requests` — which is the honest
cost of an extra delegate and a guide emitted twice.

What works right now: an orchestrator that sizes a repository up and splits it
across up to three explorers, explorers that read one directory each and take
notes, and a doc-writer that turns those notes into a guide
with an architecture overview, a where-things-happen table, and an explicit list
of what went unread. Still ahead: skills (Phase 7), approval gates (Phase 8).
Paths in the guide carry three prompts' instruction to cite only verified ones,
one structural guarantee — the doc-writer has no GitHub access, so it cannot
describe a file nobody read — and, since Phase 6, one arithmetic check: every
cited path is matched against the repository's real file tree before the guide is
handed over.

The good-first-issues section the project promises is deliberately absent: nothing
here can read an issue tracker yet, so `DOC_WRITER_PROMPT` forbids inferring one
from the code rather than letting the model invent it.

---

## How it works

```mermaid
flowchart LR
    U([Your question]) --> A[Orchestrator<br/>plans, splits, delegates, checks]
    A <--> M[["LLM<br/>Gemini or OpenRouter"]]
    A --> P[write_todos]
    A --> S[get_repo_scopes<br/>shape only, 1 request]
    A --> K{task}
    K --> E[explorer × up to 3<br/>one per top-level dir<br/>own context window each]
    K --> C[doc-writer<br/>own context window]
    K --> L[link-checker<br/><b>no model — plain Python</b>]
    E --> T{GitHub tools}
    T --> T1[get_repo_tree]
    T --> T2[get_file_contents]
    T --> T3[search_code]
    T1 & T2 & T3 --> G[(GitHub REST API)]
    L --> T1
    E -- write_file --> D[("./workspace<br/>notes/*.md · guide.md")]
    D -- read_file --> C
    C -- write_file guide.md --> D
    D -- reads guide.md --> L
    A -- ls / read_file --> D
    C -- the guide --> A
    L -- citation verdict --> A
    A --> R([Answer])
```

Up to six agents, one workspace. The orchestrator can learn a repository's
*shape* and nothing else — `get_repo_scopes` returns directory names and file
counts in one request, which is enough to divide the work and not enough to
describe the code. Each explorer reads one top-level directory and writes its own
notes file; the doc-writer has no repository access at all and builds the guide
from those notes. Every sub-agent runs in its own message thread, so a file an
explorer reads never enters the orchestrator's context — only the explorer's short
final report does.

Two channels carry work across those boundaries and there are no others: a
sub-agent's final message, and files in the shared workspace. That constraint is
the cost of isolation, and it is why the handoff conventions are stated in all
three prompts rather than assumed.

Each agent loops: the model picks a tool, the tool runs, the result goes back into
that agent's context, repeat until it can answer. The three GitHub tools are plain
Python functions — the library derives each tool's schema and description from its
signature and docstring, so `tools.py` is the single source of truth for what the
model knows about them.

There are two namespaces here and keeping them apart is most of the prompts' job.
GitHub is remote and read-only; the workspace is local, writable, and starts
empty, so `read_file` reaches a repository file only after some agent has put one
there. Every agent gets a deliberately narrow set:

| | GitHub | workspace | other |
|---|---|---|---|
| orchestrator | `get_repo_scopes` only | `ls`, `read_file` | `task`, `write_todos` |
| explorer (×N) | `get_repo_tree`, `get_file_contents`, `search_code` | `read_file`, `write_file` | — |
| doc-writer | — | `ls`, `read_file`, `write_file` | — |
| link-checker | `get_repo_tree`, in Python | reads `/guide.md` directly | **no tools — it has no model to offer them to** |

Two mechanisms produce that table, and they are not interchangeable.
[`middleware.py`](repo_cartographer/middleware.py) hides tools from the
orchestrator per model request — the tool node still holds them, the model is just
never told. Each sub-agent instead restates `FilesystemMiddleware(tools=[...])` in
its own spec, which is stronger: an excluded tool is never constructed, so it
cannot be dispatched at all. Sub-agents need their own because **a parent's
middleware is not inherited by declarative sub-agents** — omit it and both
delegates get `execute`, `glob`, `grep` and `delete` handed straight back.

`tests/test_wiring.py` pins all of it, with no model involved:

```console
$ uv run pytest tests/test_wiring.py -q
16 passed
```

Both halves of the table are deliberate:

- **`write_todos` is added.** It comes from `TodoListMiddleware`, passed
  explicitly — as of deepagents 0.7.3 planning is *not* in the default middleware
  stack. Mapping a repo is several steps deep, so
  [`ORCHESTRATOR_PROMPT`](repo_cartographer/prompts.py) asks for a todo list before
  the first tool call, and the agent writes one.
- **Six built-ins are taken away from the orchestrator.** `create_deep_agent` also
  supplies `glob`, `grep`, `delete` and a shell (`execute`); none has anything to
  do here, and `execute` has no sandbox backend so it only ever returns an error.
  `write_file` and `edit_file` joined that list at Phase 4, when writing moved out
  of the orchestrator — its explorer writes and its doc-writer reads, and it only
  checks. There is a second reason beyond wasted turns: an orchestrator that can
  edit its delegates' notes can quietly launder them.
  This mattered most at Phase 2, when the whole filesystem was hidden: the first
  model tried here spent 8 of 16 tool calls on `read_file("src/foo.py") → not
  found` before the workspace had any purpose, and removing the tools halved what
  a run cost.
- **The default `general-purpose` sub-agent is switched off.** `create_deep_agent`
  adds one to the `task` menu unless told otherwise, advertised to the model as
  having "all tools as the main agent" — which, now that the orchestrator holds
  none, means none. A delegate that can do nothing, described as the one that can
  do anything. A `HarnessProfile` registered against the active model removes it;
  the key is built in [`models.py`](repo_cartographer/models.py) because its shape
  differs per provider, and a key that fails to match doesn't raise — it silently
  leaves the default in place, which is why a test asserts the menu instead of
  trusting the call.

**Offloading.** `FilesystemMiddleware` watches every tool result and writes any
one over `TOOL_RESULT_TOKEN_LIMIT` (2,000 tokens, well below the library's 20,000
default) into the workspace, leaving a head-and-tail preview and a path in the
thread. This is what keeps a 10,000-token file from being re-sent on every
subsequent turn, and it applies to the GitHub tools too — the exclusion list in
the library covers only its own built-ins. On top of that the prompt asks the
agent to keep `/notes.md` as it reads and to answer from the notes rather than
from the sources, so what survives to the final turn is the summary and not the
files.

Worth being precise about what the *backend* buys, since it's easy to overclaim:
files land in `./workspace` rather than in graph state, and that is a durability
change, not a context one — a `StateBackend` keeps file contents out of the
message thread just as well. Disk is what lets you read `workspace/notes.md`
afterwards and see what the agent actually wrote down.

**The tools layer** (`repo_cartographer/tools.py`) is intentionally free of any
LLM or agent code. It's ordinary, testable Python:

| Function | Returns | Notable behaviour |
|---|---|---|
| `get_repo_tree(owner, repo, ref="HEAD")` | Every file path in the repo | Files only — directories and submodules are filtered out, since neither can be read. Raises rather than silently returning a truncated tree. |
| `get_file_contents(owner, repo, path)` | The file as a UTF-8 string | Decodes GitHub's base64 for you. Refuses directories, symlinks, binaries, and files over 1 MB with a message naming the actual problem. |
| `search_code(owner, repo, query)` | Matching results | URL-escapes the query and scopes it to the one repo. No matches is an empty list, not an error. |

Every failure mode raises `GitHubError` (the API said no, or could not be reached
at all) or `ValueError` (the argument pointed somewhere unreadable), so a caller
can tell the two apart. All four go through one helper that retries a dropped or
timed-out connection three times — and only that, never an HTTP answer, since a
404 will not become a 200 and a 403 from the search quota gets worse if you ask
again. Phase 5 is where that turned out to matter; see
[the first thing it caught](#the-first-thing-it-caught-was-not-the-agent).

---

## Requirements

- **Python 3.12 or newer** — `uv` will install it for you if you don't have it.
- **[uv](https://docs.astral.sh/uv/)** for dependency management.
- **A model provider key** — either a
  [Google AI Studio key](https://aistudio.google.com/apikey) (recommended: 500
  requests/day free, no card) or an [OpenRouter key](https://openrouter.ai/keys)
  (50/day free). Both are free; see [Notes on the model](#notes-on-the-model) for
  which to use when.
- **A GitHub personal access token** — read-only on public repos is enough.

---

## Setup

The steps are the same on every OS; only installing `uv` and creating the `.env`
file differ. Pick your platform for those two.

### 1. Install uv

<details open>
<summary><b>Linux / macOS</b></summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or via a package manager: `brew install uv` (macOS), or your distro's package.
</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Or `winget install --id=astral-sh.uv -e`.
</details>

Close and reopen your terminal, then check it worked:

```bash
uv --version
```

### 2. Clone the repository

```bash
git clone https://github.com/ArchitKandu/repo-cartographer.git
cd repo-cartographer
```

### 3. Install dependencies

```bash
uv sync
```

This one command does everything: installs Python 3.12 if it's missing, creates
a `.venv/` in the project, and installs every dependency at the exact versions
pinned in `uv.lock` — so every machine gets an identical environment.

### 4. Add your API keys

Copy the example file, then open `.env` and fill in both values.

<details open>
<summary><b>Linux / macOS</b></summary>

```bash
cp .env.example .env
```
</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
Copy-Item .env.example .env
```
</details>

<details>
<summary><b>Windows (Command Prompt)</b></summary>

```cmd
copy .env.example .env
```
</details>

`.env` should end up looking like this:

```ini
GEMINI_API_KEY=AIza-your-key-here
GITHUB_TOKEN=ghp_your-token-here
```

One model provider is required. Configure both and `LLM_PROVIDER` picks between
them; configure one and it is used automatically.

- **Google AI Studio key:** https://aistudio.google.com/apikey — no card
  required, and the higher free request budget of the two.
- **OpenRouter key (optional):** https://openrouter.ai/keys
- **GitHub token:** https://github.com/settings/tokens — a fine-grained token
  with read-only access to public repositories is sufficient. Get one even
  though it's technically optional: unauthenticated GitHub allows 60
  requests/hour against 5000 with a token, and an agent reading file trees
  burns through 60 almost immediately. Code search rejects anonymous requests
  entirely.

`.env` is gitignored. Don't commit it.

### 5. Verify the setup

```bash
uv run pytest
```

Expect `105 passed, 1 deselected`. These tests hit the real GitHub API with no
mocking, so a pass means your token works and the tools genuinely function. No
model is called anywhere in the suite, so it costs nothing against your provider
quota. A few tests skip themselves rather than fail if GitHub is unreachable or
your hourly quota is spent — an environment problem is not a bug in the code.

---

## Running it

Ask your own question:

```bash
uv run main.py "Explore pallets/flask and explain how routing works."
```

Or run the built-in example ask, which maps `pallets/flask`:

```bash
uv run python -m repo_cartographer.agent
```

`uv run` is the same command on Linux, macOS, and Windows — it uses the project's
environment without you having to activate anything.

<details>
<summary>Prefer to activate the virtual environment manually?</summary>

You don't need to, but if you want `python` and `pytest` available directly:

| OS / shell | Command |
|---|---|
| Linux / macOS | `source .venv/bin/activate` |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| Windows Git Bash | `source .venv/Scripts/activate` |

If PowerShell blocks the script, allow it for your user once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Leave with `deactivate`.
</details>

### Driving it from Python

```python
from repo_cartographer.agent import ask

print(ask("Explore pallets/flask and explain its architecture."))
```

`ask()` is a thin wrapper. For the full message history — every tool call, in
order, which is what you want when you're studying the agent's behaviour rather
than its answer — invoke the graph directly:

```python
from repo_cartographer.agent import agent

result = agent.invoke(
    {"messages": [{"role": "user", "content": "..."}]},
    config={"recursion_limit": 60},
)
for message in result["messages"]:
    for call in getattr(message, "tool_calls", None) or []:
        print(call["name"])

print(result["messages"][-1].text)   # `.text`, not `.content` — see below
```

Read the answer off `.text`, not `.content`. On Gemini, `content` is a list of
typed blocks (the prose plus an encrypted thought signature), so printing it
dumps a repr of that structure; on OpenRouter it happens to be a plain string.
`.text` gives you the prose on both.

Give it a genuinely multi-step question. Asking for one fact wastes the harness —
the interesting behaviour shows up when the agent has to plan, explore, and
decide what to read next.

### Using the tools without the agent

The GitHub layer stands alone, with no LLM and no API key involved:

```python
from repo_cartographer import get_repo_tree, get_file_contents

paths = get_repo_tree("pallets", "flask")
print(len(paths), "files")
print(get_file_contents("pallets", "flask", "pyproject.toml"))
```

---

## Project layout

```
repo-cartographer/
├── repo_cartographer/
│   ├── __init__.py      Package docstring; re-exports the GitHub tools
│   ├── agent.py         build_agent(), build_subagents(), ask() — the wiring
│   ├── prompts.py       The three prompts: orchestrator, explorer, doc-writer
│   ├── middleware.py    Hides the built-ins the orchestrator doesn't use
│   ├── models.py        Provider selection (Google / OpenRouter), .env loading
│   ├── citations.py     Does this cited path exist? — no LLM code
│   ├── link_checker.py  The graph around citations.py — a sub-agent with no model
│   └── tools.py         Four GitHub API functions — no LLM code
├── scripts/
│   ├── measure_context.py   Phase 3's A/B: offloading on vs. off
│   ├── show_contexts.py     Phase 4: per-agent context, via subgraphs=True
│   ├── run_evals.py         Phase 5: six repos in, one score out
│   └── prove_link_checker.py  Phase 6: plant a fake path, watch it get caught
├── tests/
│   ├── test_tools.py    Live tests against the real GitHub API
│   ├── test_wiring.py   The four-way split, asserted without a model
│   ├── test_citations.py  Does the checker catch an invented path? (no model)
│   ├── test_evals.py    Is the eval set itself true? (live GitHub, no model)
│   └── evals/
│       ├── known_repos.jsonl   The dataset: 6 repos, 31 expected facts
│       ├── dataset.py          Loads and validates it, strictly
│       ├── scoring.py          Is this fact in this guide? No model involved
│       └── results/            Recorded runs — git-ignored
├── workspace/           The agent's scratch space — git-ignored, run output
├── main.py              CLI: uv run main.py "your question"
├── .env.example         Template for your keys — copy to .env
├── ARCHITECTURE.md      How the whole system works, start to finish
├── langgraph.json       Graph entry point for LangGraph Studio / Platform
├── IMPLEMENTATION_GUIDE.md   The phased build plan
├── pyproject.toml       Dependencies and tool config
└── uv.lock              Exact pinned versions
```

`prompts.py` exists because Phase 4 needed three prompts instead of one. The
division is that `agent.py` is *wiring* — which model, which tools, which
middleware, in what order — and `prompts.py` is *content*. Prompts and the three
tool docstrings are still nearly the whole specification of behaviour; by Phase 7
the ecosystem-specific parts move out again into `skills/`.

`build_agent()` takes exactly one argument, `tool_result_token_limit`, and that
is deliberate — it is the variable Phase 3 measures. Passing `None` disables
offloading and gives the control arm: same prompt, same tools, same workspace,
but large tool results stay inline. `agent` is the module-level default with
offloading on.

Note that `agent` is deliberately *not* re-exported from `__init__.py`.
Importing it builds an LLM client and needs a provider key immediately, so
re-exporting it would make the bare `import repo_cartographer` fail on any
machine without a configured `.env` — including during test collection. Import
it from its own module: `from repo_cartographer.agent import agent`.

---

## Development

```bash
uv run ruff check .          # lint
uv run ruff check . --fix    # lint and autofix
uv run mypy repo_cartographer/ scripts/   # type check
uv run pytest                # tests (skips the slow one)
uv run pytest -m slow        # the one slow test: a large repo's tree
uv run pytest -v --log-cli-level=INFO   # verbose, with live logs

uv run scripts/measure_context.py               # Phase 3's A/B, 2 model runs
uv run scripts/measure_context.py --repeats 3   # ...with ranges, 6 model runs

uv run scripts/run_evals.py                     # Phase 5's score, 6 model runs
uv run scripts/run_evals.py --case flask        # ...one repo only
uv run scripts/run_evals.py --score-only        # re-score recorded runs, free
uv run scripts/run_evals.py --history           # every run recorded so far

uv run scripts/prove_link_checker.py            # Phase 6's proof, ~2 model requests
```

`measure_context.py` spends real model quota — two runs per `--repeats`, each a
full repository mapping. Point it at a repo with files large enough to cross the
2,000-token eviction threshold or it will correctly report that nothing was
offloaded and the difference is noise.

The test suite calls GitHub for real rather than mocking it, which means it can
be affected by your rate limit. It's built to tell the difference: a spent quota
*skips* with an explanation, while a broken or revoked token *fails*, so an
environment problem never gets mistaken for a bug in the tools. If your quota is
nearly exhausted, run a subset with a lower floor:

```bash
PHASE1_MIN_BUDGET=4 uv run pytest
```

---

## Troubleshooting

**`RuntimeError: No model provider is configured`**
Either `.env` doesn't exist yet (step 4) or both key lines are empty. The check is
deliberate — it fails at startup with a clear message rather than surfacing as a
confusing `401` from deep inside the agent's reasoning loop.

**`400 Function call is missing a thought_signature`**
You're reaching Gemini through an OpenAI-compatible client. Gemini needs its
native one — see [Notes on the model](#notes-on-the-model).

**`ModuleNotFoundError: No module named 'repo_cartographer'`**
You're running a bare `python` instead of `uv run python`, so the project's
environment isn't active. Use `uv run python -m repo_cartographer.agent`, and
run it from the repository root.

**Tests skip with "cannot reach the GitHub API" or "rate limit"**
Your `GITHUB_TOKEN` is missing, expired, or the hourly quota is spent. Limits
reset hourly; `PHASE1_MIN_BUDGET=4 uv run pytest` runs a reduced set meanwhile.

**`429 Rate limit exceeded: free-models-per-day` from OpenRouter**
The free tier's 50 requests/day is spent; it resets at midnight UTC. Ten credits
($10, one time) raise the cap to 1,000/day — see
[Notes on the model](#notes-on-the-model).

**`401` or `402` from OpenRouter**
The key is wrong, or the free endpoint is busy — free models are shared. Set
`OPENROUTER_MODEL` in `.env` to try another.

**PowerShell won't run the activate script**
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, once. Or skip activation
entirely and use `uv run`, which needs no execution-policy change.

---

## Notes on the model

Two providers, because on free tiers no single one is good at both jobs this
project has. Pick with `LLM_PROVIDER`; with only one key configured it is used
automatically.

| | `google` (default) | `openrouter` |
|---|---|---|
| Default model | `gemini-3.5-flash-lite` | `nemotron-3-super-120b-a12b:free` |
| Requests/day | **500** | 50 |
| Requests/minute | 15 | 20 |
| Mapping runs/day | ~50 | ~5 |
| Best for | iterating on the prompt | runs whose output matters |

**Watch Gemini's per-model daily caps.** They are not uniform, and reaching for a
bigger model costs you the ability to run at all:

| Model | RPM | TPM | RPD | Runs/day |
|---|---|---|---|---|
| `gemini-3.6-flash` | 5 | 250K | **20** | ~2 |
| `gemini-3.5-flash` | 5 | 250K | **20** | ~2 |
| **`gemini-3.5-flash-lite`** | 15 | 250K | **500** | **~50** |
| `gemini-3.1-flash-lite` | 15 | 250K | 500 | ~50 |
| `gemma-4-31b` | 30 | **16K** | 14,400 | token-bound |
| `gemini-*-pro` | — | — | 0 | unavailable on free tier |

`gemma-4-31b` is the interesting one: effectively unlimited requests, but 16K
input tokens/minute, which an agent loop exceeds within a few turns because it
resends the whole history each time. Phase 3's offloading helps here and was
partly built for it, though not enough to rescue this model on a real repo: the
measured `psf/requests` run still reached 21K tokens on its final turn with
offloading on. The ceiling moved; it is still below what the task needs.

### Why Gemini needs its native client

Google's OpenAI-compatible endpoint does **not** work for this. Gemini 3 models
think by default, and every function call they emit carries an encrypted
`thought_signature` that a stateless client must send back verbatim on the next
turn. The compatibility layer drops it, so the second turn of any tool-calling
loop dies with:

```
400 Function call is missing a thought_signature in functionCall parts.
```

`langchain-google-genai`'s `ChatGoogleGenerativeAI` round-trips it. OpenRouter
still uses `ChatOpenAI`.

### Why not the smallest model

The project first ran on `nvidia/nemotron-3-nano-30b-a3b:free` (3B active
parameters). Measured on the same `psf/requests` ask, it:

- **never wrote a plan**, even with `write_todos` available and the prompt asking
  for one first — precisely what Phase 2's definition of done turns on;
- spent 8 of 16 tool calls on `read_file`/`ls` against the empty workspace,
  including three identical retries of a path that had already 404'd;
- reached a correct answer, just wastefully.

Multi-step tool use is where small models fall down first, and this task is
nothing but multi-step tool use. The planning failure was fixed by moving to a
capable-enough model. The wasted `read_file` calls were fixed at Phase 2 by hiding
the workspace tools outright; Phase 3 gave them back, so what keeps the model from
repeating that mistake is the prompts drawing the line between a remote read-only
repository and a local workspace that starts empty — a weaker guarantee than
removal, and the reason the distinction is stated twice in both
[`ORCHESTRATOR_PROMPT` and `EXPLORER_PROMPT`](repo_cartographer/prompts.py).

Phase 4 narrows the opening again from a different direction: the only agent that
can reach GitHub now holds just `read_file` and `write_file` on the workspace, and
the only agent that could confuse the two namespaces is the one whose prompt spends
the most words on the distinction. The doc-writer, which has no GitHub tools at
all, cannot make the mistake in either direction.

### If you stay on OpenRouter

Its free tier allows 20 requests/minute and 50/day across all `:free` models
combined — about three runs. A one-time purchase of **10 credits ($10) raises the
daily cap to 1,000**, permanently; the credits are not spent by `:free` models,
so it is a deposit rather than a bill. When the cap is spent you get a `429` with
`limit_source: openrouter_free_tier_daily` and a reset timestamp (midnight UTC).

Avoid `openrouter/free` — it picks a free model at random per request, so no run
is reproducible, which breaks both Phase 2's definition of done and Phase 5's
evals.
