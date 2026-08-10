# AGENTS.md — house style for the guides this project writes

House style for Repo Cartographer's output, kept here rather than in
`repo_cartographer/prompts.py` for the reason Phase 7 exists: *how to explore any
repository*, *what Python layouts look like*, *what Node layouts look like* and
*what our guides read like* are four separate concerns, and one system prompt
holding all four grows every time any one of them changes.

This file is the fourth. It is appended to the doc-writer's prompt at build time
and applies to every guide whatever the repository is written in — which is what
distinguishes it from `skills/`, where each file loads only when the ecosystem
matches.

**Editing this file changes the output. No Python involved.**

It is deliberately additive. `DOC_WRITER_PROMPT` already sets the guide's four
sections and the rules that keep it truthful — every path comes from the notes,
keep the hedges you were given, describe rather than redesign. Repeating those
here would recreate exactly the sprawl this file was split out to end. What
follows is only what that prompt does not already say.

## Emphasis

- **A table row that names a directory has told nobody anything.** `src/` is not
  a location. Name the file, and name the function or class whenever the notes
  name one — that is the difference between a section people use and a section
  people scroll past.
- **Say how to run the tests**, whenever the notes carry it. It is the first
  thing a new contributor needs and the last thing an architecture overview
  usually mentions.
- **Name the thing that would surprise someone.** A vendored dependency that is
  load-bearing, a directory that is generated rather than written, a module whose
  name does not match what it does. If the notes flagged it, the guide keeps it.

## Voice

- **Plain sentences.** No "delve", no "leverage", no "seamless", no "robust", no
  "comprehensive".
- **No padding.** No preamble, no "in conclusion", no closing offer of further
  help, no restating the question back.
- **Prefer the concrete noun.** "The `Session` object holds the connection pool"
  beats "session management functionality is provided".

## Deliberately not required: a good-first-issues section

The project's README promises one, and this is the natural place to demand it —
the implementation guide even uses it as *the* example of a house-style rule. It
is absent on purpose, and the absence is the rule.

Nothing in this system can read an issue tracker. A house style demanding a
good-first-issues section would be a house style demanding invention, and the
model would comply: plausible issues inferred from the code, indexed by nothing,
which a reader would then go looking for. **A house style can only ask for what
the pipeline can actually supply.**

When something here can read the issues API, this file is where that rule goes,
and it will be one paragraph and no code.
