"""Repo Cartographer — maps a public GitHub repository and explains it.

Point it at `owner/repo` with a question and it walks the file tree, decides
which files are worth opening, reads them, and answers from what it found:
the architecture, and where things actually happen. It is not a coding
assistant — it reads repositories and describes them, it does not write code.

The GitHub tools are re-exported here so callers can reach them from the
package root: `from repo_cartographer import get_repo_tree`. They stand alone,
with no LLM involved.

`agent` is deliberately not re-exported. Importing it builds a chat model and
needs a provider key at import time, so re-exporting it would make the bare
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
