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

model = ChatOpenAI(
    model="nvidia/nemotron-3-nano-30b-a3b:free",
    base_url="https://openrouter.ai/api/v1",
    api_key=SecretStr(api_key),
)
