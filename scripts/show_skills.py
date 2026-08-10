"""Phase 7's definition of done: a Python repo and a JS repo, back to back.

    uv run scripts/show_skills.py
    uv run scripts/show_skills.py --python encode/httpx --node expressjs/express

The guide asks for two runs against different ecosystems, with the output
visibly following the matching skill *without touching any code*. This runs both
and reports what actually happened, and it takes the phrase "visibly follows"
seriously enough to measure two different things:

**Which skill was read.** This is the hard evidence, and it needs no
interpretation. `SkillsMiddleware` shows the explorer one line per skill and asks
it to `read_file` the full text only when the repository matches. So the tool
calls in the explorers' threads say exactly which ecosystem the system decided it
was in — and, more usefully, which one it decided it was *not* in. A run that
reads both skills has not demonstrated selection; it has demonstrated
thoroughness.

**What reached the guide.** Keyword checks for the things each SKILL.md tells the
explorer to record — `package.json` and an entry-point field for Node, the
packaging manifest and layout for Python. These are a heuristic and are labelled
as one: a marker can be missing from a good guide, and present in a guide that
never read the skill at all. They are corroboration, not proof, which is why the
verdict below turns on the skill reads.

## What "without touching any code" means here

Both runs use the same binary, the same three prompts, and the same `agent`
object. The only thing that differs is the repository named in the question. If
the two guides come out shaped differently, no Python made that happen — two
markdown files did.

## Cost

Two full mapping runs, so roughly fifty model requests and several minutes, most
of it spent in the shared rate limiter. `--python` and `--node` take any
`owner/repo`; the defaults are small on purpose.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

# scripts/ is not a package and the repo root is not on sys.path when this file
# is run directly — pyproject's `pythonpath = ["."]` covers pytest, not this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_cartographer.agent import WORKSPACE, agent, run_config
from repo_cartographer.skills import SKILLS_MOUNT, available_skills

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_PYTHON = "psf/requests"
DEFAULT_NODE = "chalk/chalk"

QUESTION = (
    "Explore the public GitHub repository {slug} and explain its architecture: "
    "what the main modules are, where the entry point is, and how a newcomer "
    "would build and test it. Cite the file paths you actually read."
)

# What each SKILL.md tells the explorer to write down. Keyword checks, and only
# ever corroboration — see the module docstring on why the verdict does not rest
# on them. Each entry is (label, alternatives): any one alternative counts.
MARKERS = {
    "python-repo": [
        ("packaging manifest", ("pyproject.toml", "setup.py", "setup.cfg")),
        ("layout", ("src/", "src layout", "flat layout")),
        ("how tests run", ("pytest", "tests/", "tox", "nox")),
    ],
    "node-repo": [
        ("package.json", ("package.json",)),
        ("entry point field", ('"exports"', '"main"', "exports field", "index.js")),
        ("scripts", ("scripts", "npm run", "npm test")),
    ],
}


class Run(NamedTuple):
    slug: str
    expected: str
    """The skill this repository's ecosystem should select."""

    skills_read: tuple[str, ...]
    guide: str
    error: str | None = None


def clear_workspace() -> None:
    """Empty ./workspace between runs, for the reason `run_evals.py` does.

    The doc-writer is told to `ls` and read what it finds rather than trusting
    its brief. Leave the Python run's notes on disk and the Node run's
    doc-writer can open them — which would produce exactly the cross-ecosystem
    contamination this script exists to rule out. Same seatbelt: refuse to
    delete anything that is not the workspace.
    """
    if WORKSPACE.name != "workspace":
        raise SystemExit(f"refusing to clear {WORKSPACE}, which is not the workspace")
    WORKSPACE.mkdir(exist_ok=True)
    for child in WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def _skill_reads(args: object) -> Iterable[str]:
    """Skill names inside one `read_file` call's arguments.

    Matched on the mount prefix rather than on a filename, because `SKILL.md` is
    the same string in every skill directory and the whole question is *which*
    one was opened.
    """
    text = str(args)
    for name in available_skills():
        if f"{SKILLS_MOUNT}{name}/" in text:
            yield name


