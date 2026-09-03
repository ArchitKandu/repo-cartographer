"""Tests for the two turns the explorer no longer spends.

No model and no network. Everything `briefing.py` does is a string parse, a list
filter and a filename match, which is the whole reason it was worth moving out of
the model in the first place — and it means the tests for it are instant.

Two kinds of assertion here, and the second kind is the one that matters.

**Does the arithmetic work?** Can a brief be read, is a scope filtered the way
`get_repo_scopes` counts it, does a Python repository match the Python skill.
Ordinary unit tests.

**Do the two sides still agree?** This optimisation spans a prompt and a module:
`ORCHESTRATOR_PROMPT` promises a brief in a particular shape, `parse_brief` reads
that shape, `briefing_for` emits sections under particular headings, and
`EXPLORER_PROMPT` tells the explorer to look for those headings and skip a tool
call when it finds them. Every one of those is a string in a different file, and
none of them is enforced anywhere at run time.

That is the failure this file exists for, because of how it fails: if the heading
drifts, the section is still injected and the explorer still calls
`get_repo_tree`, so the run works and costs exactly what it cost before. Nothing
errors, no output changes, and the saving is silently gone. It is the same shape
as the notes-path bug from Phase 4 — it succeeds somewhere useless — and the same
answer applies: pin the convention in a test, on both sides.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from repo_cartographer import briefing
from repo_cartographer.briefing import (
    _LISTING_TEMPLATE,
    MAX_INJECTED_PATHS,
    Brief,
    BriefingMiddleware,
    briefing_for,
    parse_brief,
    scoped_paths,
)
from repo_cartographer.prompts import EXPLORER_PROMPT, ORCHESTRATOR_PROMPT
from repo_cartographer.skills import SKILLS_DIR, match_skill, skill_body

# The listing and skill sections are found by the explorer by their headings, and
# the headings are the interface. Named once here and asserted against both the
# module that writes them and the prompt that reads them.
LISTING_HEADING = "## The files in your scope"
SKILL_HEADING = "## Your skill for this repository"

# A tree shaped like a real Python project, small enough to read in a test.
PY_TREE = [
    "README.md",
    "pyproject.toml",
    "src/requests/__init__.py",
    "src/requests/adapters.py",
    "src/requests/sessions.py",
    "tests/test_adapters.py",
]


# --------------------------------------------------------------------------- #
# Reading a brief
# --------------------------------------------------------------------------- #


def test_the_brief_the_orchestrator_is_told_to_write_is_one_we_can_read() -> None:
    """The handoff, asserted from the prompt's own words rather than a copy.

    The line below is lifted from `ORCHESTRATOR_PROMPT` — the test asserts it is
    still in there — so a reworded instruction fails here instead of quietly
    producing briefs the parser cannot read.
    """
    template = "owner=<owner> repo=<repo> scope=<scope> notes=/notes/<scope>.md"
    assert template in ORCHESTRATOR_PROMPT, "the brief format moved or was reworded"

    filled = template.replace("<owner>", "psf").replace("<repo>", "requests")
    filled = filled.replace("<scope>", "src")
    assert parse_brief(filled) == Brief(owner="psf", repo="requests", scope="src")


@pytest.mark.parametrize(
    "text",
    [
        "owner=psf repo=requests scope=src notes=/notes/src.md",
        "owner = psf, repo = requests, scope = src",
        'owner="psf" repo="requests" scope="src"',
        "owner=`psf` repo=`requests` scope=`src`",
        "Map the repo. owner=psf\nrepo=requests\nscope=src\nnotes=/notes/src.md",
        "scope=src repo=requests owner=psf",
    ],
)
def test_a_brief_is_read_however_a_model_spaced_it(text: str) -> None:
    """Forgiving on purpose: the writer is a model and the fallback is silent.

    A parse that only accepted one exact spelling would lose the saving the first
    time the model added a comma, and lose it invisibly — the explorer would just
    go back to paying for its own file list.
    """
    assert parse_brief(text) == Brief(owner="psf", repo="requests", scope="src")


@pytest.mark.parametrize(
    "text",
    [
        "repo=requests scope=src",
        "owner=psf scope=src",
        "owner=psf repo=requests",
        "Explore psf/requests and explain how routing works.",
        "",
    ],
)
def test_an_incomplete_brief_is_no_brief(text: str) -> None:
    """Missing one field means None, not a Brief with a guess in it.

    Guessing is the expensive mistake. A wrong owner fetches the wrong
    repository's tree and hands the explorer a file list for a codebase it is not
    exploring — every path in it wrong, and every one of them looking verified.
    """
    assert parse_brief(text) is None


def test_the_notes_path_is_not_mistaken_for_a_field() -> None:
    """`notes=/notes/src.md` sits next to `repo=`, and shares a word with it."""
    brief = parse_brief("owner=psf repo=requests scope=. notes=/notes/root.md")
    assert brief == Brief(owner="psf", repo="requests", scope=".")


@pytest.mark.parametrize("scope", ["", ".", "/", "all", "whole", "repository"])
def test_the_whole_repo_is_recognised_however_it_is_named(scope: str) -> None:
    assert Brief("psf", "requests", scope).is_whole_repo


# --------------------------------------------------------------------------- #
# Filtering a scope
# --------------------------------------------------------------------------- #


def test_a_directory_scope_gets_that_directory() -> None:
    paths = scoped_paths(PY_TREE, Brief("psf", "requests", "src"))
    assert paths == [
        "src/requests/__init__.py",
        "src/requests/adapters.py",
        "src/requests/sessions.py",
    ]


def test_the_dot_scope_gets_the_root_files_and_only_those() -> None:
    """`.` means the root, the same way `get_repo_scopes` counts it.

    The two definitions have to stay identical: the count the orchestrator saw
    and the listing the explorer gets are supposed to be two views of one fact,
    and a scope that counted eight files but listed forty would make the
    orchestrator's sizing decision a fiction.
    """
    assert scoped_paths(PY_TREE, Brief("psf", "requests", ".")) == [
        "README.md",
        "pyproject.toml",
    ]


def test_a_whole_repo_scope_gets_everything() -> None:
    assert scoped_paths(PY_TREE, Brief("psf", "requests", "all")) == PY_TREE


def test_a_scope_that_matches_nothing_is_empty_rather_than_everything() -> None:
    """The dangerous failure would be falling back to the whole tree.

    An explorer briefed `scope=srcs` and handed every path in the repository
    would read outside its scope, duplicate another explorer's work, and report
    success. Empty is the honest answer, and `briefing_for` says so out loud.
    """
    assert scoped_paths(PY_TREE, Brief("psf", "requests", "srcs")) == []


# --------------------------------------------------------------------------- #
# Matching a skill without a model
# --------------------------------------------------------------------------- #


def test_a_python_tree_matches_the_python_skill() -> None:
    assert match_skill(PY_TREE) == "python-repo"


def test_a_node_tree_matches_the_node_skill() -> None:
    assert match_skill(["package.json", "src/index.js", "src/server.ts"]) == "node-repo"


def test_a_source_directory_matches_on_extensions_alone() -> None:
    """The manifest is at the root, and an explorer scoped to `src/` never sees it.

    This is why the matcher has two tiers rather than a list of manifests: the
    scope most likely to hold the answer is the one least likely to contain
    `pyproject.toml`.
    """
    assert match_skill(["src/requests/adapters.py", "src/requests/sessions.py"]) == "python-repo"


def test_a_manifest_outranks_any_number_of_other_files() -> None:
    """One `pyproject.toml` beats a hundred `.ts` files, deliberately.

    A Python package that ships a TypeScript front end is still a Python package
    to the question "what do I read first", and the manifest is the only
    unambiguous evidence of that.
    """
    tree = ["pyproject.toml", *[f"web/src/component{i}.ts" for i in range(100)]]
    assert match_skill(tree) == "python-repo"


def test_an_ecosystem_we_have_no_skill_for_matches_nothing() -> None:
    assert match_skill(["go.mod", "main.go", "internal/server/http.go"]) is None


def test_an_empty_tree_matches_nothing() -> None:
    assert match_skill([]) is None


def test_a_tie_matches_nothing_rather_than_picking_one() -> None:
    """Equal evidence is not a small preference, it is no answer.

    Injecting a coin-flip would put the wrong reading order in front of the
    explorer with the authority of a fact, and the explorer has no way to tell
    that it was a guess. The fallback — the Phase 7 menu, and the explorer's own
    judgement — is strictly better than that.
    """
    tie = ["pyproject.toml", "package.json", "app.py", "app.js"]
    assert match_skill(tie) is None


def test_skill_body_drops_the_frontmatter_and_keeps_the_instructions() -> None:
    body = skill_body("python-repo")
    assert body
    assert "name: python-repo" not in body
    assert "description:" not in body
    assert body.startswith("# Exploring a Python repository")
    # A `---` rule inside the body must survive the frontmatter split.
    assert "## Read in this order" in body


def test_skill_body_of_a_skill_that_does_not_exist_is_empty() -> None:
    """Empty rather than raising: a missing skill costs the explorer nothing.

    It falls back to reading the mount, which is where the file would have been.
    """
    assert skill_body("cobol-repo") == ""


# --------------------------------------------------------------------------- #
# What gets injected, and what the explorer is told to expect
# --------------------------------------------------------------------------- #


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve a tree without touching GitHub.

    `cached_tree` is patched rather than `get_repo_tree` so the TTL cache plays
    no part in a test's outcome — a cache that leaked between tests would make
    them pass in one order and fail in another.
    """

    def serve(paths: list[str] | None) -> None:
        monkeypatch.setattr(briefing, "cached_tree", lambda *_args, **_kwargs: paths)

    return serve


