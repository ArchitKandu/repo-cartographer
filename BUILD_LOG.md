# Build log — one phase, one concept, one measurement

Repo Cartographer was built in nine phases, and the rule behind every one of them
was **change one variable at a time**. Each phase adds a single mechanism, states
in advance what would count as evidence it worked, and then produces that
evidence — a number, a trace, a test that fails without it.

That constraint is why this file exists separately from the README. The story of
*how* a thing was built is not the same as the story of *what it is*, and mixing
them means a reader who wants to run the project has to read a laboratory
notebook first. If you want to understand the system, read
[ARCHITECTURE.md](ARCHITECTURE.md). If you want to know what each piece is
*for* — and what happened when it was added — you are in the right place.

The plan the phases follow, and the reasoning behind their ordering, is
[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md).

| Phase | Concept it isolates | Evidence it produced |
|---|---|---|
| 1 | A deterministic, testable foundation | 26 live tests against the real GitHub API, zero mocking |
| 2 | What an agent harness buys over a bare loop | It planned before exploring, unprompted |
| 3 | Context offloading | Final-turn context 37.8k → 21.4k tokens |
| 4a | Context quarantine | Four threads, each with its own context window |
| 4b | Parallel fan-out | Two explorers overlapping for 35.5s, confirmed in the trace |
| 5 | Measurable regressions | 6 repositories, 37 expected facts, one score |
| 6 | Sub-agents are not smaller model calls | A delegate that costs **0 tokens** and cannot be argued with |
| 7 | Prompt decomposition | Two ecosystems, two skills, each read only when it matched |
| 8 | Approval gates | Execution pauses; reject and approve provably differ |
| 9 | Packaging | Not started |

Three habits recur below and are worth naming up front, because they are the
reason the numbers here can be trusted:

- **A measurement that cannot fail proves nothing.** Every definition of done is
  written so that a broken implementation produces a different result.
- **One run is not a result.** The model chooses which files to open, and that
  choice moves between runs of an unchanged system. Where a claim rests on a
  single run, it says so.
- **A number that fell because you stopped measuring is worse than no number.**
  This happened here, at Phase 4, and the instrument that produced it now
  carries the warning in its own docstring.

---

## Phase 1: a layer that can be checked without a model

The GitHub tools are four ordinary functions over the REST API with no agent code
in them at all, and `tests/test_tools.py` calls them against real repositories
with **zero mocking**. That is the whole point of the phase: an LLM's behaviour is
probabilistic and slow to check, a Python function's is neither. A bug in
`get_repo_tree` would otherwise be inherited by every phase after it, wrapped in
enough agent reasoning to look like a prompt problem.

The tests distinguish an environment failure from a defect, deliberately: a spent
GitHub quota *skips* with an explanation, while a rejected token *fails*. A suite
that goes red for reasons nobody can act on teaches you to ignore red.

## Phase 2: what the harness buys over a bare loop

The definition of done — *"in the transcript, it calls the built-in planning tool
before calling any of yours, unprompted"* — is met. On `psf/requests` with
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

## Phase 3: context offloading

The definition of done is a number: run the same task with and without
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

## Phase 4: one thread became four, and two of them ran at once

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

### The fan-out has a hard cost, and it is not tokens

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

## Phase 5: a number instead of an impression

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

### The first thing it caught was not the agent

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

## Phase 6: the delegate with no model in it

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

### This is the first change Phase 5 got to referee

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

## Phase 7: the instructions moved out of the code

By Phase 6 the prompts were carrying four jobs at once — how to explore any
repository, what Python layouts look like, what Node layouts look like, and what
this project's guides read like. `EXPLORER_PROMPT` named `pyproject.toml` and
`package.json` and `index.*` in a single breath, and every run paid for all of it
regardless of what the repository was written in.

Phase 7 splits those apart into files a person can edit without opening Python:

| File | What it carries | Loaded |
|---|---|---|
| `skills/python-repo/SKILL.md` | which files answer which questions in a Python repo | **only when the explorer decides it matches** |
| `skills/node-repo/SKILL.md` | the same for JavaScript and TypeScript | same |
| `AGENTS.md` | this project's house style for a guide | always, into the doc-writer |

That difference is the design. A skill is *conditional* — the explorer sees one
line describing each and reads the full file only if the repository matches — so
a Node run never pays for the Python conventions. `AGENTS.md` is
*unconditional*, because "a table row naming a directory has told nobody
anything" is true of every repository there is.

**The definition of done is two runs, back to back, with no code change between
them:**

```console
$ uv run scripts/show_skills.py

repository            expected skill  skills actually read
------------------------------------------------------------------------
psf/requests          python-repo     python-repo
chalk/chalk           node-repo       node-repo
------------------------------------------------------------------------

psf/requests — what reached the guide (python-repo markers):
  [x] packaging manifest   [x] layout   [x] how tests run

chalk/chalk — what reached the guide (node-repo markers):
  [x] package.json   [x] entry point field   [x] scripts

PASS — each run read exactly the skill for its own ecosystem and never the other one.
```

