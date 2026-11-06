# Implementation plan: Multi-language AST support

**Status:** Phase 0–3 implemented (JS/TS `graphlm[js]`, Java `graphlm[java]`, Rust `graphlm[rust]`). Further packs only on demand.
**Tracking issue:** [#42](https://github.com/519lab/graphLM/issues/42).
**Design baseline:** `docs/plans/multi-language-support.md` (approved 2026-08-30).
**Repo state at write time:** `main` @ `e5ebf92`.

Owner decisions this plan implements:
1. **Python is the only core language** — the sole grammar in the base install,
   the only language with a resolver today. Every other language is an **opt-in
   pip extra** (`graphlm[js]`, `graphlm[java]`, … `graphlm[all]`) with a
   **bundled, graphlm-authored, in-tree** resolver — no plugin API, no
   third-party code. A missing grammar → zero edges for that language, not a
   crash.
2. **No non-Python resolver exists yet.** JS/TS are a facade returning an empty
   `ParsedFile`. Every phase below *builds* a resolver from scratch; none flips
   on something already there. There is no privileged non-Python language —
   JS/TS is simply the first pack (for the reason in Phase 1), Java and the rest
   follow by demand.

Each phase below is an independent PR that keeps `uv run pytest -q` green and
references #42. **Do not start a phase until the prior one is merged and green.**

---

## Compatibility contract (must hold across every phase)

Real consumers of `graphlm.parser`, verified in-tree:

- `graphlm/__init__.py:36` → `from graphlm.parser import build_dependency_graph`
- `tests/test_parser.py:10` → `ParsedFile, build_dependency_graph,
  detect_import_cycles, detect_language, parse_file`
- `tests/test_parser.py:305` → `from graphlm.parser import _source_roots`

Therefore **`graphlm.parser` must keep exporting**:
`ParsedFile`, `build_dependency_graph`, `detect_import_cycles`,
`detect_language`, `parse_file`, `_source_roots`.

After Phase 0 these become re-exports from the `parsers/` package. Existing tests
must pass **unmodified** through Phase 0 — that is the proof the refactor changed
no behavior.

`ImportEdge.kind` already documents `'import' | 'from' | 'register' | 'include'
| 'uses'` (models.py:16-18), so the model needs no change to admit new kinds —
but see Phase 2/3 on choosing kind values (they are part of the diff contract).

---

## Phase 0 — Refactor: `parser.py` → `parsers/` package (no behavior change)

**Goal:** split the 645-line `parser.py` (over the 600 limit) into a package,
moving Python's resolver **verbatim**. Zero behavior change. This de-risks
everything after it.

**Layout:**
```
graphlm/parsers/
  __init__.py       # re-exports the public contract above
  base.py           # registry-driven _TreeSitterBackend, ParsedFile,
                    #   shared resolution helpers, _dedupe_edges,
                    #   detect_import_cycles, the group-by-language dispatch
  python.py         # everything Python-specific, moved verbatim:
                    #   _PY_*_QUERY, _parse_python_*, _module_candidates,
                    #   _source_roots, _resolve_module_name, _resolve_import,
                    #   _placeholder_edge, _parse_file_python
graphlm/parser.py   # thin shim: `from graphlm.parsers import *`  (+ explicit
                    #   re-export of the contract names, incl. _source_roots)
```

**Registry (the one real change to structure, still behavior-neutral):**
Replace `_get_language`'s `if language == PYTHON` ladder with a table:

```python
# base.py
@dataclass(frozen=True)
class _GrammarSpec:
    pip_module: str      # e.g. "tree_sitter_python"
    accessor: str        # e.g. "language" ; TS uses "language_typescript"

_GRAMMARS: dict[str, _GrammarSpec] = {
    "python": _GrammarSpec("tree_sitter_python", "language"),
}

def _get_language(self, language: str):
    if language in self._language_cache:
        return self._language_cache[language]
    spec = _GRAMMARS.get(language)
    if spec is None:
        raise ValueError(f"Unsupported language: {language}")
    try:
        mod = importlib.import_module(spec.pip_module)
    except ImportError:
        raise _GrammarUnavailable(language)   # NOT ValueError — caller degrades
    lang = self._ts.Language(getattr(mod, spec.accessor)())
    self._language_cache[language] = lang
    return lang
```

`_GrammarUnavailable` is caught by `parse_file` / `build_dependency_graph`, which
skip that file and log **once per language** (dedupe the warning). In Phase 0
only `python` is registered, so this path is exercised by a unit test that
registers a fake missing language.

**Invariant — `_GrammarUnavailable` must never escape `build_dependency_graph`
(load-bearing).** `__init__.py:229-234` wraps the entire `build_dependency_graph`
call in `except Exception → warn → deterministic_edges = None`, and `diff.py`
decision 5 reads `None` as "AST was off" (sets the AST dimension `compared=False`).
So if a single uninstalled *pack* grammar let the exception escape, it would not
degrade that one language — it would **zero every language's edges** for the run
*and* make the diff report the AST dimension as not-compared on a run where AST
was on. That is exactly the silent collapse decisions 4/5 exist to prevent.
Therefore:
- Per-language grammar failure contributes **zero edges for that language** and
  leaves other languages' edges intact.
- With `ast=True`, `build_dependency_graph` **returns a list, never `None`** — a
  missing grammar is not an error at this layer. (`None` for
  `deterministic_edges` remains reserved for `ast=False`, per decision 5.)
This invariant is tested in Phase 3 (mixed-language, grammar absent), not just the
pure-single-language case — see there.

**Acceptance:**
- `uv run pytest -q` — all ~395 tests pass **unmodified**.
- `uv run mypy graphlm --ignore-missing-imports` clean.
- Deterministic-edge count on this repo is unchanged (**51**). **Do NOT** measure
  this via `graphlm . --dry-run` — that line prints the LLM's `import_edges`
  (empty in dry-run), not `deterministic_edges`. Measure it directly:
  ```bash
  uv run python -c "
  from pathlib import Path
  from graphlm.parser import build_dependency_graph
  from graphlm.scanner import scan_project
  s = scan_project(Path('.'), max_files=200)
  print(len(build_dependency_graph(s.file_fragments, project_dir=Path('.'), max_files=200)))
  "  # expect 51
  ```
- `git grep 'from graphlm.parser import'` still resolves.
- New test: a registered-but-uninstalled language yields `[]` edges + one log
  line, no exception (see the never-escapes invariant below).

**Commit:** `refactor(parser): split into parsers/ package (Python verbatim)`

---

## The resolver interface (introduced in Phase 0, filled in later phases)

Each language module implements a small, uniform surface so `base.py`'s dispatch
is language-agnostic:

```python
# each parsers/<lang>.py provides:
LANGUAGE: str                       # "javascript"
EXTENSIONS: dict[str, str]          # {".js": "javascript", ...}  (feeds EXT_TO_LANGUAGE)
GRAMMAR: _GrammarSpec               # registered into _GRAMMARS
def extract_imports(tree, path) -> list[_RawImport]: ...
def resolve(imp: _RawImport, from_path: str, known: set[str],
            ctx: _RepoContext) -> list[tuple[str, str]]:  # [(to_path, kind), ...]
    ...
```

- `_RawImport` is a language-neutral carrier (the specifier text + a per-language
  payload). Python's existing `_ParsedImport` becomes Python's payload type.
- `_RepoContext` is computed **once per run** in `build_dependency_graph` and
  passed down: `known` file set, per-language source roots, and any manifest data
  a resolver needs (Java: source-root names; Rust: nothing beyond the mod tree).
  Keeps per-file resolution pure and cheap.
- **`resolve` must never raise.** Return `[]` on anything unresolvable. A missing
  edge is always preferred to a false one (#19).

`build_dependency_graph` becomes: group fragments by `detect_language`, build the
`_RepoContext` per language, resolve, merge, dedupe. The `!= PYTHON` filter is
gone; unregistered/unavailable languages simply contribute nothing.

---

## Phase 1 — JavaScript / TypeScript pack (the first pack; proves the extras mechanism)

The **first opt-in pack** (`graphlm[js]`) and the phase that proves the whole
Python-core / opt-in-extra machinery end to end — including the
grammar-absent degradation path on a base install (see the mixed-language
degradation test, moved here from what used to be a later phase). Chosen as the
first pack for a concrete reason unrelated to any tier: it removes a **live**
facade defect (`SUPPORTED_LANGUAGES` claims JS/TS, `parse_file` returns empty
today) **and** delivers real edges for the most repos.

**Packaging:** add `[project.optional-dependencies]` with
`js = ["tree-sitter-javascript…", "tree-sitter-typescript…"]` and start the
`all = ["graphlm[js]"]` aggregate. The grammars are **not** base deps — only
`tree-sitter-python` is. On a base install (no `[js]`), a JS/TS repo must yield
zero edges + one log line, never a crash.

**Grammars — and the tsx-selection mechanism (name it, don't hand-wave).**
`detect_language` maps **both** `.ts` and `.tsx` to `"typescript"`
(`tests/test_parser.py:34-35` asserts this and must keep passing). So a registry
keyed by *language name* alone cannot pick `language_tsx()` vs
`language_typescript()` — the name is identical. **Decision:** grammar selection
takes the **file suffix**, not just the language name. Concretely, make the
registry value able to choose by suffix:

```python
# base.py — grammar selection is (language, suffix) -> _GrammarSpec
_GRAMMARS: dict[str, _GrammarSpec | Callable[[str], _GrammarSpec]] = {
    "python":     _GrammarSpec("tree_sitter_python", "language"),
    "javascript": _GrammarSpec("tree_sitter_javascript", "language"),  # .js/.jsx
    "typescript": lambda suffix: _GrammarSpec(
        "tree_sitter_typescript",
        "language_tsx" if suffix == ".tsx" else "language_typescript",
    ),
}
def _get_language(self, language: str, suffix: str = ""):
    entry = _GRAMMARS.get(language)
    spec = entry(suffix) if callable(entry) else entry
    ...  # cache key is (language, spec.accessor), not language alone
```

The `parse_file` / `build_dependency_graph` dispatch already has the path, so it
passes `path.suffix` down. `.jsx` uses the javascript grammar (it handles JSX).
Keep `EXT_TO_LANGUAGE`'s public names (`javascript`/`typescript`); the tsx split
lives entirely inside grammar selection. **Cache by `(language, accessor)`** so
TS and TSX are cached separately.

**Extraction:** `import ... from "spec"`, `export ... from "spec"`,
`require("spec")` (call expr), and dynamic `import("spec")`. Capture the string
literal specifier.

**Resolution (relative specifiers only — v1 scope):**
- Only specifiers starting `.` / `..` resolve; bare specifiers (`react`) → drop
  (node_modules rule, == Python stdlib).
- Probe order: `<spec>`, then `<spec>.{ts,tsx,js,jsx,mjs,cjs}`, then
  `<spec>/index.{ts,tsx,js,jsx,mjs,cjs}`. First hit in `known` wins.
- **Explicitly out of scope (drop, don't half-resolve):** `tsconfig.json`
  `paths`/`baseUrl` aliases, package `exports` maps. When any bare/aliased
  specifier is dropped, the resolver is *known-partial* → trip the "not
  exhaustive" framing (see "Honesty" below).

**kind values (diff-contract decision):** use `"import"` for import/export-from,
`"require"` for `require()`, `"import"` for dynamic `import()`. Document in
DECISIONS.md — `(from,to,kind)` is the diff identity key.

**Fixture:** `tests/fixtures/ts_project/` with a relative-import chain, an
`index.ts` barrel, a `.tsx` file, a bare import (must be dropped), and a 2-node
cycle. Assert exact edge set + that the bare import produced no edge.

**Acceptance:**
- Deterministic edges on a real TS repo go from 0 to **>0**. Measure with the
  direct `build_dependency_graph` call (as in Phase 0), **not** `--dry-run`
  (which prints the empty LLM `import_edges`). Record the number in the PR —
  baseline `emberfall-game` = 0.
- `parse_file(Path("x.ts"))` no longer returns empty for a file with imports.
- **Grammar-absent degradation on a base install** (this is the first pack, so
  the extras path is proven here): with `[js]` **not** installed, a JS/TS repo
  yields 0 edges + one log line, no crash.
- **Mixed-language degradation (the invariant test, not the pure case).** A
  **Python + TS** tree with the `[js]` grammars **absent** must yield the
  *unchanged* Python edge count (equal to the Python-only baseline) **and**
  `deterministic_edges is not None`. The pure "TS-only → 0 edges" case cannot
  distinguish the poison-the-whole-run bug (see the never-escapes invariant in
  Phase 0) from correct degradation; this one can.
- CI: the **default** `uv sync --group dev` job runs *without* the extra and
  covers the degradation path. Add a **new** job *with* `graphlm[js]` installed
  to cover the enabled path. (Don't add a redundant no-extra job — that's the
  default.) Later packs reuse this two-job pattern.
- mypy clean; full suite green.

**Commit:** `feat(parser): JavaScript/TypeScript language pack via optional extra (#42)`

---

## Phase 2 — Java pack

Verified tractable: `import com.acme.User;` → `com/acme/User.java` — a
FQN→file model, Python's analog, **no** directory-as-target problem.

**Packaging:** `java = ["tree-sitter-java…"]` extra (not a base dep); extend
`all`. Same two-CI-job pattern as Phase 1.

**Grammar:** `java` → `(tree_sitter_java, language)`. Add `.java` to
`EXT_TO_LANGUAGE`/`_SOURCE_EXTS` if not already (it's in `_SOURCE_EXTS`).

**Extraction:** `import_declaration` nodes → `scoped_identifier`. Distinguish:
- normal: `import com.acme.User;`
- wildcard: `import com.acme.*;` (has `asterisk` child)
- static: `import static com.acme.Helpers.now;` (has `static` child)

**Resolution:**
- **Source roots** are the #19 problem in Java clothing: reuse the
  `_source_roots` *pattern* with Java root names — accept a prefix ending in
  `src/main/java`, `src/test/java`, or `src` that directly contains the package.
  The file's own `package com.acme.service;` declaration is a **disambiguator**:
  a file at `src/main/java/com/acme/service/Foo.java` declaring
  `package com.acme.service` confirms the root — use it to reject false roots.
- normal import → `<root>/com/acme/User.java` in `known`.
- static import → strip the trailing member, resolve the class file.
- wildcard → the package is a **directory**; resolve to each `.java` directly in
  that dir that's in `known` (a bounded fan-out), OR drop if that's too noisy —
  **decide and document.** Recommend: resolve to files in the immediate package
  dir only (no recursion), kind `"import"`.
  - **Diff-churn consequence (decide with this in view):** because the edge
    identity key is `(from,to,kind)` (decision 3), resolving a wildcard to every
    `.java` in the package means **adding one file to a package mutates the edge
    set of every wildcard importer** — a structurally-correct change that reads
    as spurious diff noise in `GRAPH_DIFF.md`. This is an argument for *dropping*
    wildcards (and tripping the non-exhaustive framing) rather than fanning them
    out. Make the call knowing this; don't discover it in the first noisy diff.

**kind values:** `"import"` (normal), `"static"` (static import). Wildcard edges,
if kept, use `"import"`. Document.

**Fixture:** `tests/fixtures/java_project/src/main/java/com/acme/...` with a
normal import, a static import, a wildcard, a cross-package edge, and a cycle.

**Acceptance:** exact edge set on the fixture, incl. correct root-stripping;
static/wildcard handled per decision; grammar-absent degradation holds (base
install → 0 Java edges, no crash, Python edges intact); suite green; mypy clean.

**Commit:** `feat(parser): Java language pack via optional extra (#42)`

---

## Phase 3 — Rust pack

The third pack. By this point the extras mechanism is already proven (Phase 1),
so this is "another resolver, same template" — no new machinery.

**Packaging:** add `rust = ["tree-sitter-rust…"]` and extend `all` to include it.
Grammar is **not** a base dep.

**Rust specifics:**
- Two-step resolution: `mod foo;` declarations map to `foo.rs` / `foo/mod.rs`
  (relative to the declaring file's directory), building the **module tree**;
  then `use crate::/super::/self::…` paths resolve **against that module tree**,
  not the raw filesystem. This is more involved than JS relative paths — budget
  for it.
- `crate::` = crate root (`src/lib.rs` or `src/main.rs`); `super::` = parent
  module; `self::` = current module. External crates (`use serde::…`) → **drop**
  (same rule as Python third-party / JS node_modules).
- Under-resolve honestly: if the mod-tree walk is incomplete, trip the
  non-exhaustive framing rather than emitting guesses.

**Acceptance:**
- With `graphlm[rust]` installed, the Rust fixture yields the expected edge set;
  external-crate `use`s produce no edge.
- On a base install (no `[rust]`), a Rust repo yields 0 edges + one log line, no
  crash (the degradation path — already covered generically by the Phase 1
  invariant test, re-asserted here for Rust).

**Commit:** `feat(parser): Rust language pack via optional extra (#42)`

Further packs (Go, C, Ruby, C#, …) follow the same template **only on demand** —
none is planned here.

---

## Honesty when a resolver is known-partial

Reuse the mechanism `context.py` already has: when the edge table is capped, its
framing flips to "not exhaustive — infer the rest." A resolver that drops
bare/aliased specifiers (JS without tsconfig aliases, Rust with an incomplete
mod-tree walk) is *known-partial*. Surface a per-language "partial" signal from
`build_dependency_graph` so the pass-2 edge block uses the non-exhaustive framing
for those languages, even when the table isn't size-capped. This keeps a partial
list from being presented to the model as complete ground truth (the #19 rule at
the prompt layer).

---

## Cross-cutting, do once (final phase or folded into Phase 1)

- **`cycles.py` render note:** cycle *interpretation* is language-specific
  (Python cycle = smell; JS/TS = often benign) though scoring isn't. Add a
  one-line qualifier in the rendered cycle section keyed on the languages
  involved.
- **`CLAUDE.md`:** replace "Only Python is fully implemented; JS/TS are
  recognized by extension but return empty `ParsedFile`" in **lock-step** with
  the phase that makes it false. Document the Python-core / opt-in-pack model,
  the extras, and the registry/degradation contract.
- **`DECISIONS.md`:** ADR — Python-core / opt-in-pack model (every non-Python
  language is a pip extra with a bundled resolver, no plugin API), registry +
  ImportError degradation, per-language `kind` values as diff contract,
  under-resolve-honestly rule.
- **`CHANGELOG.md`:** an `[Unreleased] / Added` entry per phase, stakeholder-led
  ("graphlm now extracts verified import edges for TypeScript projects…").
- **`README` / `--help`:** document `graphlm[all]`, the extras, and (optional) a
  `--languages` diagnostic listing which grammars are importable.

---

## Risks & how this plan retires them

- **False edges** (the #19 lesson): every resolver starts under-resolving,
  `resolve` never raises, partial resolvers trip the non-exhaustive framing.
  Per-language cyclic fixtures with **exact** expected sets are the guard.
- **ABI breakage on grammar upgrade:** loading was verified against core 0.26 for
  all candidates; pin windows conservatively; the degradation path means a
  future incompatible grammar fails soft (zero edges), not hard.
- **Refactor regressing Python (#19):** Phase 0 moves Python **verbatim** with
  the existing suite unmodified as the proof; no language is added in the same PR.
- **Diff churn from `kind`:** `kind` values are fixed per language and documented
  as contract before the first non-Python edges ship, so a later change is a
  conscious contract change, not drift.
