"""Phase 6's definition of done, run the expensive way.

    uv run scripts/prove_link_checker.py

The guide says: *deliberately feed the doc-writer a fake file path and confirm
`link-checker` flags it before a human ever sees the output.* This does exactly
that, in three steps and about two model requests:

1. Plant a notes file in the workspace that is accurate about `psf/requests`
   except for one invented path, `src/requests/router.py`.
2. Run the **real** doc-writer sub-agent over it — same prompt, same tools, same
   graph as a live run — and let it write its guide to `/guide.md`.
3. Run the **real** link-checker over that guide and print the verdict.

## Why this exists when a unit test already asserts it

`tests/test_citations.py::test_the_definition_of_done` plants a guide and checks
the alarm goes off. That is the right test — deterministic, free, and it runs in
milliseconds — but it proves the checker rejects *a string this project wrote*.
It cannot prove the checker rejects what the system actually produces, because
the doc-writer is nowhere in it.

The difference is not academic. Between a notes file and a verdict sit a model's
choices about which paths to repeat and how to spell them, a `write_file` call,
a workspace path convention agreed by two prompts, and a brief the orchestrator
composes in prose. Every one of those is a place the chain can come apart while
every unit test stays green. So the phase gets both: the fast proof that the
logic is right, and this one, that the wiring is.

## What a run can honestly conclude

The doc-writer might not repeat the planted path — it is told to report only
what the notes support, and dropping a suspicious-looking file is within that.
When that happens the script says the run was **inconclusive** rather than
claiming a pass, because a check that was never presented with a fake path has
demonstrated nothing about fake paths.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_cartographer.agent import RECURSION_LIMIT, WORKSPACE, agent
from repo_cartographer.link_checker import DEFAULT_GUIDE_PATH

OWNER, REPO = "psf", "requests"

# The lie. Plausible enough that a reader would go looking for it, and absent
# from psf/requests at every revision — that library has no router.
FAKE_PATH = "src/requests/router.py"

NOTES_PATH = "/notes/src.md"

# Notes in the shape an explorer really writes, because the doc-writer's
# behaviour is a response to their shape as much as their content. Everything
# here is true of psf/requests except the one line marked below.
POISONED_NOTES = """# Architecture Notes: `src/` directory of `psf/requests`

## Main modules in `src/requests/`

- **`src/requests/__init__.py`**: Package entry point. Exposes the top-level
  functions (`get`, `post`, `request`) and classes (`Session`, `Request`,
  `Response`).
- **`src/requests/api.py`**: The module-level convenience functions. Every one of
  them opens a short-lived `Session` and delegates to it.
- **`src/requests/sessions.py`**: Defines the `Session` object and
  `SessionRedirectMixin`. Persists cookies, headers, auth and connection pools
  across requests.
- **`src/requests/router.py`**: Maps a request's method and URL onto the adapter
  that will send it, and holds the prefix-matching table `Session.mount()`
  writes into.
- **`src/requests/adapters.py`**: `HTTPAdapter.send()` — the wire-level send,
  over urllib3 connection pools.
- **`src/requests/models.py`**: `Request`, `PreparedRequest` and `Response`.

Read files: src/requests/__init__.py, src/requests/api.py,
src/requests/sessions.py, src/requests/router.py, src/requests/adapters.py,
src/requests/models.py — 6 of 20 files in scope. Skipped packaging metadata.
"""

DOC_WRITER_BRIEF = f"""Write the onboarding guide for the public GitHub repository {OWNER}/{REPO}.

The question: explain the architecture of {OWNER}/{REPO} — what the main modules
are, where the Session object is defined, and how a request ends up being sent.

The notes are at {NOTES_PATH}. Save the guide to {DEFAULT_GUIDE_PATH}, then
return it as your final message."""

CHECKER_BRIEF = f"owner={OWNER} repo={REPO} guide={DEFAULT_GUIDE_PATH}"


def subagent(name: str) -> Any:
    """Reach one compiled sub-agent out of the `task` tool's closure.

    Private structure, and knowingly so — the same walk `tests/test_wiring.py`
    documents. The alternative is a full mapping run to exercise one delegate,
    which costs twenty model requests and several minutes to demonstrate a
    handoff that involves two.
    """
    node: Any = agent.nodes["tools"]
    task = getattr(node, "bound", node).tools_by_name["task"]
    for cell in (c.cell_contents for c in (task.func.__closure__ or ())):
        if isinstance(cell, dict) and name in cell and hasattr(cell[name], "invoke"):
            return cell[name]
    raise SystemExit(f"could not reach the {name!r} sub-agent — has deepagents moved?")


def clear_workspace() -> None:
    """Empty ./workspace so the planted notes are the only notes there are.

    The doc-writer is told to `ls` and read what it finds rather than trusting
    its brief, which is the right instruction and also means a leftover notes
    file from an earlier run would dilute the poison. Same seatbelt as
    `run_evals.py`: refuse to delete anything that is not the workspace.
    """
    if WORKSPACE.name != "workspace":
        raise SystemExit(f"refusing to clear {WORKSPACE}, which is not the workspace")
    WORKSPACE.mkdir(exist_ok=True)
    for child in WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def run() -> int:
    print(f"Planting a fake path in the notes: {FAKE_PATH}\n")
    clear_workspace()
    notes = WORKSPACE / NOTES_PATH.lstrip("/")
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(POISONED_NOTES, encoding="utf-8")

    print("1. running the real doc-writer over the poisoned notes …")
    result = subagent("doc-writer").invoke(
        {"messages": [{"role": "user", "content": DOC_WRITER_BRIEF}]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    guide = result["messages"][-1].text
    written = WORKSPACE / DEFAULT_GUIDE_PATH.lstrip("/")
    print(f"   guide returned: {len(guide)} chars")
    print(f"   guide on disk:  {'yes' if written.exists() else 'NO — it wrote nothing'}\n")

    repeated = FAKE_PATH in guide or (written.exists() and FAKE_PATH in written.read_text())
    print("2. did the doc-writer repeat the fake path?")
    if not repeated:
        print(f"   NO — it dropped {FAKE_PATH}.\n")
        print(
            "INCONCLUSIVE. The checker was never handed a fake path, so this run\n"
            "says nothing about whether it would catch one. That is a good outcome\n"
            "for the doc-writer and a useless one for this script — run it again."
        )
        return 2
    print(f"   YES — it cites {FAKE_PATH}, exactly as an explorer's mistake would carry.\n")

    print("3. running the real link-checker over the guide it wrote …\n")
    verdict = subagent("link-checker").invoke(
        {"messages": [{"role": "user", "content": CHECKER_BRIEF}]}
    )["messages"][-1].text
    print("\n".join(f"   │ {line}" for line in verdict.splitlines()))

    flagged = FAKE_PATH in verdict and "NOT FOUND" in verdict
    print()
    if flagged:
        print(
            f"PASS — {FAKE_PATH} was flagged before a human ever saw the guide,\n"
            "by a delegate that made no model call to do it."
        )
        return 0
    print(
        f"FAIL — {FAKE_PATH} reached the guide and the checker did not catch it.\n"
        "This is the failure mode the whole phase exists to prevent."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
