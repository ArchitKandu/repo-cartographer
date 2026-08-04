# Test Report — `tests/test_tools.py`

**Command**

```bash
uv run pytest tests/test_tools.py -v --log-cli-level=INFO
```

**Result:** ✅ **20 passed**, 1 deselected in **~15s** · plus **1 passed** under `-m slow` · `ruff` clean

| Field | Value |
| --- | --- |
| Platform | linux — Python 3.12.3 |
| pytest | 9.1.1 (pluggy 1.6.0) |
| Interpreter | `.venv/bin/python3` |
| rootdir / config | `/home/blackcoat/Repositories/repo-cartographer` — `pyproject.toml` |
| Plugins | anyio 4.14.2, langsmith 0.10.15 |
| Collected | 21 items → 1 deselected (`slow`) → **20 selected** |
| GitHub core quota | 5000/hour — **authenticated** |

> These are **live** tests: they hit the real GitHub REST API against `octocat/Hello-World`, `pallets/flask` and `microsoft/TypeScript`. A run costs ~21 calls against the 5000/hour core pool and 5 against the separate 30/minute search pool.

---

## Results at a glance

| # | Test | Area | Status |
| --- | --- | --- | --- |
| 1 | `test_tools_authenticate_with_the_token` | auth | ✅ |
| 2 | `test_tree_returns_plain_strings` | `get_repo_tree` | ✅ |
| 3 | `test_tree_is_recursive_and_repo_relative` | `get_repo_tree` | ✅ |
| 4 | `test_tree_has_no_duplicate_paths` | `get_repo_tree` | ✅ |
| 5 | `test_tree_contains_files_only` | `get_repo_tree` | ✅ |
| 6 | `test_tree_accepts_a_branch_ref` | `get_repo_tree` | ✅ |
| 7 | `test_tree_accepts_a_commit_sha` | `get_repo_tree` | ✅ |
| 8 | `test_tree_raises_on_unknown_repo` | `get_repo_tree` | ✅ |
| 9 | `test_tree_raises_on_unknown_ref` | `get_repo_tree` | ✅ |
| — | `test_tree_raises_rather_than_truncating` | `get_repo_tree` | ✅ `slow` |
| 10 | `test_file_contents_are_decoded_text` | `get_file_contents` | ✅ |
| 11 | `test_file_contents_from_a_nested_path` | `get_file_contents` | ✅ |
| 12 | `test_every_tree_path_is_a_valid_argument` | integration | ✅ |
| 13 | `test_file_contents_raises_on_missing_file` | `get_file_contents` | ✅ |
| 14 | `test_file_contents_rejects_binary_files` | `get_file_contents` | ✅ |
| 15 | `test_file_contents_rejects_a_directory_path` | `get_file_contents` | ✅ |
| 16 | `test_file_contents_rejects_a_file_over_the_inline_limit` | `get_file_contents` | ✅ |
| 17 | `test_search_returns_dicts_with_paths` | `search_code` | ✅ |
| 18 | `test_search_stays_inside_the_requested_repo` | `search_code` | ✅ |
| 19 | `test_search_with_no_matches_returns_empty_list` | `search_code` | ✅ |
| 20 | `test_search_query_is_url_escaped` | `search_code` | ✅ |

The `slow` test is deselected by default (`addopts = "-m 'not slow'"`); run it with `uv run pytest tests/test_tools.py -m slow`.

---

## Fixtures

Two tree fetches are shared across tests — one API call each, then reused:

| Fixture | Repo | Entries | First entries |
| --- | --- | --- | --- |
| `tiny_tree` | `octocat/Hello-World` | 1 | `README` |
| `nested_tree` | `pallets/flask` | 236 | `.devcontainer/devcontainer.json`, `.devcontainer/on-create-command.sh`, `.editorconfig`, `.github/ISSUE_TEMPLATE/bug-report.md`, … |

`microsoft/TypeScript:src/compiler/checker.ts` (~3.2 MB) is read once, unfixtured, by the inline-limit test. That call is cheap precisely because GitHub omits the content of a file that size.

## Exception types

| Type | Means | Raised by |
| --- | --- | --- |
| `GitHubError` | the API said no — a rejected request, or a tree it would not return whole | all three tools |
| `ValueError` | the argument was wrong — a path that is not a readable UTF-8 text file | `get_file_contents` |

`GitHubError` subclasses `Exception`, so `search_or_skip`'s broad `except` still catches a spent search quota.

---

## Auth

### 1. `test_tools_authenticate_with_the_token`

- `_headers()` carries an `Authorization` header → 2 headers: `Accept`, `Authorization`
- GitHub grants the **authenticated** quota for those headers:

```json
{
  "limit": 5000,
  "remaining": 4910,
  "status": 200
}
```

---

## `get_repo_tree`

### 2. `test_tree_returns_plain_strings`
Return type is a non-empty `list[str]`; the whole tiny tree equals `['README']`.

### 3. `test_tree_is_recursive_and_repo_relative`
- Root-level file present: `pyproject.toml`
- Deep file present, proving `recursive=1`: `src/flask/__init__.py`
- Absolute or `./`-prefixed paths: **0**

### 4. `test_tree_has_no_duplicate_paths`
Duplicates across 236 entries: **0**.

### 5. `test_tree_contains_files_only`
Every entry must be readable as a file, so `type=tree` (directories) and `type=commit` (submodules) are filtered out.

- `src`, `src/flask`, `docs` — **absent** from the tree
- `src/flask/__init__.py` — still present, so the filter didn't over-reach
- Directory names remain **derivable** from the paths: `.devcontainer`, `.github`, `docs`, `examples`, `src`, … (6 total)

