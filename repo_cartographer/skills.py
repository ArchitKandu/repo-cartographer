"""Phase 7: the two files that are not code, and how an agent reaches them.

By Phase 6 the prompts were carrying four different jobs at once — how to explore
any repository, what Python layout conventions look like, what Node layout
conventions look like, and what this project's guides read like. Every new
ecosystem grew `EXPLORER_PROMPT`, and every agent paid for all of it on every
turn whether or not the repository was written in that language.

This module splits those four apart. Two of them move out of `prompts.py`
entirely:

| Lives in | What it is | Loaded |
|---|---|---|
| `skills/python-repo/SKILL.md` | which files answer which questions in a Python repo | **only when the explorer decides it applies** |
| `skills/node-repo/SKILL.md` | the same for JavaScript and TypeScript | same |
| `AGENTS.md` | this project's house style for a guide | always, into the doc-writer |

The difference between those two rows is the whole design. A skill is
*conditional*: the explorer is shown a one-line description of each and reads the
full file only if the repository matches, so a Node run never pays for the Python
conventions. `AGENTS.md` is *unconditional*, because "our guides end with what we
did not look at" is true of every repository there is.

Both are plain markdown a person can edit without touching Python, which is the
point Phase 7 is making: this is prompt decomposition, not a feature.

## How a skill reaches the model

`SkillsMiddleware` reads skills **through the agent's backend**, and it tells the
model to `read_file` the path it advertises. Both halves of that matter: the
skills have to live at a path the explorer's own `read_file` can reach, or the
model is handed a path it cannot open.

The workspace backend is rooted at `./workspace`, and `skills/` is source rather
than scratch, so it cannot simply be moved there. `build_backend()` therefore
composes the two into one virtual filesystem:

    /notes/src.md            → ./workspace/notes/src.md      (read-write)
    /guide.md                → ./workspace/guide.md          (read-write)
    /skills/node-repo/…      → ./skills/node-repo/…          (READ-ONLY)

## Why the skills mount is read-only, and not as a matter of taste

`./skills` is inside the git repository. Without the wrapper below, an explorer
holding `write_file` could write through the mount into this project's own source
tree — including into the very instructions it is following. Nothing would error.

Two of this codebase's recurring arguments meet here: *a capability withheld
beats an instruction repeated*, and the observation that failures in a delegated
system do not announce themselves, they succeed somewhere useless. A prompt
saying "do not write to /skills" would be an instruction; `ReadOnlyBackend` is
the capability withheld.

## Why AGENTS.md is not loaded the library's way

deepagents implements the AGENTS.md specification, as
`create_deep_agent(memory=["/memory/AGENTS.md"])`. It reads through the backend
too — so using it would mean mounting the directory holding `AGENTS.md`, which is
the repository root, into the doc-writer's filesystem.

That is a bad trade for this agent in particular. `DOC_WRITER_PROMPT` opens by
telling it to `ls` the workspace and read what it finds, deliberately, so that a
missing notes file is discovered rather than assumed. Mount the repository root
and that instruction starts turning up `README.md` and `ARCHITECTURE.md` —
documents about *this* project, sitting in the same namespace as notes about the
repository it is supposed to be describing. The failure would not be an error; it
would be a guide that quietly drifts into describing the wrong codebase.

So the house style is read once, at build time, and appended to the doc-writer's
system prompt. The file stays where the convention says it should be, editable by
anyone, and no delegate gains a view of a directory it has no business in.
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileUploadResponse,
    WriteResult,
)

# Anchored to this file rather than the working directory, for the same reason
# `agent.py` anchors the workspace and `models.py` anchors its .env: a run
# started from a REPL or another directory must reach the same files.
_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = _ROOT / "skills"
AGENTS_FILE = _ROOT / "AGENTS.md"

# Where the skills appear in the agents' virtual filesystem. The trailing slash
# is required by `CompositeBackend`, which matches routes by prefix; without it
# `/skillset.md` would route into the skills mount.
SKILLS_MOUNT = "/skills/"

_READ_ONLY = (
    "Error: '{path}' is in the read-only skills library. Skills are instructions "
    "to follow, not files to change. Write your own output to the workspace path "
    "your brief named."
)


class ReadOnlyBackend(FilesystemBackend):
    """A `FilesystemBackend` with every mutating operation refused.

    Subclasses rather than wraps, deliberately. `CompositeBackend` introspects
    the backend *class* to decide how to call it — `_method_accepts_max_count`
    reads the signature of `grep` off the type — so a delegating proxy with
    `__getattr__` would satisfy every call at run time and quietly fail that
    inspection. Inheriting means every read path is the real implementation with
    the real signature, and only the four write paths are replaced.

    Refusals are returned, not raised: these results are rendered into a tool
    message the model reads, and an exception here would surface as an agent
    crash rather than as "that file is read-only, write somewhere else."
    """

    def __init__(self, *args: object, mount: str = SKILLS_MOUNT, **kwargs: object) -> None:
        # `CompositeBackend` strips the route prefix before calling a mounted
        # backend, so `file_path` arrives here as `/node-repo/SKILL.md` — a path
        # that appears nowhere in the model's world. Refusing by that name would
        # name a file the agent never asked for. The mount is carried so the
        # message can put the path back the way the model wrote it.
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._mount = mount.rstrip("/")

    def _refuse(self, file_path: str) -> str:
        return _READ_ONLY.format(path=f"{self._mount}{file_path}")

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self._refuse(file_path))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(error=self._refuse(file_path))

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self._refuse(file_path))

    async def adelete(self, file_path: str) -> DeleteResult:
        return self.delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error=self._refuse(path)) for path, _ in files
        ]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)


def build_backend(workspace: Path) -> BackendProtocol:
    """One virtual filesystem: a writable workspace plus a read-only skills mount.

    Returned as a single backend because every agent in the system must share it
    — that is what makes the workspace a channel rather than three private
    scratch spaces, and it is now also what lets the explorer `read_file` a skill
    with the same tool it uses for everything else.
    """
    return CompositeBackend(
        default=FilesystemBackend(root_dir=workspace),
        routes={SKILLS_MOUNT: ReadOnlyBackend(root_dir=SKILLS_DIR)},
    )


def available_skills() -> list[str]:
    """The skill names on disk, from the directory layout alone.

    Used by `tests/test_skills.py` to check the mount against the source tree
    without building an agent. The Agent Skills specification requires a skill's
    `name` to equal its directory name, so the directory listing is the
    authority on what exists.
    """
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        entry.name for entry in SKILLS_DIR.iterdir() if (entry / "SKILL.md").is_file()
    )


def house_style() -> str:
    """`AGENTS.md`, ready to append to a system prompt. Empty if it is missing.

    Missing is tolerated rather than fatal: the house style shapes a guide, it
    does not enable one, and a fresh clone that has lost the file should still
    map a repository. `tests/test_skills.py` asserts the file is actually there,
    which is the right place for that check — it is a fact about this repository,
    not a runtime requirement.
    """
    if not AGENTS_FILE.is_file():
        return ""
    return AGENTS_FILE.read_text(encoding="utf-8").strip()


HOUSE_STYLE_HEADER = """

## House style

The rest of this prompt is how to do the job. What follows is how this project's
guides are written — it comes from `AGENTS.md` at the repository root, it applies
to every guide whatever the repository is written in, and where it is more
specific than anything above, it wins.

"""