def test_the_listing_is_injected_under_the_heading_the_explorer_looks_for(tree: Any) -> None:
    tree(PY_TREE)
    section = briefing_for("owner=psf repo=requests scope=src notes=/notes/src.md")

    assert LISTING_HEADING in section
    assert "src/requests/adapters.py" in section
    # Scoped, not the whole tree: a listing that leaked `tests/` would send the
    # explorer outside its scope with the file list's authority behind it.
    assert "tests/test_adapters.py" not in section


def test_the_matched_skill_is_injected_whole(tree: Any) -> None:
    tree(PY_TREE)
    section = briefing_for("owner=psf repo=requests scope=. notes=/notes/root.md")

    assert SKILL_HEADING in section
    assert skill_body("python-repo") in section


def test_no_skill_section_when_nothing_matches(tree: Any) -> None:
    tree(["go.mod", "main.go"])
    section = briefing_for("owner=x repo=y scope=all")

    assert LISTING_HEADING in section
    assert SKILL_HEADING not in section


def test_a_scope_too_large_to_inject_injects_no_listing(tree: Any) -> None:
    """Above the cap this declines to help, rather than helping expensively.

    A system prompt is re-sent every turn and cannot be evicted, so injecting a
    listing of thousands of paths would trade one request for tokens on all of
    them. See `briefing.py` for why a truncated listing was the wrong answer.
    """
    tree([f"src/module{i}.py" for i in range(MAX_INJECTED_PATHS + 1)])
    section = briefing_for("owner=x repo=y scope=src")

    assert LISTING_HEADING not in section
    # The skill still is: matching a filename costs nothing at any repository size.
    assert SKILL_HEADING in section


