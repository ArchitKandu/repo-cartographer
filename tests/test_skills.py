"""Phase 7's tests: are the skills discoverable, reachable, and unwritable?

No model, no network, about a second. Everything Phase 7 adds is a file plus a
mount, and all three ways it can silently fail are checkable without an agent:

**The skill is never found.** `SkillsMiddleware` reads through the backend and
skips anything whose frontmatter breaks the Agent Skills specification — a `name`
that does not match its directory, a missing `description`. It does not raise;
it collects a warning and moves on, so a malformed skill is simply a skill that
never loads and a run that looks completely normal.

**The skill is found but unreachable.** The middleware advertises a path and
tells the model to `read_file` it. If the mount and the advertised path disagree,
the explorer spends a turn on a path that does not resolve, and the transcript
reads like a model failure rather than a wiring one.

**The mount is writable.** `./skills` is inside the git repository. An explorer
holding `write_file` and a mount without `ReadOnlyBackend` can write through it
into this project's own source — including into the instructions it is following.

The one thing not asserted here is whether the explorer *chooses* the right
skill: that is a model decision, so it belongs to the eval set and to
`scripts/show_skills.py`, not to a unit test.
"""

from __future__ import annotations

from typing import Any

import pytest
from deepagents.middleware.skills import SkillsMiddleware

from repo_cartographer.agent import WORKSPACE, build_subagents
from repo_cartographer.prompts import DOC_WRITER_PROMPT
from repo_cartographer.skills import (
    AGENTS_FILE,
    SKILLS_DIR,
    SKILLS_MOUNT,
    ReadOnlyBackend,
    available_skills,
    build_backend,
    house_style,
)

EXPECTED_SKILLS = {"python-repo", "node-repo"}


def _specs_by_name() -> dict[str, dict[str, Any]]:
    """The sub-agent specs as plain mappings, keyed by name.

    Plain dicts rather than the `SubAgent`/`CompiledSubAgent` union, because
    these tests ask about keys that exist on one member and not the other —
    `system_prompt` on the two that think, `skills` on the one that explores.
    """
    specs = build_subagents(build_backend(WORKSPACE), tool_result_token_limit=2_000)
    return {str(spec["name"]): dict(spec) for spec in specs}


@pytest.fixture(scope="module")
def backend():
    return build_backend(WORKSPACE)


@pytest.fixture(scope="module")
def loaded(backend):
    """What `SkillsMiddleware` actually discovers through the real mount.

    Calling the middleware rather than reading the directory is the point: the
    directory listing says what exists on disk, and this says what the explorer
    will be shown. Those differ precisely when a skill is malformed, which is the
    failure this file exists to catch.
    """
    update = SkillsMiddleware(backend=backend, sources=[SKILLS_MOUNT]).before_agent(
        state={}, runtime=None, config=None
    )
    return update or {}


# --------------------------------------------------------------------------- #
# The files exist and say what the specification requires.
# --------------------------------------------------------------------------- #


def test_the_skills_on_disk_are_the_ones_we_expect() -> None:
    assert set(available_skills()) == EXPECTED_SKILLS


def test_every_skill_loads_with_no_warnings(loaded) -> None:
    """A malformed skill is skipped silently, so silence has to be asserted."""
    assert not loaded.get("skills_load_errors"), loaded.get("skills_load_errors")
    assert {s["name"] for s in loaded["skills_metadata"]} == EXPECTED_SKILLS


def test_each_description_says_when_to_use_the_skill(loaded) -> None:
    """Progressive disclosure means the description is all the model sees first.

    The explorer decides whether to spend a read on the full file from this one
    line and nothing else. A description that says only what a skill *is* —
    "conventions for Python repositories" — gives it nothing to match a
    repository against, so each one has to name the files that identify the
    ecosystem.
    """
    triggers = {
        "python-repo": ("pyproject.toml", ".py"),
        "node-repo": ("package.json", "tsconfig.json"),
    }
    for skill in loaded["skills_metadata"]:
        description = skill["description"]
        assert len(description) <= 1024, "over the Agent Skills specification limit"
        assert "use this" in description.lower(), f"{skill['name']} never says when"
        for marker in triggers[skill["name"]]:
            assert marker in description, f"{skill['name']} does not mention {marker}"


def test_the_advertised_path_is_one_the_explorer_can_actually_read(loaded, backend) -> None:
    """The wiring failure that looks like a model failure.

    The middleware tells the model to `read_file` the path in `skill["path"]`.
    The explorer's `read_file` goes through this backend. If the two disagree the
    model burns a turn on a path that does not resolve — and the only sign is a
    tool error buried in a sub-agent's context nobody reads.
    """
    for skill in loaded["skills_metadata"]:
        result = backend.read(skill["path"])
        assert result.error is None, f"{skill['path']} is advertised but unreadable"
        assert result.file_data
        assert result.file_data["content"].startswith("---")


# --------------------------------------------------------------------------- #
# The mount is read-only.
# --------------------------------------------------------------------------- #