### 6–7. Ref handling
| Test | Call | Result |
| --- | --- | --- |
| `test_tree_accepts_a_branch_ref` | `get_repo_tree(ref='master')` on `octocat/Hello-World` | `['README']` |
| `test_tree_accepts_a_commit_sha` | `get_repo_tree(ref='7fd1a60b01f9…')` | `['README']` |

### 8–9. Error handling
Both an unknown repo and an unknown ref surface the same 404:

```text
Failed to fetch repository tree: 404 - {"message":"Not Found","documentation_url":"https://docs.github.com/rest/git/trees#get-a-tree","status":"404"}
```

### `test_tree_raises_rather_than_truncating` (`slow`)
GitHub caps a recursive tree at ~100k entries / 7MB and sets `truncated=true`. The tool refuses the partial answer rather than returning a tree that looks complete:

```text
GitHub truncated the tree for torvalds/linux@HEAD — 71798 entries returned, more exist. Reading the whole tree would need per-directory walking, which this tool does not do; explore a subdirectory instead.
```

---

## `get_file_contents`

### 10. `test_file_contents_are_decoded_text`
`octocat/Hello-World:README` → 13 chars, `'Hello World!\n'`. Base64 was decoded, not passed through.

### 11. `test_file_contents_from_a_nested_path`
`pallets/flask:src/flask/__init__.py` → 2072 chars; newlines survived decoding.

```python
from . import json as json
from .app import Flask as Flask
from .blueprints import Blueprint as Blueprint
from .config import Config as Config
from .ctx import after_this_request as after_this_request
…
```

### 12. `test_every_tree_path_is_a_valid_argument`
The contract between the two tools: all 1 tiny-tree paths fed back into `get_file_contents` → `README → 13 chars`. Now guaranteed for every entry, since the tree holds files only.

### 13. `test_file_contents_raises_on_missing_file`

```text
Failed to fetch file contents: 404 - {"message":"Not Found","documentation_url":"https://docs.github.com/rest/repos/contents#get-repository-content","status":"404"}
```

### 14. `test_file_contents_rejects_binary_files`
Reading a PNG (`docs/_static/debugger.png`) raises a `ValueError` that names the path and the reason:

```text
ValueError: 'docs/_static/debugger.png' in pallets/flask is not UTF-8 text — likely a binary file such as an image or archive ('utf-8' codec can't decode byte 0x89 in position 0: invalid start byte)
```

### 15. `test_file_contents_rejects_a_directory_path`
A directory path (`src`) is detected — GitHub answers with a JSON array — and the error points at a usable path instead:

```text
ValueError: 'src' is a directory in pallets/flask, not a file. Pass a path inside it, such as 'src/flask'.
```

### 16. `test_file_contents_rejects_a_file_over_the_inline_limit`
Past 1 MB GitHub stops inlining content: it answers `encoding: "none"` with `content: ""`, which decodes to `""` and reads as a legitimately empty file. The tool refuses instead, and says where to go:

```text
ValueError: 'src/compiler/checker.ts' in microsoft/TypeScript is 3.2 MB, over GitHub's 1 MB inline limit, so the API returned no content (encoding='none'). Skip it, or fetch it directly from https://raw.githubusercontent.com/microsoft/TypeScript/main/src/compiler/checker.ts
```

Symlinks and submodules are refused on the same path — GitHub sends a target or a git URL where the content would be.

---

## `search_code`

### 17. `test_search_returns_dicts_with_paths`
Searching `pallets/flask` for `Blueprint` → 30 results; every result is a `dict` carrying a `path`.

```text
src/flask/blueprints.py
src/flask/sansio/blueprints.py
src/flask/wrappers.py
docs/blueprints.rst
docs/cli.rst
… 25 more
```

### 18. `test_search_stays_inside_the_requested_repo`
Distinct repos represented in the results: **1** — `pallets/flask`.

### 19. `test_search_with_no_matches_returns_empty_list`
Searching for a string that cannot occur → `[]` (0 items).

### 20. `test_search_query_is_url_escaped`
Queries are percent-encoded before reaching the URL. Two probes:

| Query | Expectation | Result |
| --- | --- | --- |
| `def create_app` | the space survives encoding and still matches | 26 results — `examples/tutorial/flaskr/__init__.py`, `examples/celery/src/task_app/__init__.py`, `docs/cli.rst`, … |
| `zzqq&9c1f2bnotpresent` | the `&` must not end the `q` parameter early | `[]` |

---

## Skip / fail behavior

The suite distinguishes environment limits from real defects, so red always means a bug:

| Condition | Behavior | Why |
| --- | --- | --- |
| Core quota below `PHASE1_MIN_BUDGET` (default 20) | **skip** the module | A drained rate limit is an environment problem |
| GitHub answers 401 to `GITHUB_TOKEN` | **fail** | A broken credential is a real problem |
| Search pool (30/min) spent → 403 | **skip** that test | Ordinary throttling; likely on back-to-back runs |
| API unreachable | **skip** | Not a defect in the tools |

---

## Notes

- Every wart these tests once pinned is **fixed**, and the tests now assert the corrected behavior: directories in the tree, a bare `UnicodeDecodeError`, an opaque `TypeError` on a directory, unencoded search queries, silently truncated trees, and files over 1 MB returning `""`.
- Three of those were **silent wrong answers** rather than crashes — a partial tree, an empty large file, an unscoped search — which is why they are worth fixing before an agent sits on top of the tools: nothing raises, so nothing tells you.
- `uv run ruff check repo_cartographer tests` passes. One `# noqa: TRY004` is deliberate: ruff wants `TypeError` for the directory case, but `path` is a valid `str` that merely points somewhere unreadable, so `ValueError` is correct.
- Runtime of ~15s is dominated by network round-trips; the shared tree fixtures keep the API-call count low.