The first column is the hard evidence and it needs no interpretation: the
explorers' `read_file` calls say which SKILL.md was opened, and — more usefully —
which was not. A run that read both would have demonstrated thoroughness, not
selection. The markers underneath are keyword checks, corroboration rather than
proof, and the script says so.

You can see the node skill's fingerprint in the guide it produced for
`chalk/chalk`:

> Chalk relies on vendored dependencies for ANSI definitions and color support
> detection, which are located in `source/vendor/` and accessed via Node.js
> **subpath imports defined in `package.json`**.

Nothing in any prompt mentions subpath imports or vendored directories.
`skills/node-repo/SKILL.md` does — it warns that a vendored directory can be
load-bearing rather than skippable, and that `package.json` names the entry
points no directory listing reveals.

**The split is real because something was removed.** The ecosystem specifics are
gone from `prompts.py`, not duplicated into the skills. `AGENTS.md` is
deliberately additive and says so in its own text: the four sections a guide must
have, and the rules that keep it truthful, stay in `DOC_WRITER_PROMPT`, because
those are the job rather than the styling.

**The skills mount is read-only, and that is not fastidiousness.** `./skills`
lives inside this git repository, so an explorer holding `write_file` and a plain
mount could write through it into the project's own source — including into the
instructions it is currently following. `ReadOnlyBackend` makes that impossible
rather than forbidden, which is the same argument the doc-writer's empty tool
list makes.

**Phase 5's instrument had to grow to keep up.** `run_evals.py` stamps every
recorded run with a fingerprint of the instructions that produced it, so
re-scoring after an edit warns you the guides are stale. That fingerprint hashed
the three prompts — which was the whole set until now. `AGENTS.md` and the
skills are instructions too, and they are the ones most likely to be edited,
precisely because editing them needs no Python. A fingerprint blind to them would
have let the cheapest kind of change alter the output while every recorded score
kept reading as current. It now covers all of them, and a test moves a byte in a
`SKILL.md` to prove the digest follows.

Two caveats worth stating. The run above used `gemini-3.1-flash-lite` rather than
the usual `gemini-3.5-flash-lite`, because the latter's daily quota was spent —
so it is not directly comparable to the token tables above, and the script prints
the model for that reason. And skill *selection* is a model decision, so `PASS`
is a result, not a guarantee; the script reports `INCONCLUSIVE` rather than
failure when no skill is read, because that is a prompt-adherence finding worth
recording.

**The scored before/after is half-done, and the missing half is quota, not
work.** The eval set gained one ecosystem fact per case — the packaging manifest
each skill tells the explorer to record by name — and re-scoring the recorded
Phase 6 guides against it costs nothing: **36 of 37**. The pre-skill guides
already named the manifest in five of six cases, so this particular fact is a
weak discriminator, which is worth saying rather than hiding. The matching
after-sweep wants the same model as the before-runs to be a controlled
comparison, and that model's daily quota is spent; it is one command
(`uv run scripts/run_evals.py`) once it resets.

## Phase 8: the one thing it can do that cannot be undone

Everything through Phase 7 reads. `tools.py` is four GET requests, `citations.py`
compares strings, the workspace is a scratch directory nobody else can see. The
worst outcome of a bad run has been a wrong sentence in a guide, and the fix for
a wrong sentence is to run it again.

`open_pull_request` breaks that. It writes a branch, a file and a draft pull
request into a repository that is probably not yours, notifies its maintainers,
and closing it does not unsend the notification. There is no version of "run it
again" that helps — which makes it the right thing to build a gate around, and
the reason the gate is the phase rather than the feature.

**The definition of done is explicitly not "the parameter is set":**

```console
$ uv run scripts/prove_approval_gate.py

1. running until the gate …
   PAUSED. The graph stopped before the tool ran.
   │ {'action_requests': [{'name': 'open_pull_request',
   │   'args': {'repo': 'chalk', 'title': 'docs: add an onboarding guide', 'owner': 'chalk'}}],
   │  'review_configs': [{'allowed_decisions': ['approve', 'edit', 'reject', 'respond']}]}

   open_pull_request results so far: 0 — nothing has executed.

2. resuming with a REJECTION …
   │ [status=error]
   │ Not this time.

3. replaying the same run and APPROVING …
   │ [status=success]
   │ Refused: opening pull requests is switched off. …

   tool body ran on rejection: False   on approval: True

PASS — execution really pauses, and the two answers really differ.
```

The last line before the verdict is the one that matters. A gate that pauses and
then behaves identically whatever you answer is theatre; the test is that the two
branches *differ*. Rejecting produced a synthetic tool message the tool never
wrote (`status=error`), and the call never ran. Approving released it into the
function body — whose own first act was to refuse.

**Two independent guards, because they stop different things:**