def test_the_skills_mount_refuses_every_kind_of_write(backend) -> None:
    """`./skills` is source, not scratch — a write here lands in the git repo."""
    target = f"{SKILLS_MOUNT}python-repo/SKILL.md"

    assert backend.write(target, "overwritten").error
    assert backend.edit(target, "Python", "Perl").error
    assert backend.delete(target).error
    assert backend.write(f"{SKILLS_MOUNT}new-skill/SKILL.md", "x").error

    # And the file on disk is untouched, which is the claim that actually matters.
    assert "overwritten" not in (SKILLS_DIR / "python-repo" / "SKILL.md").read_text()


def test_the_refusal_names_the_path_the_model_used(backend) -> None:
    """`CompositeBackend` strips the route prefix before the mount sees it.

    So the naive message names `/python-repo/SKILL.md` — a path that exists
    nowhere in the model's world and that it never asked for. An error the agent
    cannot connect to its own tool call is an error it cannot act on.
    """
    error = backend.write(f"{SKILLS_MOUNT}python-repo/SKILL.md", "x").error
    assert f"{SKILLS_MOUNT}python-repo/SKILL.md" in error


def test_the_workspace_half_of_the_mount_is_still_writable(backend) -> None:
    """The control arm: read-only must apply to the skills route and nothing else.

    Explorers write notes and the doc-writer writes the guide through this same
    backend, so a wrapper applied one level too high would break every run.
    """
    assert backend.write("/notes/_probe.md", "written by a test").error is None
    assert backend.delete("/notes/_probe.md").error is None


def test_read_only_is_a_subclass_not_a_proxy() -> None:
    """`CompositeBackend` introspects the backend *class*, not the instance.

    It reads the signature of `grep` off the type to decide how to call it, so a
    `__getattr__`-based proxy would answer every runtime call correctly and still
    fail that inspection — a failure that appears only for one method, only on
    one code path.
    """
    from deepagents.backends import FilesystemBackend

    assert issubclass(ReadOnlyBackend, FilesystemBackend)
    assert ReadOnlyBackend.grep is FilesystemBackend.grep
    assert ReadOnlyBackend.write is not FilesystemBackend.write


# --------------------------------------------------------------------------- #
# The house style: always applied, and to exactly one agent.
# --------------------------------------------------------------------------- #


def test_agents_md_exists_and_is_loaded() -> None:
    assert AGENTS_FILE.is_file(), "AGENTS.md is the house style; it should be at the root"
    assert house_style()


def test_the_doc_writer_carries_the_house_style_and_the_others_do_not() -> None:
    """House style shapes the deliverable, so it goes where the deliverable is made.

    Putting it on the explorer would spend tokens on every turn of the agent that
    writes no prose, and putting it on the orchestrator would invite it to edit a
    guide it is told to relay unchanged.
    """
    specs = _specs_by_name()
    marker = "Deliberately not required"  # a phrase that exists only in AGENTS.md

    assert marker in specs["doc-writer"]["system_prompt"]
    assert marker not in specs["explorer"]["system_prompt"]
    assert marker not in DOC_WRITER_PROMPT, "the house style must stay out of prompts.py"


def test_the_house_style_asks_for_nothing_the_pipeline_cannot_supply() -> None:
    """The rule the implementation guide suggests, and why it is refused.

    A house style demanding a good-first-issues section would be a house style
    demanding invention — nothing here can read an issue tracker — and the model
    would comply by inferring issues from code. `AGENTS.md` says so explicitly
    rather than just omitting it, so the omission reads as a decision.
    """
    text = house_style()
    assert "good-first-issues" in text
    assert "Deliberately not required" in text


# --------------------------------------------------------------------------- #
# Only the explorer gets skills.
# --------------------------------------------------------------------------- #


def test_the_eval_fingerprint_covers_the_files_that_are_not_python() -> None:
    """Phase 5's staleness marker has to see Phase 7's instructions.

    `run_evals.py --score-only` re-scores recorded guides for free, which makes
    it the thing you reach for after editing an instruction — and it flags a
    record whose instructions have since changed. Until Phase 7 the instructions
    were three strings in `prompts.py`, so hashing those was the whole set.

    `AGENTS.md` and the skills are now instructions too, and they are the ones
    most likely to be edited, because editing them needs no Python. A fingerprint
    that missed them would let exactly the cheapest kind of change alter the
    output while every recorded score kept reading as current — the instrument
    quietly measuring a system that no longer exists.
    """
    import importlib.util

    root = SKILLS_DIR.parent
    spec = importlib.util.spec_from_file_location("_run_evals", root / "scripts" / "run_evals.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    before = module.prompt_fingerprint()

    skill = SKILLS_DIR / "python-repo" / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    try:
        skill.write_text(original + "\n<!-- fingerprint probe -->\n", encoding="utf-8")
        assert module.prompt_fingerprint() != before, "editing a SKILL.md must move the digest"
    finally:
        skill.write_text(original, encoding="utf-8")

    assert module.prompt_fingerprint() == before, "restoring the file must restore the digest"


def test_only_the_explorer_is_given_the_skills_mount() -> None:
    """Ecosystem knowledge is for the agent choosing which files to open.

    The doc-writer never sees the repository, so "read package.json first" is
    advice it cannot act on — and the skills index costs system-prompt tokens on
    every turn of whichever agent carries it.
    """
    specs = _specs_by_name()
    assert specs["explorer"].get("skills") == [SKILLS_MOUNT]
    assert "skills" not in specs["doc-writer"]
    assert "skills" not in specs["link-checker"]
