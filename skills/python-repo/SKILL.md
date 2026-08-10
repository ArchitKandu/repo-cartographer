---
name: python-repo
description: How to explore a Python repository — which files answer which questions, in what order, and which ones to skip. Use this whenever the scope you were given contains pyproject.toml, setup.py, setup.cfg, requirements.txt, a package directory with __init__.py, or .py files generally.
---

# Exploring a Python repository

You have a budget of a handful of file reads and a repository with hundreds of
files. This is the order that answers the most questions per read, and the things
that are worth writing down because a newcomer cannot guess them.

## Read in this order

1. **`pyproject.toml`** (or `setup.py` / `setup.cfg` on an older project). One
   read, and it settles more than any other file:
   - `[project] name` and `dependencies` — what this is and what it stands on.
     The dependency list is often the fastest architecture summary available:
     `flask` + `jinja2` says web app, `pydantic` + `httpx` says API client.
   - `[project.scripts]` / `[project.entry-points]` — the command-line entry
     points, each pointing at `module:function`. That target is a file worth
     opening.
   - `[tool.*]` sections — which linter, type checker and test runner the
     project actually uses, which is what a contributor needs on day one.
   - The `src/` question below.

2. **The package's `__init__.py`.** This is the public API in one file: what the
   library exports is what its users touch, and everything it imports is a module
   worth knowing about. Reading it is usually worth two or three other reads.

3. **The modules the question points at**, found by following imports from
   `__init__.py` rather than by guessing from filenames.

4. **`conftest.py`**, only if the question is about testing. It holds the
   fixtures, which is where the project's own idea of its seams shows.

## Layout: work out which one you are in

- **`src/` layout** — the package lives at `src/<name>/`. The manifest usually
  says so. Everything importable is under there; the repository root holds only
  packaging and CI.
- **Flat layout** — the package directory sits at the repository root next to
  `tests/`, e.g. `httpx/`, `requests/`. Root scope and package scope overlap.

Say which one it is in your notes. It is the first thing that orients a reader,
and it changes where every other path lives.

## Conventions worth naming, because they are invisible to a newcomer

- A leading underscore means private-by-convention: `httpx/_client.py` is
  internal, and `httpx/__init__.py` re-exports the parts that are not. A guide
  that lists `_client.py` without saying this makes the layout look arbitrary.
- `__init__.py` makes a directory a package; an empty one is not a bug.
- `if __name__ == "__main__":` and `__main__.py` are the "run this directly"
  entry points.
- Type stubs (`.pyi`) and `py.typed` describe types, not behaviour.

## Skip these

Lockfiles (`uv.lock`, `poetry.lock`, `Pipfile.lock`), `.egg-info/`, `build/`,
`dist/`, `__pycache__/`, vendored dependencies, and generated migrations. They
are large, they are derived from something else you can read instead, and they
tell a reader nothing.

## Record these in your notes, every time

A guide about a Python repository should be able to answer these, so the notes
have to carry them:

- **The packaging manifest** — the path, by name. `pyproject.toml` or
  `setup.py`.
- **The import entry point** — which `__init__.py`, and the handful of names it
  exports.
- **The layout** — `src/` or flat.
- **How the tests run** — the runner named in the manifest or CI config, and
  where the tests live.
- **What you did not read**, as always.
