---
name: node-repo
description: How to explore a JavaScript or TypeScript repository — which files answer which questions, in what order, and which ones to skip. Use this whenever the scope you were given contains package.json, tsconfig.json, a src/ or lib/ directory of .js/.mjs/.ts files, or node_modules.
---

# Exploring a JavaScript or TypeScript repository

The instinct carried over from other ecosystems — open the folder that sounds
like the source — wastes reads here. `package.json` names the entry point
explicitly, and it is almost never the file you would have guessed.

## Read in this order

1. **`package.json`**. Always first, and it settles most of the map:
   - `"main"`, `"module"`, `"exports"` — the entry point(s). `"exports"` is the
     modern one and can define several, including subpath entries like
     `"./plugin"`. Whatever it points at is your next read.
   - `"type"` — `"module"` means ESM (`import`), absent or `"commonjs"` means
     CJS (`require`). This changes what the code looks like everywhere.
   - `"scripts"` — how the project is built, tested and released. This is what a
     contributor types on day one, and it is invisible from the source.
   - `"dependencies"` — the architecture in a list. `"bin"` — the CLI entry.
   - `"workspaces"` — if present, this is a monorepo and each package under it
     has its own `package.json`. Say so early; it changes what "the source" even
     means.

2. **The entry point named by `main`/`module`/`exports`.** Often a thin
   re-export barrel — which is still useful, because it names the real modules.

3. **The modules it re-exports**, following imports rather than guessing.

4. **`tsconfig.json`**, only if the question touches build or types: `paths`
   aliases explain imports that otherwise look impossible to resolve.

## Layout: the guesses that go wrong here

- `src/` is source, `lib/` and `dist/` and `build/` are usually *compiled
  output* of it. Reading `dist/` gives you bundled, minified, generated code —
  and describing it in a guide is worse than saying nothing.
- ...except when `lib/` **is** the source, which is common in older CJS projects
  with no build step. Check `package.json`'s `"main"` and `"files"` before
  deciding. If `main` points into `lib/` and there is no build script, `lib/` is
  hand-written.
- A vendored directory (`vendor/`, `source/vendor/`) can be load-bearing rather
  than skippable — some libraries inline a dependency deliberately. If the entry
  point imports from it, read it.

## Conventions worth naming, because they are invisible to a newcomer

- `index.js` / `index.ts` is the implicit entry for a directory import.
- A `.d.ts` beside a `.js` is types only, no behaviour.
- The exported thing is frequently a *factory function*, not a class — the
  "application object" a newcomer looks for is often created by calling
  something, not by `new`.
- Named vs default exports decide how the library is imported, so say which.

## Skip these

`node_modules/`, lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`),
`dist/` and `build/` output, `.min.js` bundles, `coverage/`, and snapshot
fixtures. Large, generated, and derived from something you can read instead.

## Record these in your notes, every time

A guide about a Node repository should be able to answer these, so the notes have
to carry them:

- **`package.json`** — the path, by name.
- **The entry point** — which field declared it (`main`, `module` or `exports`)
  and which file it names.
- **The module system** — ESM or CJS.
- **The scripts** — at least how to build and how to test.
- **Source versus output** — which directory is hand-written, and which is
  generated.
- **What you did not read**, as always.