def test_an_empty_scope_is_said_out_loud(tree: Any) -> None:
    """Two turns saved and a wrong turn prevented.

    Without this the explorer fetches the tree, filters to nothing, and has to
    work out on its own whether it was misbriefed or the directory is empty.
    """
    tree(PY_TREE)
    section = briefing_for("owner=psf repo=requests scope=nonexistent")

    assert "holds no files" in section
    assert "src/requests/adapters.py" not in section


@pytest.mark.parametrize(
    ("brief_text", "served"),
    [
        ("Explore psf/requests, no fields at all", PY_TREE),
        ("owner=psf repo=requests scope=src", None),
    ],
)
def test_nothing_is_injected_when_the_answer_is_not_known(
    tree: Any, brief_text: str, served: list[str] | None
) -> None:
    """An unreadable brief and an unreachable GitHub both inject nothing.

    Both are normal outcomes rather than errors, and both must leave the explorer
    exactly as capable as it was before this module existed — it still holds
    `get_repo_tree` and the skills mount.
    """
    tree(served)
    assert briefing_for(brief_text) == ""


# --------------------------------------------------------------------------- #
# The middleware
# --------------------------------------------------------------------------- #


class _Request:
    """The two fields `BriefingMiddleware` touches, and an `override` that records.

    A stand-in rather than a real `ModelRequest`: building one needs a chat model,
    a runtime and a state, none of which this middleware reads. What is asserted
    is that it appends to the system message and leaves everything else alone.
    """

    def __init__(self, brief: str, system: str | list[Any] | None = "PROMPT") -> None:
        self.messages = [HumanMessage(brief)]
        self.system_message = SystemMessage(system) if system is not None else None
        self.overrides: dict[str, Any] = {}

    def override(self, **overrides: Any) -> Any:
        self.overrides = overrides
        return self