| | Stops | Fails open when |
|---|---|---|
| `interrupt_on={"open_pull_request": True}` | the **agent** acting without a human | nobody is present to answer — it pauses instead |
| `ALLOW_PULL_REQUESTS` | the **deployment** acting at all | somebody deliberately sets it |

The second one is not belt-and-braces. An approval gate nobody has exercised is a
gate nobody has tested, and the obvious way to test this one — approve it and see
— would mean opening a real pull request on someone else's repository to prove
that a safety feature works. With the env guard, the approve path runs all the
way into the tool and GitHub is never called. That is why step 3 above is a real
demonstration rather than a claim, and why the script *refuses to start* if
`ALLOW_PULL_REQUESTS` is set.

**The gate is narrow on purpose.** Exactly one tool is listed, and a test asserts
the set has exactly one member. Gate every tool and whoever answers learns to
approve without reading — and then the one call that mattered gets waved through
with the rest.

**What the gate cannot enforce, the prompt has to.** `interrupt_on` stops a call;
it cannot stop the model deciding to make one on every run, and it cannot stop it
rephrasing a rejected call and trying again. Both would respect the gate and
defeat it. So `ORCHESTRATOR_PROMPT` gained three non-negotiable rules — only when
the user asked, only after the citations were checked, and never retry a refusal
— and `tests/test_approval.py` asserts they are still in there.

**A checkpointer arrived with it**, because an interrupt without one is not a
pause but a crash: `interrupt()` needs somewhere to write the run's state. That
made `thread_id` mandatory on every invocation, which is exactly the requirement
that gets forgotten in the fourth script rather than the first — hence one
`run_config()` rather than four literals. It also introduced a trap worth naming:
**a paused run returns normally.** Every caller that reaches for `messages[-1]`
gets the assistant's tool call and reads it as prose. Nothing raises. `ask()` now
checks for `__interrupt__` and says "PAUSED — waiting for your approval" instead
of handing back half an answer.

Two caveats. This ran on `gemini-3.1-flash-lite`, the default model's daily quota
being spent, and the script prints the model because *whether the agent asks for a
pull request at all* is a model decision. And no run in this repository has ever
executed `open_pull_request`'s network half — that is a deliberate gap, and the
honest way to close it is a throwaway repository you own, not a test suite that
writes to other people's.

---

## After Phase 8: the requests nobody was reading code with

Not a phase — no new mechanism, no new capability, and it is recorded here rather
than in the table above because it has **no measurement yet.** That is stated
first on purpose, since the rest of this file is measurements.

The free tier's binding limit turned out not to be tokens per day but **requests
per minute**: fifteen, shared by every agent in a run, which is the ceiling
Phase 4b's fan-out ran into and the rate limiter exists to pace. A request is
spent per model turn, so on that budget the unit of waste is a *turn*, and the
cheapest turn is the one that never happens.

Reading Phase 4's own trace with that in mind, three of its lines stand out:

```
get_repo_tree → filter to src/    get_repo_tree → filter to root
read_file /skills/python-repo/    read_file /skills/python-repo/
get_file_contents __init__.py     get_file_contents pyproject.toml
get_file_contents sessions.py     get_file_contents setup.py
...
```

Four requests went on the first two rows before either explorer had read a line
of the repository — and neither row is a question a model is needed for. *Which
files are in this scope* is a filter over a list. *Which skill matches* is
`"pyproject.toml" in paths`. That is Phase 6's argument arriving somewhere new:
the link-checker beat an LLM at checking citations because checking a citation is
arithmetic, and these two are arithmetic as well. The only reason they were model
decisions is that the model was already there.

So `briefing.py` answers both in Python before an explorer starts and splices the
answers into its prompt, and `EXPLORER_PROMPT` now asks for the file reads in
batches — the explorer picks its whole first batch before reading any of it, so
those reads cannot depend on each other and four of them in one message tell it
exactly what four messages would.

**What is claimed, and what is not.** The prefetch is arithmetic: four requests
per two-explorer run, and one shared GitHub tree fetch instead of one per
explorer. The batching is not — it is a model decision, in exactly the way the
fan-out in Phase 4b is a model decision, and a prompt asking for it is not
evidence that it happened.

**How to close that.** `scripts/show_contexts.py` already prints turns per agent
and tool calls per turn, and turns *are* requests. So the number this needs is
one the instrument already reports, on a run nobody has made yet:

```
uv run scripts/show_contexts.py
```

Until then this section claims a design, not a result. Two things it should be
checked against when the run happens, because both would be invisible in an
answer that still reads well: whether the injected file list pushed the
explorer's own context up more than the two saved turns pulled it down, and
whether an explorer told to batch reads four files it chose in advance rather
than the four it would have chosen by following imports. The first is a token
figure `show_contexts.py` prints. The second is what `run_evals.py` is for.

---

## Where to go next

- **[README.md](README.md)** — what the project is, and how to run it.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the system works, in depth: the
  agents, the files, one real run traced end to end, and the fifteen decisions
  that are the architecture.
- **[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)** — the plan these phases
  follow, and why they are ordered the way they are.
