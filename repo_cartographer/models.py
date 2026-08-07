"""The chat model Repo Cartographer reasons with, and the .env loading it needs.

Kept apart from `agent.py` so the agent module is only about the mapping task —
prompt, tools, harness — and changing models never means touching it.

Two providers are supported, because on free tiers no single one is best at both
jobs this project has. Select with `LLM_PROVIDER` in .env:

`google` (default when `GEMINI_API_KEY` is set) — Gemini via Google AI Studio.
    For iterating. `gemini-3.5-flash-lite` allows 500 requests/day at 15/minute
    with a 250K input-tokens/minute ceiling, which is roughly 28 mapping runs a
    day — enough to work on a prompt. Flash-Lite is the weakest tier Google
    serves, so treat its planning behaviour as something to verify, not assume.

`openrouter` — free open-weight models, `nemotron-3-super-120b-a12b` by default.
    For runs whose output matters. A much stronger model, but the free tier
    allows 50 requests/day total — about 3 runs — so it is the wrong place to
    debug a prompt.

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
"""

import os
from pathlib import Path

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

# 500 requests/day at 15/minute — the most headroom Google's free tier offers.
_DEFAULT_GOOGLE_MODEL = "gemini-3.5-flash-lite"

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


def _rate_limiter(provider: str) -> InMemoryRateLimiter:
    """A shared request budget for every agent in a run.

    One limiter instance is attached to the one chat model, and sub-agents inherit
    that instance rather than building their own — so the orchestrator and all
    three explorers draw from a single bucket. That is what makes the accounting
    correct: the provider's limit is per project, not per agent, so three private
    limiters set to the same rate would together spend three times the budget.

    Override the rate with `REQUESTS_PER_MINUTE` when you are on a paid tier and the
    ceiling is not 15.
    """
    configured = os.environ.get("REQUESTS_PER_MINUTE")
    per_minute = float(configured) if configured else _REQUESTS_PER_MINUTE[provider]
    return InMemoryRateLimiter(
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


def _build_model() -> tuple[BaseChatModel, str]:
    """Construct the chat model named by LLM_PROVIDER, and the key it resolves under.

    Keys are checked here rather than left to the first request: without one both
    providers answer 401, which surfaces deep inside the agent loop as an opaque
    failure rather than a missing-configuration message.

    The second return value is a deepagents harness-profile key — see
    `MODEL_PROFILE_KEY` below for what it is for and why it is computed here.
    """
    google_key = os.environ.get("GEMINI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")

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
        google_model = os.environ.get("GOOGLE_MODEL", _DEFAULT_GOOGLE_MODEL)
        return (
            ChatGoogleGenerativeAI(
                model=google_model,
                api_key=SecretStr(google_key),
                rate_limiter=_rate_limiter("google"),
            ),
            # `google_genai` is the provider name langchain reports for this class,
            # and Gemini model names contain no colon, so the joined form is what
            # deepagents looks up first.
            f"google_genai:{google_model}",
        )

    if provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set. Get a "
                "key at https://openrouter.ai/keys, or set LLM_PROVIDER=google."
            )
        openrouter_model = os.environ.get("OPENROUTER_MODEL", _DEFAULT_OPENROUTER_MODEL)
        return (
            ChatOpenAI(
                model=openrouter_model,
                base_url=_OPENROUTER_BASE_URL,
                api_key=SecretStr(openrouter_key),
                rate_limiter=_rate_limiter("openrouter"),
            ),
            # Not `openai:<model>`. OpenRouter model names carry their own colon
            # (`vendor/model:free`), and deepagents refuses any key with more than
            # one — so the joined form would silently fall through to the
            # provider-wide `openai` registration, which is far too broad. The bare
            # identifier splits cleanly and matches on its own.
            openrouter_model,
        )

    raise RuntimeError(
        f"LLM_PROVIDER={provider!r} is not recognised — use 'google' or 'openrouter'."
    )


model, MODEL_PROFILE_KEY = _build_model()
"""The key `model` resolves under in deepagents' harness-profile registry.

Published from here because this module is the only place that knows which of the
two providers was actually built, and the key's *shape* differs between them —
see the comments in `_build_model`. `agent.py` uses it to switch off a default
sub-agent it has no use for; a key that fails to match doesn't raise, it just
silently leaves the default in place, which is why the shape is worth a comment
rather than a guess.
"""