def _augment(brief: str, **kwargs: Any) -> _Request:
    """Run one model call through the middleware and hand back what it did.

    The handler is the identity function: this middleware's whole effect is on
    the request going in, so there is nothing to fake on the way out. The ignore
    is for the stand-in request above — mypy is right that it is not a
    `ModelRequest`, and it is deliberately not one.
    """
    request = _Request(brief, **kwargs)
    middleware: Any = BriefingMiddleware()
    middleware.wrap_model_call(request, lambda r: r)
    return request


def test_the_middleware_appends_to_the_system_prompt(tree: Any) -> None:
    tree(PY_TREE)
    request = _augment("owner=psf repo=requests scope=src notes=/notes/src.md")

    injected = request.overrides["system_message"].content
    assert injected.startswith("PROMPT")
    assert LISTING_HEADING in injected


def test_the_middleware_changes_nothing_when_there_is_nothing_to_inject(tree: Any) -> None:
    """A no-op has to be a real no-op: no override call at all.

    Overriding the system message with an unchanged copy would work, and would
    also mean this middleware rewrites the prompt on every turn of every run —
    including the runs where it has nothing to say.
    """
    tree(None)
    request = _augment("owner=psf repo=requests scope=src")
    assert request.overrides == {}


def test_a_prompt_that_is_not_a_string_is_left_alone(tree: Any) -> None:
    """Gemini fills content with typed blocks, which cannot be concatenated.

    Skipping is the safe branch — the explorer keeps the tools to do this itself.
    """
    tree(PY_TREE)
    request = _augment("owner=psf repo=requests scope=src", system=[{"type": "text"}])
    assert request.overrides == {}


# --------------------------------------------------------------------------- #
# Both sides of the handoff
# --------------------------------------------------------------------------- #


