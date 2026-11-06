# Plan: Multi-language support (AST parsing beyond Python)

**Status:** Research / recommendation. Not yet approved, not yet built.
**Tracking issue:** [#42](https://github.com/519lab/graphLM/issues/42).
**Implementation plan:** `docs/plans/multi-language-implementation.md`.
**Repo state at write time:** `main` @ `e5ebf92`.
**Empirical basis:** all version/ABI/baseline claims below were measured on
2026-08-30 (Debian 13, Python 3.13, core `tree-sitter` 0.26.0) — not read off
docs. Commands are shown so a fresh agent can reproduce them.

**Owner decisions (2026-08-30 session):**
1. **Python is the only core language** — it is the one language with a
   deterministic resolver today, and the only one in the base install. Every
   other language is an **opt-in extra**. (Earlier in the session a "core 4"
   was floated; on reflection the owner narrowed it to Python-only, since no
   non-Python resolver exists yet — there is no basis for privileging any of
   them as core.)
2. **Every non-Python language ships as a pip extra with a *bundled,
   graphlm-authored* resolver.** graphlm writes and tests every resolver
   in-tree; the extra only gates the grammar *wheel* install. **No third-party
   plugin API, no arbitrary-code execution** (a runtime extension API was
   considered and rejected — locked-forever public contract, untrusted code).
3. **None of these resolvers exist yet.** Only Python is real; JS/TS are a
   facade that returns an empty `ParsedFile`. So "add a language" always means
   "write and test a new in-tree resolver," never "flip on something already
   built."

---

## The one-line reframe

**This is not "make graphlm parse JS/TS/Java."** The scanner already ingests
those files, ranks them into the `max_files` cap, and packs tens of thousands of
tokens of them into the pass-2 prompt — where the LLM emits `import_edges` for
them *with no deterministic ground truth to check against*. So the real work is:

> **Add deterministic AST ground truth to languages the LLM already guesses at.**

The parsing half is cheap (~20 lines of query per language). The expensive,
per-language half is **import resolution** — mapping an import statement onto a
file that exists in the scan. That is where all the risk lives.

---

## Architecture: Python core + opt-in language packs

```
CORE (base install — the ONLY always-on language)
  graphlm/parsers/python.py      ✅ done — moved verbatim from parser.py
                                    the only real resolver that exists today

PACKS (pip extra, bundled graphlm-authored resolver, opt-in grammar wheel)
  graphlm/parsers/javascript.py  ← JS+TS+TSX; `graphlm[js]` pulls the wheels
  graphlm/parsers/java.py        ← FQN→path; `graphlm[java]` pulls the wheel
  graphlm/parsers/rust.py        ← mod tree; `graphlm[rust]` pulls the wheel
  ...                              (none built yet; JS/TS is the first)
```

Planned packs: **JS/TS, Java, Rust** (in build order). Others (Go, C, Ruby, C#)
are possible later on demand but are **not planned** — nothing here commits to
them.

**One rule, not a tier list:** Python is core because it is the one language
with a resolver and graphlm's own language. Everything else is a pack — JS/TS is
not privileged over Go; it is simply the *first* pack on the build order (for a
concrete, non-tiering reason: it removes a live facade bug — see build order).

The **only** difference between core and a pack is whether the tree-sitter
grammar wheel is a base dependency or gated behind an extra. Every pack
resolver lives in-tree and is tested by graphlm's own suite (they are *not*
third-party). A pack whose grammar isn't installed **degrades to zero edges**
for that language (one log line), never a crash — that degradation path is now
the *common* path (any repo whose languages the user didn't install extras for),
so it is exercised constantly rather than being a rare edge case.

This deliberately rejects a runtime plugin/entry-point API: no locked-forever
public resolver contract, no running untrusted third-party resolver code. The
cost is "only languages graphlm ships a resolver for" — accepted.

---

## Empirical baseline (measured, not asserted)

`build_dependency_graph` today, run against real sibling repos:

| Repo                      | Lang | Files scanned | `deterministic_edges` |
|---------------------------|------|--------------:|----------------------:|
| graphlm (this repo)       | Py   | 90            | **51**                |
| emberfall-game            | TS   | 190           | **0**                 |
| lodescout-search          | JS   | 49            | **0**                 |
| claude-usage-mon          | Rust | 47            | **0**                 |

Reproduce:
```bash
uv run python -c "
from pathlib import Path
from graphlm.scanner import scan_project
from graphlm.parser import build_dependency_graph
for name,p in [('py','.'),('ts','../emberfall-game'),('rs','../claude-usage-mon')]:
    s = scan_project(Path(p), max_files=200)
    e = build_dependency_graph(s.file_fragments, project_dir=Path(p), max_files=200)
    print(name, len(s.file_fragments), len(e))
"
```

Via the CLI (`--dry-run`, no LLM cost) the TS repo packs **~93k tokens** of
source into pass 2 and still returns 0 AST edges. That is the gap this fills:
those 190 TS files are already sent to the model; today its edge claims about
them are unverifiable.

---

## Why the current code is Python-only (the seam)

`graphlm/parser.py` (645 lines — see "Forced refactor") presents a
multi-language *facade* over a Python-only *implementation*:

- `EXT_TO_LANGUAGE` maps `.js/.ts/.jsx/.tsx`, and `SUPPORTED_LANGUAGES`
  advertises JS/TS.
- **But** `_TreeSitterBackend._get_language` hardcodes `if language == PYTHON:
  … raise ValueError` — only Python has a grammar wired in.
- `parse_file` returns an **empty `ParsedFile()`** for JS/TS — indistinguishable
  from "parsed fine, no imports found."
- `build_dependency_graph` short-circuits: `if detect_language(...) != PYTHON:
  continue`.
- Every resolver helper (`_module_candidates`, `_source_roots`,
  `_resolve_module_name`, `_resolve_import`) encodes **Python's** import model.

`scanner.py:176` `_SOURCE_EXTS` already includes
`.rb .go .rs .java .cs .cpp .c .h .hpp` — that is why non-Python files rank into
the scan. Language support at the *scanner* level already exists; the *parser*
stops at Python.

---

## Feasibility: grammars load against core 0.26 (verified)

graphlm pins `tree-sitter>=0.26,<0.27`. Candidate grammar wheels declare *older*
core requirements in their `[core]` extra (`~=0.22`/`~=0.23`/`~=0.24`), which
raised the real question: **do they load against core 0.26?** Tested in an
isolated venv (`tree-sitter==0.26.0` + each grammar): every one imported, built a
`Language`, and parsed a trivial buffer **without error**. The tree-sitter
language ABI is forward-compatible here; the version markers are conservative.

| Grammar wheel            | Version | `Language()` + parse vs core 0.26 | Install    |
|--------------------------|---------|-----------------------------------|------------|
| tree-sitter-python       | 0.25.0  | OK (`module`)                     | **base**   |
| tree-sitter-javascript   | 0.25.0  | OK (`program`)                    | pack `[js]`|
| tree-sitter-typescript   | 0.23.2  | OK — accessor note below          | pack `[js]`|
| tree-sitter-java         | 0.23.5  | OK (`program`)                    | pack `[java]`|
| tree-sitter-rust         | 0.24.2  | OK (`source_file`)                | pack `[rust]`|
| tree-sitter-go           | 0.25.0  | OK (`source_file`)                | not planned |
| tree-sitter-c            | 0.24.2  | OK (`translation_unit`)           | not planned |
| tree-sitter-cpp          | 0.23.4  | OK (`translation_unit`)           | not planned |
| tree-sitter-ruby         | 0.23.1  | OK (`program`)                    | not planned |
| tree-sitter-c-sharp      | 0.23.5  | OK (`compilation_unit`)           | not planned |

**Accessor gotcha (confirmed):** `tree_sitter_typescript` has **no bare
`language()`**. It exposes `language_typescript()` and `language_tsx()`. So the
grammar registry keys on **`(module, accessor)` pairs**, and **TS + TSX are two
grammar entries for one logical language.** Every other grammar uses `language()`.

---

## Per-language resolution notes

Governing rule, carried forward from #19: **a false edge in the
do-not-contradict table is worse than a missing one.** Each resolver starts
deliberately *under*-resolving, and when it knows it is partial it trips the same
honesty mechanism the edge-budget cap already uses in `context.py` (framing flips
to "not exhaustive — infer the rest").

**Python — ✅ the only built resolver.** Move `parser.py`'s resolver to
`parsers/python.py` **verbatim** (do not re-litigate #19). Pure relocation,
Python suite green before anything else moves. Everything below is a pack that
does not exist yet.

**JavaScript / TypeScript (pack `[js]`)** — one shared resolver. Specifiers are
**path-relative** (`./foo`, `../bar/baz`), not dotted. Resolution: extension-probe
order (`.ts .tsx .js .jsx .mjs .cjs`), then `<spec>/index.*`. Bare specifiers
(`react`, `lodash`) → node_modules → **drop** (same rule as Python stdlib).
`import`, `require()`, and dynamic `import()` all count as edges; decide the
`kind` label (see models.py touch below). **Out of scope for v1:**
`tsconfig.json` `paths`/`baseUrl` aliases and package `exports` maps — the rabbit
hole. Relative specifiers alone cover most intra-repo edges. TS + TSX are two
grammar registrations feeding this one resolver.

**Java (pack `[java]`)** — closer to Python than to Go (verified). Confirmed
against the real grammar: `import com.acme.model.User;` parses to a
`scoped_identifier` `com.acme.model.User`, which maps to path
`com/acme/model/User.java` — a **fully-qualified-name → file** model, the direct
analog of Python's dotted-module → file. **No** Go-style directory-as-target
mismatch.
- **Source roots** are the #19 problem again: files live under
  `src/main/java/…`, `src/test/java/…`, or `src/…` (Maven/Gradle), and the FQN is
  relative to that root. Reuse the `_source_roots` *pattern* with Java root names
  (`src/main/java`, `src/test/java`, `src`).
- **`package com.acme.service;`** gives the file's own FQN — a real source-root
  *disambiguator* Python lacks (verify the file sits where its package says).
- **Two edge shapes to handle:** `import com.acme.model.*;` (wildcard → a package
  = a directory of `.java` files — a fan-out, or resolve to the dir) and
  `import static com.acme.util.Helpers.now;` (member → strip trailing member,
  resolve the class file). Handle both explicitly or drop them honestly.

**Rust (pack `[rust]`)** — the third planned pack. Two steps: `mod foo;` →
`foo.rs`/`foo/mod.rs` (builds the module tree), then `use crate::/super::/self::`
resolves against that **module tree**, not the raw filesystem. External crates
(`use serde::…`) → drop. More involved than JS relative paths.

**Not planned (possible later, on demand only):** Go (import path = full module
path; resolves to a package = *directory*, a structural mismatch with
`ImportEdge.to_path` that would need a decision first), C/C++ (`.h`↔`.c` pairing),
Ruby, C#. Their grammars load fine against core 0.26 (see the table) — but no
resolver is committed for them here.

Cycle *interpretation* is language-specific (scoring in `cycles.py` is not):
Python cycle = real smell; JS/TS cycle = common/benign. Worth a render note.

---

## Recommended build order

1. **Refactor first** (`parser.py` → `parsers/` package, Python moved verbatim,
   suite green). No behavior change — de-risks everything after.
2. **JS/TS pack** — the first pack. Chosen first for a concrete reason unrelated
   to tiering: it removes a *live* facade defect (`SUPPORTED_LANGUAGES` claims
   JS/TS, `parse_file` returns empty today) *and* delivers real edges for the
   most repos. This is also the PR that proves the whole opt-in-extra mechanism
   end to end, including the ImportError-degradation path on a base install.
3. **Java pack**, then **Rust pack**. These are the planned packs. None is
   privileged over the others; each is the same shape of work (a new in-tree
   resolver + its extra + fixtures). Further languages (Go, C, …) only on demand
   — nothing here commits to them.

---

## Forced refactor (not stylistic)

`graphlm/parser.py` is **645 lines** — already over the 600-line house limit
before adding anything. Splitting into `graphlm/parsers/` is **required**:

- `parsers/base.py` — registry-driven `_TreeSitterBackend`, `ParsedFile`,
  `ImportEdge` plumbing, resolution helpers shared across languages.
- `parsers/python.py` — Python resolver moved **verbatim**.
- `parsers/javascript.py`, `parsers/java.py`, then pack modules.
- `parser.py` stays as a thin re-export shim so existing imports
  (`from graphlm.parser import build_dependency_graph`) keep working.

### Registry, not `if` ladder
`_get_language` becomes a table lookup keyed on
`(pip_module, accessor_fn_name)` — TS/TSX as two entries — that **degrades on
`ImportError`** rather than raising, so a pack language whose wheel isn't
installed yields no edges (one log line) instead of crashing the run. This is the
load-bearing mechanism for the whole Python-core / opt-in-pack design.

---

## Packaging: Python in the base, every other language an extra

```toml
[project]
dependencies = [
  # …existing…
  "tree-sitter>=0.26,<0.27",
  "tree-sitter-python>=0.25,<0.26",       # the ONLY grammar in the base install
]

[project.optional-dependencies]
# Planned packs (in build order):
js   = ["tree-sitter-javascript>=0.25,<0.26", "tree-sitter-typescript>=0.23,<0.24"]
java = ["tree-sitter-java>=0.23,<0.24"]
rust = ["tree-sitter-rust>=0.24,<0.25"]
all  = ["graphlm[js,java,rust]"]
# (Go / C / Ruby / C# extras would be added here only if those packs are ever built.)
```

Only `tree-sitter-python` is a base dependency — the base install stays as lean
as it is today. Every other grammar is behind an extra, so a user opts into
exactly the languages they need (`graphlm[js]`, or `graphlm[all]`). Pin windows
are illustrative — set from current versions at implementation time; grammar
*loading* against core 0.26 was verified for all of these, exact upper bounds
are judgment. `graphlm --version` / a `--languages` flag could report which
grammars are actually importable, so users see what's active in their install.

---

## Touch list (for the eventual implementer)

- **`parser.py` → `parsers/` package** — split above; registry with
  ImportError degradation; extend `EXT_TO_LANGUAGE` / `SUPPORTED_LANGUAGES`;
  `parse_file` dispatch; `build_dependency_graph`: replace the `!= PYTHON` filter
  with group-by-language → per-language resolve → merge.
- **`models.py` + `diff.py`** — `ImportEdge.kind` carries Python's
  `"import"`/`"from"`. The diff's edge identity key is `(from,to,kind)` and
  DECISIONS.md decision 3 makes those keys the **contract**. Decide what `kind`
  holds per language (JS: `"import"`/`"require"`; Java: `"import"`/`"static"`/
  `"wildcard"`?) before shipping — it changes what counts as the same edge.
- **`cycles.py`** — mixed-language edge set is fine mechanically; add the
  language-specific interpretation note to the render.
- **`scanner.py`** — `_SOURCE_EXTS` already covers targets; confirm `_rank_file`
  ranks new source above docs the same way (#19). Likely no change.
- **`CLAUDE.md`** — the "Only Python is fully implemented; JS/TS are recognized
  by extension but return empty `ParsedFile`" claim moves in lock-step.
- **`DECISIONS.md`** — new ADR: Python-core / opt-in-pack model (every non-Python
  language is a pip extra with a bundled resolver, no plugin API), registry +
  ImportError degradation, under-resolve-honestly rule. (ADR material *once
  approved*.)
- **Tests / fixtures** — all four fixture trees are Python. Per-language
  fixtures needed; per CLAUDE.md **extend a fixture, don't mock file I/O.** A
  cyclic fixture per language is the cheapest correctness check. Add a test that
  asserts graceful degradation when a pack grammar is *not* installed.

---

## Small in-scope defect found while researching

`SUPPORTED_LANGUAGES` advertises `javascript`/`typescript`, but `parse_file`
returns an empty `ParsedFile()` for them — a caller cannot distinguish "parsed,
no imports" from "not implemented." No live-pipeline bug
(`build_dependency_graph` filters by language first), but `parse_file` is public
surface with no internal caller and currently misleads. The JS/TS work fixes it;
if deferred, the honest interim is to drop JS/TS from
`SUPPORTED_LANGUAGES`/`EXT_TO_LANGUAGE` until they're real.

---

## Out of scope for v1 (say no explicitly)

- Runtime plugin / entry-point API for third-party resolvers (rejected by owner
  decision — bundled resolvers only).
- tsconfig `paths`/`baseUrl` aliases and package `exports` maps.
- node_modules / GOPATH / cargo-registry / Maven-repo resolution (all → drop,
  like stdlib).
- Cross-language edges (Python → compiled Rust ext, FFI). Edges stay within one
  language.
- Non-import structure (JS/TS `export`, Rust `pub`, call graphs). The Python
  parser extracts some of these but they aren't in the deterministic edge table;
  adding them per-language is a separate effort.
