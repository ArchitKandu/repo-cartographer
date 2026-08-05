"""The chat model Repo Cartographer reasons with, and the .env loading it needs.

Kept apart from `agent.py` so the agent module is only about the mapping task —
prompt, tools, harness — and swapping models never means touching it. Any
OpenRouter-served model works: the client is a standard OpenAI-compatible one.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

_MISSING_KEY = "OPENROUTER_API_KEY is not set — add it to .env at the repo root."

# Load environment variables from .env file. The path is anchored to this file
# rather than the working directory, so the key resolves whichever directory
# the agent is started from — including from a REPL or `python -c`, where
# dotenv's own find_dotenv() falls back to the cwd.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Checked here rather than left to the first request: without a key OpenRouter
# answers 401, which surfaces deep inside the agent loop as an opaque failure.
api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key:
    raise RuntimeError(_MISSING_KEY)

# Free, so the project can be cloned and run at no cost — but not the smallest
# free model available, which is the tradeoff that matters here. Mapping a repo is
# a multi-step tool-calling task: the agent has to plan, hold a file tree in
# context, and decide what to read next. Models with very few active parameters
# skip the planning step and mistake the workspace tools for the repository.
#
# Overridable from .env so trying another model is a config change, not a code
# change. Free alternatives that support tool calling, roughly in descending
# order of capability on this task:
#
#   nvidia/nemotron-3-ultra-550b-a55b:free    550B/55B active, 1M context.
#                                             Strongest, and the slowest — 55B
#                                             active parameters on a free
#                                             endpoint means queueing.
#   nvidia/nemotron-3-super-120b-a12b:free    120B/12B active, 262K context.
#                                             The default: NVIDIA positions it
#                                             for multi-agent work, and it also
#                                             supports structured outputs.
#   inclusionai/ling-3.0-flash:free           124B/5.1B active. Built for
#                                             token-efficient agentic loops —
#                                             the one to try if Super is slow.
#   openai/gpt-oss-20b:free                   21B/3.6B active. Well-trodden with
#                                             LangChain, but the same weight
#                                             class as the nano model this
#                                             replaced.
#
# Not `openrouter/free`: it picks a free model at random per request, which makes
# a run impossible to reproduce — fatal for Phase 2's definition of done and for
# the eval set in Phase 5.
model = ChatOpenAI(
    model=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
    base_url="https://openrouter.ai/api/v1",
    api_key=SecretStr(api_key),
)
