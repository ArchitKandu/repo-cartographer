"""Repo Cartographer — an agent that explains code in a GitHub repository.

The GitHub tools are re-exported here so callers can reach them from the
package root: `from repo_cartographer import get_repo_tree`.

`agent` is deliberately not re-exported. Importing it builds a chat model and
reads OPENROUTER_API_KEY at import time, so re-exporting it would make the bare
statement `import repo_cartographer` fail whenever no .env is configured —
including during test collection. Import it from its own module instead:
`from repo_cartographer.agent import agent`.
"""

from repo_cartographer.tools import (
    GitHubError,
    get_file_contents,
    get_repo_tree,
    search_code,
)

__all__ = [
    "GitHubError",
    "get_file_contents",
    "get_repo_tree",
    "search_code",
]