def run_one(slug: str, expected: str) -> Run:
    """One full mapping, collecting the skill reads out of the sub-agent threads."""
    clear_workspace()
    question = QUESTION.format(slug=slug)
    read: list[str] = []
    guide = ""

    try:
        for namespace, update in agent.stream(
            {"messages": [{"role": "user", "content": question}]},
            config=run_config(),
            subgraphs=True,
            stream_mode="updates",
        ):
            if not isinstance(update, dict):
                continue
            for node_update in update.values():
                if not isinstance(node_update, dict):
                    continue
                messages: list[Any] = node_update.get("messages", []) or []
                for message in messages:
                    for call in getattr(message, "tool_calls", None) or []:
                        if call.get("name") == "read_file":
                            read.extend(_skill_reads(call.get("args")))
                    # The root namespace is the orchestrator; its last message is
                    # the answer.
                    text = getattr(message, "text", "") or ""
                    if not namespace and text:
                        guide = text
    except Exception as exc:  # noqa: BLE001 — a failed run is a reported outcome
        return Run(slug, expected, tuple(dict.fromkeys(read)), guide, f"{type(exc).__name__}: {exc}")

    return Run(slug, expected, tuple(dict.fromkeys(read)), guide)


def markers_found(run: Run) -> list[tuple[str, bool]]:
    guide = run.guide.lower()
    return [
        (label, any(alternative.lower() in guide for alternative in alternatives))
        for label, alternatives in MARKERS[run.expected]
    ]


def report(runs: list[Run]) -> int:
    print(f"\n{'repository':<22}{'expected skill':<16}{'skills actually read'}")
    print("-" * 72)
    for run in runs:
        actual = ", ".join(run.skills_read) or "NONE"
        print(f"{run.slug:<22}{run.expected:<16}{actual}")
    print("-" * 72)

    for run in runs:
        print(f"\n{run.slug} — what reached the guide ({run.expected} markers):")
        if run.error:
            print(f"  run failed: {run.error[:120]}")
            continue
        for label, found in markers_found(run):
            print(f"  [{'x' if found else ' '}] {label}")

    print()
    if any(run.error for run in runs):
        print("At least one run failed, so this proves nothing either way. Re-run it.")
        return 2

    selected = all(run.skills_read == (run.expected,) for run in runs)
    read_nothing = [run.slug for run in runs if not run.skills_read]

    if read_nothing:
        print(
            f"INCONCLUSIVE — no skill was read for {', '.join(read_nothing)}. The "
            "skills are wired in and discoverable (tests/test_skills.py proves that), "
            "so this is a prompt-adherence result: the explorer was shown the index "
            "and chose not to open anything. Worth recording, not worth hiding."
        )
        return 2

    if not selected:
        crossed = [f"{r.slug} read {list(r.skills_read)}" for r in runs if r.skills_read != (r.expected,)]
        print(
            "PARTIAL — a skill was read on every run, but not exclusively the "
            f"matching one: {'; '.join(crossed)}. Progressive disclosure still saved "
            "nothing on those runs, since the point is reading one file rather than all."
        )
        return 1

    print(
        "PASS — each run read exactly the skill for its own ecosystem and never the "
        "other one. Same binary, same prompts, same agent object on both runs; the "
        "only thing that differed was the repository in the question. No code "
        "decided this — two markdown files did."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default=DEFAULT_PYTHON, metavar="OWNER/REPO")
    parser.add_argument("--node", default=DEFAULT_NODE, metavar="OWNER/REPO")
    args = parser.parse_args()

    from repo_cartographer.models import MODEL_PROFILE_KEY

    plan = [(args.python, "python-repo"), (args.node, "node-repo")]
    print(f"Skills on disk: {', '.join(available_skills())}")
    # Named because the result is a behavioural one, and skill selection is a
    # model decision. A reader comparing this against another run's output needs
    # to know whether the model was held constant.
    print(f"Model: {MODEL_PROFILE_KEY}")
    print("Two runs, back to back, with no code change between them.\n")

    runs = []
    for number, (slug, expected) in enumerate(plan, start=1):
        print(f"[{number}/2] mapping {slug} …", flush=True)
        runs.append(run_one(slug, expected))

    raise SystemExit(report(runs))


if __name__ == "__main__":
    main()
