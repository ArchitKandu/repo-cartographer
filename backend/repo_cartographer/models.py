"""The chat models Repo Cartographer reasons with, and the .env loading they need.

Kept apart from `agent.py` so the agent module is only about the mapping task —
prompt, tools, harness — and changing models never means touching it.

Two providers are supported, because on free tiers no single one is best at both
jobs this project has. Select the primary with `LLM_PROVIDER` in .env:

`google` (default when `GEMINI_API_KEY` is set) — Gemini via Google AI Studio.
    For iterating. `gemini-3.5-flash-lite` allows 500 requests/day at 15/minute
    with a 250K input-tokens/minute ceiling, which is roughly 28 mapping runs a
    day — enough to work on a prompt. Flash-Lite is the weakest tier Google
    serves, so treat its planning behaviour as something to verify, not assume.

`openrouter` — free open-weight models, `nemotron-3-super-120b-a12b` by default.
    For runs whose output matters. A much stronger model, but the free tier
    allows 50 requests/day *across every free model*, so it is the wrong place to
    debug a prompt — and the wrong place to put an agent that makes fifteen
    requests a run.

Switching is a .env edit, not a code change. Google gets its native client rather
than the OpenAI-compatible endpoint, which is not optional: Gemini 3 models think
by default, and every function call they emit carries an encrypted
`thought_signature` that a stateless client MUST send back verbatim on the next
turn. The compatibility layer drops it, and the second turn of any tool-calling
loop fails with `Function call is missing a thought_signature in functionCall
parts`. `ChatGoogleGenerativeAI` round-trips it.

Beware the per-model daily caps on Google's free tier. The capable Flash models
(3.6, 3.5, 3, 2.5) allow only 20 requests/day each — one mapping run — while the
Flash-Lite models allow 500. Reaching for a bigger Gemini therefore costs you the
ability to run at all; that is why the default here is deliberately a Lite model.

## One model became two, and the reason is arithmetic rather than quality

The limit that actually stops a run is **requests per minute**, and a request is
spent per model turn. Until now every agent shared one model, and therefore one
budget: fifteen a minute for the whole fan-out, which is the ceiling Phase 4b hit
and the rate limiter below exists to pace.

The rate limits are published **per model**, not per project — that is what
Google's own usage dashboard reports, one row and one set of RPM/TPM/RPD figures
per model name. So a second model is a second budget, and moving one agent onto
it is capacity bought without a single request saved.

`model_for(role)` is how an agent gets its model. There are two:

- **The primary**, from `LLM_PROVIDER`. It carries the orchestrator and the
  explorers, and both are there for a reason rather than by default. The
  orchestrator has to emit several `task` calls in one message for the fan-out to
  be a fan-out, has to open each brief with a line `briefing.py` can parse, and
  has to relay the doc-writer's guide *verbatim* — three things weaker models are
  specifically bad at, and all three fail silently. The explorer decides which
  files to open and writes the notes every downstream claim rests on, and it
  makes more requests than the other two combined.
- **The assist**, on the *other* provider, when a key for it exists. It carries
  the doc-writer.

## Why the doc-writer is the one that moves

Not because its output matters least — it produces the deliverable. Because it is
the only agent whose failure mode this system already guards twice.

Phase 4 took away its repository access entirely (`tools=[]`), so it cannot cite
a file nobody read; it can only write prose about files the notes name. Phase 6
then put a check with no model in it between that prose and the answer, and the
check is arithmetic: every path it cites is compared against the real tree. A
weaker model in that seat writes worse sentences. It does not get to invent a
file, and if it tries, the link-checker says so.

Compare the alternative. A weaker orchestrator stops emitting parallel tool
calls, or writes a brief the prefetch cannot read, or quietly summarises the
guide instead of relaying it — three regressions with no guard anywhere, none of
which raises, and all of which read as a normal run.

It is also the right size. The doc-writer spends about five requests of a run's
thirty, which fits OpenRouter's 50-a-day free ceiling at roughly ten runs, while
the fifteen an explorer spends would fit three.

Two things to know before pushing more of the system onto free models, since the
temptation is obvious once the mechanism exists. `ASSIST_ROLES` will move any
role you name, including all of them. And `openrouter/free` is a real model id
that picks a free model at *random per request*, which this project does not use
by default and should not: no run would be reproducible, which is fatal to Phase
2's definition of done and to Phase 5's evals, both of which compare runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

# Load environment variables from .env file. The path is anchored to this file
# rather than the working directory, so the keys resolve whichever directory the
# agent is started from — including from a REPL or `python -c`, where dotenv's
# own find_dotenv() falls back to the cwd.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# The three roles that hold a model. `link-checker` is deliberately absent: it is
# a graph over plain functions, and Phase 6's whole point is that it has no model
# to give one to. Named here rather than as strings in `agent.py` so that a
# misspelled role in `ASSIST_ROLES` can be reported instead of silently ignored.
ORCHESTRATOR = "orchestrator"
EXPLORER = "explorer"
DOC_WRITER = "doc-writer"
ROLES = (ORCHESTRATOR, EXPLORER, DOC_WRITER)

# Which roles go on the assist model. See the module docstring on why this one
# and not the others. Override with `ASSIST_ROLES` — a comma-separated list, or
# empty to put every agent back on one model.
_DEFAULT_ASSIST_ROLES = frozenset({DOC_WRITER})

# Requests per minute each free tier allows, and the reason a rate limiter exists
# here at all. Phase 4b dispatches up to three explorers concurrently, and three
# agents stepping through their own tool loops at once reach a per-minute ceiling
# in seconds: the first fan-out run attempted here died mid-explorer on
# `429 RESOURCE_EXHAUSTED ... limit: 15`, with the API itself suggesting a retry
# one second later.
#
# The lesson is worth stating plainly, because it is the real cost of the fan-out:
# on a request-per-minute budget, concurrency does not buy wall-clock speed. Three
# explorers still finish no faster than the bucket refills. What parallelism buys
# is the thing Phase 4 is actually about — three separate context windows — and the
# limiter is what makes that survivable rather than a burst of 429s.
_REQUESTS_PER_MINUTE = {"google": 15, "openrouter": 20}

# Spend the budget at 80% of the ceiling. The remaining fifth is for the fact that
# a limiter meters *our* requests, not the provider's accounting of them: retries
# inside the SDK, and clock skew between the bucket and the server's window, both
# land on the wrong side of an exactly-tuned rate.
_RATE_LIMIT_HEADROOM = 0.8

# How many times the client may re-send a request the provider rejected.
#
# Two, and explicitly, because the default is six and every one of those six is a
# real request that the provider counts — against the per-minute limit that
# caused the rejection, and against the per-day limit that has nothing to do with
# it. That is how a single bad minute takes a chunk out of a whole day: the
# limiter below meters what *we* send, and a retry storm inside the SDK is spend
# it never sees. Google's usage dashboard showed 17 requests in a minute against
# a limiter set to 12, and this is where the other five came from.
#
# Two rather than zero because one 429 is not the same event as a dead endpoint,
# and the API's own advice on the failure that started all of this was "retry in
# 1.010311967s" — which one retry satisfies and six do not improve on.
_MAX_RETRIES = int(os.environ.get("MODEL_MAX_RETRIES") or 2)

# 500 requests/day at 15/minute — the most headroom Google's free tier offers.
_DEFAULT_GOOGLE_MODEL = "gemini-3.5-flash-lite"

# The other Flash-Lite, for when Google is the *assist* provider rather than the
# primary. A different model name is the entire point — see the module docstring
# on limits being published per model — so this deliberately does not repeat
# `_DEFAULT_GOOGLE_MODEL`, which the primary would already be spending.
_DEFAULT_GOOGLE_ASSIST_MODEL = "gemini-3.1-flash-lite"

# 120B total / 12B active, 262K context. NVIDIA positions it for multi-agent
# work. Free alternatives, in descending order of capability on this task:
#   nvidia/nemotron-3-ultra-550b-a55b:free   550B/55B active, 1M context.
#                                            Strongest and slowest.
#   inclusionai/ling-3.0-flash:free          124B/5.1B active. Token-efficient
#                                            agentic loops; try if Super is slow.
#   openai/gpt-oss-20b:free                  21B/3.6B active. Well-trodden with
#                                            LangChain, but small for this.
# Not `openrouter/free`: it picks a free model at random per request, so no run
# is reproducible — fatal for Phase 2's definition of done and Phase 5's evals.
_DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

_NO_KEYS = (
    "No model provider is configured. Set GEMINI_API_KEY (get one at "
    "https://aistudio.google.com/apikey) or OPENROUTER_API_KEY (at "
    "https://openrouter.ai/keys) in .env at the repo root."
)


class ModelChoice(NamedTuple):
    """A provider and a model name, resolved from the environment.

    Separated from the chat model it builds so that *which* model an agent gets
    can be decided, compared and reported without constructing anything — which
    is what lets `tests/test_models.py` assert the whole routing policy with no
    key configured at all.
    """

    provider: str
    name: str

    @property
    def profile_key(self) -> str:
        """The key this model resolves under in deepagents' profile registry.

        The shape differs per provider, and both halves are load-bearing.
        `google_genai` is the provider name langchain reports for
        `ChatGoogleGenerativeAI`, and Gemini model names contain no colon, so the
        joined form is what deepagents looks up first.

        OpenRouter model names carry their own colon (`vendor/model:free`) and
        deepagents refuses any key with more than one — so the joined form would
        silently fall through to the provider-wide `openai` registration, which is
        far too broad. The bare identifier splits cleanly and matches on its own.
        """
        return f"google_genai:{self.name}" if self.provider == "google" else self.name


def _provider_of(model_name: str) -> str:
    """Which provider serves a model, from the shape of its name.

    A slash means an OpenRouter identifier (`vendor/model:free`); Gemini names
    have none. Inferring rather than asking for a second environment variable
    keeps `ASSIST_MODEL=gemini-3.1-flash-lite` and
    `ASSIST_MODEL=inclusionai/ling-3.0-flash:free` both working with no further
    configuration, and the two namespaces cannot collide.
    """
    return "openrouter" if "/" in model_name else "google"


def _key_for(provider: str) -> str | None:
    return os.environ.get("GEMINI_API_KEY" if provider == "google" else "OPENROUTER_API_KEY")


def _resolve_primary() -> ModelChoice:
    """The model the orchestrator and explorers use.

    Keys are checked here rather than left to the first request: without one both
    providers answer 401, which surfaces deep inside the agent loop as an opaque
    failure rather than a missing-configuration message.
    """
    google_key = _key_for("google")
    openrouter_key = _key_for("openrouter")

    if not google_key and not openrouter_key:
        raise RuntimeError(_NO_KEYS)

    # Defaulting to whichever key is present keeps a fresh clone runnable with
    # only one of the two configured. An explicit LLM_PROVIDER always wins, and is
    # how you choose when both keys exist.
    requested = os.environ.get("LLM_PROVIDER") or ("google" if google_key else "openrouter")
    provider = requested.strip().lower()

    if provider == "google":
        if not google_key:
            raise RuntimeError(
                "LLM_PROVIDER=google but GEMINI_API_KEY is not set. Get a key at "
                "https://aistudio.google.com/apikey, or set LLM_PROVIDER=openrouter."
            )
        return ModelChoice("google", os.environ.get("GOOGLE_MODEL", _DEFAULT_GOOGLE_MODEL))

    if provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set. Get a "
                "key at https://openrouter.ai/keys, or set LLM_PROVIDER=google."
            )
        return ModelChoice(
            "openrouter", os.environ.get("OPENROUTER_MODEL", _DEFAULT_OPENROUTER_MODEL)
        )

    raise RuntimeError(
        f"LLM_PROVIDER={provider!r} is not recognised — use 'google' or 'openrouter'."
    )


def _resolve_assist(primary: ModelChoice) -> ModelChoice | None:
    """A second model on a second budget, or None if there is only one.

    None is the common case and a supported one: a clone with a single key runs
    exactly as it did before this existed, every agent on the primary. The split
    is capacity, not correctness, and nothing depends on having it.

    An explicit `ASSIST_MODEL` is honoured whatever provider it names, including
    the primary's — two models from one provider still hold two budgets, since
    the limits are published per model. Without one, the assist is the *other*
    provider's default, and only if its key is present: silently falling back to
    a second model on the primary's own account would double the spend against
    the very ceiling this is meant to relieve.
    """
    if configured := os.environ.get("ASSIST_MODEL", "").strip():
        choice = ModelChoice(_provider_of(configured), configured)
        if not _key_for(choice.provider):
            raise RuntimeError(
                f"ASSIST_MODEL={configured!r} needs a key for {choice.provider!r}, "
                "which is not set. Unset ASSIST_MODEL to put every agent on the "
                "primary model, or set ASSIST_ROLES= (empty) to the same effect."
            )
        return None if choice == primary else choice

    other = "openrouter" if primary.provider == "google" else "google"
    if not _key_for(other):
        return None
    return ModelChoice(
        other,
        _DEFAULT_OPENROUTER_MODEL if other == "openrouter" else _DEFAULT_GOOGLE_ASSIST_MODEL,
    )


def _assist_roles() -> frozenset[str]:
    """Which roles run on the assist model.

    An unrecognised name raises rather than being ignored. `ASSIST_ROLES=docwriter`
    would otherwise be a configuration change that appears to work, costs nothing
    to make, and does nothing at all — and the only way to notice would be to read
    a trace and count requests per provider.
    """
    configured = os.environ.get("ASSIST_ROLES")
    if configured is None:
        return _DEFAULT_ASSIST_ROLES

    named = {part.strip().lower() for part in configured.split(",") if part.strip()}
    if unknown := named - set(ROLES):
        raise RuntimeError(
            f"ASSIST_ROLES names {sorted(unknown)}, which are not roles. "
            f"Choose from {list(ROLES)}, or leave it empty for one model."
        )
    return frozenset(named)


PRIMARY = _resolve_primary()
"""The model the quality-critical agents use. See the module docstring."""

ASSIST = _resolve_assist(PRIMARY)
"""A second model on a second budget, or None when only one provider is configured."""

ASSIST_ROLES = _assist_roles()
"""The roles routed to `ASSIST` — empty, or every role, is equally valid."""

_LIMITERS: dict[ModelChoice, InMemoryRateLimiter] = {}
_MODELS: dict[ModelChoice, BaseChatModel] = {}


def _rate_limiter(choice: ModelChoice) -> InMemoryRateLimiter:
    """One shared request budget per model, built once and reused.

    Keyed by the model rather than by the agent, which is what makes the
    accounting right in both directions. Two agents on one model draw from one
    bucket, because the provider counts their requests together — three private
    limiters set to the same rate would together spend three times the budget,
    which is the bug this replaced. Two agents on *different* models get
    different buckets, because the published limits are per model, and making
    them share would throttle a budget nothing was spending.

    Override the rate with `REQUESTS_PER_MINUTE` when you are on a paid tier and
    the ceiling is not 15. It applies to every bucket, deliberately: a per-model
    override is a knob nobody has needed, and the shape of this dictionary is
    where it would go if they did.
    """
    if choice in _LIMITERS:
        return _LIMITERS[choice]

    configured = os.environ.get("REQUESTS_PER_MINUTE")
    per_minute = float(configured) if configured else _REQUESTS_PER_MINUTE[choice.provider]
    _LIMITERS[choice] = InMemoryRateLimiter(
        requests_per_second=per_minute * _RATE_LIMIT_HEADROOM / 60,
        # How often a waiting caller re-checks the bucket. Well below the interval
        # between grants, so a freed slot is taken promptly rather than adding a
        # second of latency to every request.
        check_every_n_seconds=0.1,
        # No bursting. A bucket that banks unused capacity would let three explorers
        # start simultaneously and spend the whole minute's budget at once, which is
        # precisely the failure this exists to prevent.
        max_bucket_size=1,
    )
    return _LIMITERS[choice]


def _build(choice: ModelChoice) -> BaseChatModel:
    """Construct the chat model for a choice, once per process.

    Memoised so that two roles on the same model share one client and therefore
    one rate limiter — the caching is the accounting, not an optimisation.
    """
    if choice in _MODELS:
        return _MODELS[choice]

    key = _key_for(choice.provider)
    if not key:
        raise RuntimeError(_NO_KEYS)

    if choice.provider == "google":
        built: BaseChatModel = ChatGoogleGenerativeAI(
            model=choice.name,
            api_key=SecretStr(key),
            rate_limiter=_rate_limiter(choice),
            max_retries=_MAX_RETRIES,
        )
    else:
        built = ChatOpenAI(
            model=choice.name,
            base_url=_OPENROUTER_BASE_URL,
            api_key=SecretStr(key),
            rate_limiter=_rate_limiter(choice),
            max_retries=_MAX_RETRIES,
        )

    _MODELS[choice] = built
    return built


def choice_for(role: str) -> ModelChoice:
    """Which model a role runs on, without building it.

    The routing policy in one function, so `scripts/` and the tests can report and
    assert it without a key or a network call.
    """
    if role not in ROLES:
        raise RuntimeError(f"{role!r} is not a role. Choose from {list(ROLES)}.")
    if ASSIST is not None and role in ASSIST_ROLES:
        return ASSIST
    return PRIMARY


def model_for(role: str) -> BaseChatModel:
    """The chat model for one of the three roles that hold one."""
    return _build(choice_for(role))


def profile_keys_in_use() -> list[str]:
    """Every distinct harness-profile key this run's models resolve under.

    One key when there is a single model, two when the doc-writer is routed
    elsewhere. `agent.py` registers its profile against all of them, and the
    reason is a warning that turned up the moment a second model appeared:

        No harness profile matched pre-built model ChatOpenAI
        (identifier='nvidia/...:free', provider='openai'); using defaults.

    deepagents resolves a profile per model, so registering only the
    orchestrator's key leaves the assist model unmatched. Nothing breaks — the
    profile this project registers only switches off a default sub-agent, and the
    doc-writer has no `task` tool to offer it — but a warning that is noise today
    is a warning nobody reads tomorrow, and the *other* thing this registry
    controls is which built-in tools an agent is handed.
    """
    return list(dict.fromkeys(choice_for(role).profile_key for role in ROLES))


model = model_for(ORCHESTRATOR)
"""The primary chat model, kept under its original name.

`agent.py` passes this to `create_deep_agent` as the main agent's model, and the
sub-agent specs name their own. Still exported as `model` because that is what a
main agent's model is, and because every script and test that imported it was
asking for exactly this.
"""

MODEL_PROFILE_KEY = choice_for(ORCHESTRATOR).profile_key
"""The key `model` resolves under in deepagents' harness-profile registry.

Published from here because this module is the only place that knows which of the
two providers was actually built, and the key's *shape* differs between them —
see `ModelChoice.profile_key`. `agent.py` uses it to switch off a default
sub-agent it has no use for; a key that fails to match doesn't raise, it just
silently leaves the default in place, which is why the shape is worth a comment
rather than a guess.

It is the *orchestrator's* key specifically. The profile is registered against
the main agent's model, so routing the doc-writer elsewhere must not move it.
"""
