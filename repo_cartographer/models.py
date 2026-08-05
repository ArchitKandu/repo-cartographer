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
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

# Load environment variables from .env file. The path is anchored to this file
# rather than the working directory, so the keys resolve whichever directory the
# agent is started from — including from a REPL or `python -c`, where dotenv's
# own find_dotenv() falls back to the cwd.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

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


def _build_model() -> BaseChatModel:
    """Construct the chat model named by LLM_PROVIDER, or by whichever key exists.

    Keys are checked here rather than left to the first request: without one both
    providers answer 401, which surfaces deep inside the agent loop as an opaque
    failure rather than a missing-configuration message.
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
        return ChatGoogleGenerativeAI(
            model=os.environ.get("GOOGLE_MODEL", _DEFAULT_GOOGLE_MODEL),
            api_key=SecretStr(google_key),
        )

    if provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set. Get a "
                "key at https://openrouter.ai/keys, or set LLM_PROVIDER=google."
            )
        return ChatOpenAI(
            model=os.environ.get("OPENROUTER_MODEL", _DEFAULT_OPENROUTER_MODEL),
            base_url=_OPENROUTER_BASE_URL,
            api_key=SecretStr(openrouter_key),
        )

    raise RuntimeError(
        f"LLM_PROVIDER={provider!r} is not recognised — use 'google' or 'openrouter'."
    )


model = _build_model()