def test_only_the_explorer_is_briefed() -> None:
    """The doc-writer must never be handed a list of real repository paths.

    Phase 4's blindfold is that the agent writing the guide cannot reach the
    repository, so it cannot cite a file nobody read. A file listing spliced into
    its prompt would hand it exactly that — hundreds of real, correctly spelled
    paths, every one of which would survive the link-checker — and the guide
    would start citing files no explorer ever opened. The check would pass. The
    citations would be true. And they would be inventions.

    So the middleware's placement is the invariant, not a preference, and it is
    asserted the same way `tests/test_skills.py` asserts the skills mount's.
    """
    from deepagents.backends import FilesystemBackend

    from repo_cartographer.agent import (
        TOOL_RESULT_TOKEN_LIMIT,
        WORKSPACE,
        build_subagents,
    )

    specs = {
        str(spec["name"]): spec
        for spec in build_subagents(
            FilesystemBackend(root_dir=WORKSPACE),
            tool_result_token_limit=TOOL_RESULT_TOKEN_LIMIT,
        )
    }

    def carries_briefing(name: str) -> bool:
        middleware: Any = dict(specs[name]).get("middleware") or []
        return any(m.name == "BriefingMiddleware" for m in middleware)

    assert carries_briefing("explorer")
    assert not carries_briefing("doc-writer")
    # The link-checker holds no model, so it has no prompt to splice into.
    assert "middleware" not in specs["link-checker"]



@pytest.mark.parametrize("heading", [LISTING_HEADING, SKILL_HEADING])
def test_the_explorer_is_told_to_look_for_the_headings_we_write(heading: str) -> None:
    """The drift that costs the saving and reports nothing.

    A heading changed in `briefing.py` alone still injects, and the explorer
    still calls `get_repo_tree` because it is looking for words that are no
    longer there. The run succeeds at the old price. Nothing in a transcript
    would tell you, which is why it is asserted here.

    Matched on the heading text without its `##`, because the prompt refers to
    the sections in prose rather than quoting the markdown.
    """
    title = heading.removeprefix("## ")
    assert title in EXPLORER_PROMPT, f"the explorer is never told about '{title}'"


def test_the_explorer_is_told_not_to_pay_for_what_it_was_given() -> None:
    """The instruction that actually banks the saving.

    Injecting the listing removes the *need* for `get_repo_tree`, not the tool —
    it is deliberately still on the spec as the fallback. So the request is only
    saved if the prompt says not to make it, and the same holds for the skill: it
    is still mounted and still readable, and an explorer that reads the copy it
    was handed pays the turn this was built to remove.
    """
    # Whitespace-normalised: these prompts are hard-wrapped at 80 columns, so a
    # phrase that reads as one line in the file is split by a newline and three
    # spaces in the string. Asserting the literal would make the test fail on a
    # rewrap, which is a change to nothing.
    prompt = " ".join(EXPLORER_PROMPT.split())
    assert "you must not call `get_repo_tree`" in prompt
    assert "do **not** `read_file` it" in prompt


def test_the_explorer_is_told_to_batch_its_reads() -> None:
    """Phase 3's other saving, and the one no code can enforce.

    Whether the model emits four `get_file_contents` calls in one message or four
    messages is a model decision — the same decision the orchestrator's fan-out
    already depends on. There is nothing to assert but the instruction, and
    `scripts/show_contexts.py` reports the tool-calls-per-turn ratio that says
    whether it was followed.
    """
    assert "in a single message" in EXPLORER_PROMPT


def test_the_eval_fingerprint_covers_the_sections_we_inject() -> None:
    """Phase 5's staleness marker has to see instructions that live in Python.

    `prompt_fingerprint` exists so a recorded score cannot outlive the prompt
    that produced it, and the sections below are prompt: rewording the listing
    section changes what every explorer is told. It is also the least obvious
    instruction in the system — it reads as a code change — so it is the easiest
    one to make while `--score-only` keeps reporting old numbers as current.
    """
    import importlib.util

    root = SKILLS_DIR.parent
    spec = importlib.util.spec_from_file_location("_run_evals", root / "scripts" / "run_evals.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    before = module.prompt_fingerprint()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(briefing, "_LISTING_TEMPLATE", _LISTING_TEMPLATE + " reworded")
        assert module.prompt_fingerprint() != before, "rewording a section must move the digest"

    assert module.prompt_fingerprint() == before, "restoring it must restore the digest"
