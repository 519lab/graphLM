# Handoff: start Phase 1 (JavaScript / TypeScript pack)

**Written:** 2026-08-31, end of the session that shipped Phase 0.
**Status:** Implemented on `feat/js-ts-pack` (Phase 1 of #42). Keep as the
rationale for the pack; do not re-run as a greenfield handoff.
**Baseline:** `main` @ `e0024e6` (Phase 0 merged). Everything below assumes a
fresh session starting from a clean, synced `main`.
**Epic:** #42. **Plan of record:** `docs/plans/multi-language-implementation.md`
(read the "Phase 1" section — this handoff supplements it, does not replace it).

---

## Where things stand (done)

- **Phase 0 merged** (PR #46, squashed as `250d7e3`): `graphlm/parser.py` split
  into a `graphlm/parsers/` package. `parser.py` is now a thin re-export shim.
  Pure refactor, reviewer verdict SHIP, verbatim Python move proven byte-
  identical over the pristine tree.
- **Config fix merged** (PR #47, `e0024e6`, closed #45): unrelated `.env`
  resolution bug — irrelevant to Phase 1, just noted so you're not surprised by
  it in the log.
- **404 tests pass on main; mypy clean.** Deterministic-edge count on this repo
  is now **57** (was 51 pre-Phase-0) purely because graphlm scans its own 3 new
  `parsers/` files — not a regression. Use the *pristine-tree* method if you need
  a stable number.

## First step in the new session

```bash
cd graphLM   # your checkout
git fetch origin && git checkout main && git pull --ff-only   # confirm at e0024e6+
git checkout -b feat/js-ts-pack        # Phase 1 branch
uv run pytest -q                        # confirm 404 green before starting
```
Confirm `gh api user --jq .login` is `ggrace519`.

---

## The integration seam Phase 1 plugs into (real, verified — not guessed)

Phase 0 already built the registry. A new language pack is: **one new module
`graphlm/parsers/javascript.py`** that registers a resolver, plus wiring. The
contract is in `graphlm/parsers/base.py`:

- **`_Resolver` dataclass** (base.py:232) — the surface the dispatch calls:
  ```python
  parse_file:          Callable[[bytes, Path], ParsedFile]
  imports_from_source: Callable[[bytes, Path], list]      # language import carriers
  source_roots:        Callable[[set[str]], tuple[str, ...]]
  resolve:             Callable[..., list[str]]            # -> resolved target paths
  edge_kind:           Callable[..., str]                  # ImportEdge.kind for a carrier
  ```
  `python.py` is the worked reference — copy its shape.
- **Register** with `_register_resolver("javascript", _Resolver(...))` at module
  scope in `javascript.py` (mirrors `python.py`).
- **Make it load**: add `from graphlm.parsers import javascript` to
  `_ensure_resolvers()` (base.py:251) alongside the python import. (Function-local
  import — that's how the base↔lang cycle is avoided; don't move it to module
  scope.)
- **Grammar registry**: add entries to `_GRAMMARS` (base.py:73). Remember TS/TSX:
  `tree_sitter_typescript` has NO bare `language()` — it exposes
  `language_typescript()` and `language_tsx()`. `_GrammarSpec` is
  `(pip_module, accessor)`. Grammar selection must key on the **file suffix**
  (`.tsx` → `language_tsx`), because `detect_language` maps both `.ts` and `.tsx`
  to `"typescript"` (see the plan's "tsx-selection mechanism" section for the
  approved `_GRAMMARS` shape that takes suffix).
- **Extensions**: `.js/.jsx/.ts/.tsx` are already in `EXT_TO_LANGUAGE` and
  `SUPPORTED_LANGUAGES` (base.py:30) — they were the facade. Phase 1 makes them
  real. Confirm `parse_file(Path("x.ts"))` goes from returning empty `ParsedFile`
  to returning real imports.

## Packaging (Phase 1 is the FIRST pack — this proves the whole extra mechanism)

- Add `[project.optional-dependencies]` to `pyproject.toml`:
  `js = ["tree-sitter-javascript>=0.25,<0.26", "tree-sitter-typescript>=0.23,<0.24"]`
  and start `all = ["graphlm[js]"]`. **Not** base deps — only
  `tree-sitter-python` is base.
- Grammar-loading against core `tree-sitter` 0.26 was already verified for both
  wheels (see `docs/plans/multi-language-support.md` feasibility table).
- CI: the default `uv sync --group dev` job runs WITHOUT the extra (covers the
  grammar-absent degradation path). ADD a new job WITH `graphlm[js]` installed
  (covers the enabled path). Don't add a redundant no-extra job — that's the
  default.

## Resolution scope for v1 (from the plan — say no explicitly)

- Relative specifiers only (`./foo`, `../bar`), with extension-probe order
  `.ts .tsx .js .jsx .mjs .cjs` then `<spec>/index.*`.
- Bare specifiers (`react`) → node_modules → **drop** (== Python stdlib).
- OUT of scope v1: `tsconfig.json` `paths`/`baseUrl` aliases, package `exports`
  maps. When any bare/aliased specifier is dropped, the resolver is
  *known-partial* → trip the non-exhaustive framing.
- `edge_kind`: `"import"` for import/export-from and dynamic `import()`,
  `"require"` for `require()`. `(from,to,kind)` is the diff identity contract —
  document the choice in DECISIONS.md.

---

## THREE carry-forward items from the Phase 0 review — fold into Phase 1

These are latent gaps Phase 0 inherited from main's existing behavior (not Phase 0
bugs). A second real resolver is exactly what exposes them, so fix them here:

1. **The "log once per language" grammar-unavailable dedupe is currently dead.**
   `python.py`'s `imports_from_source`/`parse_file` wrap `_backend.parse_source`
   in a broad `except Exception`. `_GrammarUnavailable` is a plain `Exception`, so
   a missing grammar is caught THERE (per-FILE "Tree-sitter parse failed"
   warning) and never reaches the once-per-language dedupe
   (`_warn_grammar_unavailable`, base.py). The JS/TS resolver must let
   `_GrammarUnavailable` propagate to the dispatcher's handler — e.g. in the
   resolver's try/except add `except _GrammarUnavailable: raise` BEFORE the
   generic `except Exception`. (Consider retrofitting python.py the same way in
   this PR for consistency, since it's the reference template Java/Rust will copy.)

2. **The degradation test must have TEETH on the dedupe.** Phase 0's
   `TestMissingGrammarDegrades` asserts `len(grammar_warnings) == 1` against a
   SINGLE fragment — vacuous (one file can't distinguish once-per-language from
   once-per-file). Phase 1's grammar-absent test must include **two** fragments
   of the missing-grammar language and assert exactly ONE warning.

3. **`resolver.source_roots()` sits outside the `_GrammarUnavailable` guard**
   (base.py ~388, above the try). Pure for Python. But a pack whose
   `source_roots` touches the grammar would escape into `__init__.py`'s
   `except Exception → deterministic_edges=None` and poison the whole run. Keep
   the JS/TS `source_roots` grammar-free (preferred — it shouldn't need the
   grammar), OR move the call inside the guard. Note the decision.

## Acceptance (Phase 1)

- Deterministic edges on a real TS repo go 0 → >0. Measure with a direct
  `build_dependency_graph` call (NOT `--dry-run`, which prints the empty LLM
  `import_edges`). Baseline: `../emberfall-game` = 0 today. Record the number.
- `parse_file(Path("x.ts"))` returns real imports for a file with imports.
- Grammar-absent on a base install: JS/TS repo → 0 edges + ONE log line, no
  crash. Mixed Python+TS with `[js]` absent → Python edge count unchanged AND
  `deterministic_edges is not None` (the never-escapes invariant, with teeth).
- New `tests/fixtures/ts_project/` fixture (relative-import chain, an `index.ts`
  barrel, a `.tsx` file, a bare import that must be dropped, a 2-node cycle).
  Assert the exact edge set. Per repo convention: extend/add a fixture, don't mock
  file I/O.
- CLAUDE.md language claim updated in lock-step: the "Only Python is fully
  implemented; JS/TS … return empty `ParsedFile`" line becomes false — fix it.
- CHANGELOG `[Unreleased] / Added` entry (stakeholder-led). DECISIONS.md ADR for
  the pack model + `kind` values (if not already added).
- `uv run pytest -q` green, `uv run mypy graphlm --ignore-missing-imports` clean.

## How to run it (the session pattern that worked for Phase 0)

Sequential, one implementer + one fresh reviewer, verify empirically yourself:
1. Implementer subagent (Claude, not codex/grok — see the
   `frontier-cli-delegation-unreliable` memory) on `feat/js-ts-pack` with a
   self-contained brief built from this handoff + the plan's Phase 1 section.
2. YOU verify: tests, mypy, the 0→>0 edge count on a real TS repo, the
   degradation invariant with teeth.
3. Fresh-context reviewer subagent on the diff (loop-review; never grade your own
   work).
4. Fix, then STOP and ask before pushing (Greg's gate). PR references #42.

Do NOT start Phase 2 (Java) until Phase 1 is merged and green.
