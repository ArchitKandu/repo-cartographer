"""Command-line entry point for Repo Cartographer.

    uv run main.py "Explore pallets/flask and explain how routing works."

With no argument it asks the example question in `repo_cartographer.agent`,
which is also what `uv run python -m repo_cartographer.agent` does.
"""

import sys


def main() -> None:
    # Imported here rather than at module scope: importing the agent builds a
    # chat model and requires OPENROUTER_API_KEY, and a module shouldn't demand
    # a configured key merely to be imported.
    from repo_cartographer.agent import EXAMPLE_QUESTION, ask

    question = " ".join(sys.argv[1:]).strip() or EXAMPLE_QUESTION
    print(f"Mapping: {question}\n")
    print(ask(question))


if __name__ == "__main__":
    main()
