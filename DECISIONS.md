# Architecture Decision Records

Significant, hard-to-reverse decisions for graphLM. Newest first.

---

## ADR-017 — Embeddable library: injected LLM backend, structure-only maps, no config read at import

**Status:** Accepted (2026-11-05)

**Context.** graphLM is being embedded in an agent host (a Hermes Agent plugin). The host already owns model access, credentials and billing, and runs many plugins in one long-lived process. Three things blocked that:
1. The only transport was graphLM's own HTTP client configured from `GRAPHLM_*`, so a host had to hand graphLM a second set of credentials.
2. `import graphlm` ran `load_dotenv(~/.config/graphlm/.env)`. That copied graphLM's config into the host process's environment, where every other component could see it. Host plugin policies (Hermes catalog rule 11) forbid reading `.env` files at all.
3. The cheapest useful map (tree + AST edges + cycles) existed only as a side effect of `--dry-run`, labelled "DRY RUN" and never written.

**Decision.**
- `generate_graph(llm=...)` accepts an `LLMBackend`: a callable taking `(system_prompt, user_prompt, *, json_schema, schema_name, timeout, max_output_tokens)` and returning `LLMReply(text, usage)`. graphLM still builds both prompts and still parses and validates the reply through one function, `llm.parse_reply`, which the HTTP client also uses. A backend moves text and cannot change what counts as a valid graph. `usage` uses OpenAI field names so it lands in `meta.usage` unchanged.
- The user `.env` is read by `config.load_user_env()`, never at import. Callers are the CLI (at startup) and `generate_graph` only when graphLM owns the model call (no `llm=`, not `structure_only`). Dry runs keep reading it, because `GRAPHLM_MAX_CONTEXT` shapes their stats.
- `generate_graph(structure_only=True)` is a first-class mode: no LLM, no settings, no config read, no "DRY RUN" note, and it writes when `output_dir` is set. `structure.structure_graph` builds it and dry-run reuses it.
- The renderer's node/link model moved from private `html_render` helpers to public `graphlm.view` (`graph_view`, `build_nodes`, `build_links`, `directory_color`, `PALETTE`). GRAPH.html now embeds `graph_view()`, so every renderer draws the same data.

**Consequences.**
- A library caller that relied on import-time `.env` loading, then read `GRAPHLM_*` from `os.environ` itself, must now call `graphlm.config.load_user_env()`. graphLM's own entry points are unaffected.
- The private `html_render._build_nodes`, `_build_links` and `_directory_color` are gone; there are no aliases. Their tests now import from `graphlm.view`.
- Streaming, the 400 fallback and retries are HTTP-client behaviours. A backend gets one bounded call per pass, and retry policy is the host's.

---

## ADR-016 — Agent-facing map: label test code, JSON opt-in, fresh-run cleanup

**Date:** 2026-09-23
**Status:** Accepted — implemented (`testpaths`, `cycles_render`, `orientation`, `degree`, `freshness`, `Cycle.test_only`, `--json`)

### Context

Three related improvements to how useful the map is to a coding agent (the
primary consumer — though humans operate those agents and verify the map first,
so signals carry to **all** outputs). Prompted by external suggestions plus
dogfooding: graphlm's own map reported its deliberately-cyclic parser *fixtures*
as if they were production import cycles, and its highest-signal sections sat
hundreds of lines down behind the directory tree.

### Decisions

1. **Label test code; do not exclude it.** A cycle whose every member is a test
   file (`testpaths.is_test_path`, one predicate shared by scanner/render/query/
   mermaid/html) is flagged `Cycle.test_only` and rendered distinctly — `GRAPH.md`
   groups them under `## Import Cycles` (production first), Mermaid outlines them
   muted amber not red, HTML mutes test nodes, MCP `cycles` reports the split.
   Tests stay in the map; `--no-tests` still drops them. **Invariant: labelled
   test ⇔ dropped by `--no-tests`** (pinned by a test). An earlier attempt to
   `.graphlmignore` the fixtures was reverted — it deleted a quarter of the map.

2. **Orientation block at the top of `GRAPH.md`.** A compact `## Orientation`
   (`orientation.py`) — entry points, cycle counts, top fan-in — sits between the
   header and the directory tree so an agent gets the repo's shape in the first
   ~10 lines. Fan-in ("most imported / blast radius", `degree.py`, shared with
   MCP `overview`) is **never** called "importance" — raw degree ≠ architectural
   weight (ADR-015). No "critical path" (import reachability ≠ execution order).

3. **`GRAPH.json` is opt-in (`--json`); default outputs are `GRAPH.md` +
   `GRAPH.html`.** The `.md` is the agent-facing map (JSON node/link arrays fill
   context faster). graphlm always writes an internal working copy
   **`.graphlm/.graph-state.json`** (`freshness.STATE_FILENAME`, lives in
   `output_dir` beside the artifacts, scanner-excluded) so the diff has a baseline
   and `--serve` has a map. **Library default stays `json=True`** (protects
   `md, json, html = result.write(...)` and the #8 CLI/library split); only the
   **CLI** default flips.

4. **Fresh-run cleanup.** A re-run removes graphlm's *own* stale artifacts (a
   `GRAPH.json` from a prior version / flag toggle) so a fresh `.md` isn't left
   beside a stale machine-readable map. **Exact artifact names only** (derived
   from the suffix params, never a dir or glob), **symlinks skipped**, **never
   raises** (runs after the paid LLM call), **`--dry-run` deletes nothing**, and
   **gated on `owns_dir`** (a working copy or prior `GRAPH.json` exists) so a run
   into a fresh user dir (`-o docs/`) never deletes a same-named user file. All
   output-path symlink refusals happen **before** cleanup, so a half-written dir
   is impossible.

5. **ADR-002 amendment — a fourth baseline state.** When the prior map came from
   a different graphlm version (`meta.graphlm_version`, already stamped, now read
   on the write path), the diff reports **`VERSION_CHANGED`** — regenerated fresh,
   not compared — instead of a false `FIRST_RUN` / `UNCOMPARABLE`. Baseline source
   order is working copy, else prior `GRAPH.json` (first post-upgrade run),
   classified exactly as before so an absent/corrupt file never masquerades.
   `DIFF_SCHEMA_VERSION` bumped **1 → 2** (added the state + `old/new_graphlm_version`).
   The `GRAPH.md` directive names the version ("generated by graphlm X against
   commit …"); `GRAPH.html` embeds it as machine-readable `provenance`.

### Consequences

- `--no-html` / `--no-diff` / dropping `--json` now also **remove** a prior
  `.html` / `_DIFF.*` / `.json` (previously left in place).
- `--serve` reads the working copy, so it works without `--json`.
- `render.py` telemetry summaries moved to `telemetry_render.py` and the
  cycle/orientation/fan-in helpers to their own modules (600-line limit;
  `render.py` 574 → ~520). `_is_test_path` → `testpaths.is_test_path`;
  `_norm` → `pathnorm.norm_path` (no drift).
- `public_api` symbol tagging is a deliberate follow-up (its own ADR).

---

## ADR-015 — Module importance: two raw components, fused at render time

**Date:** 2026-09-21
**Status:** Accepted — implemented (`evidence.score_importance`, `ModuleDescription.role`/`degree`)

### Context

The map lists modules but not which are load-bearing. Structural import degree
(already computed for the Mermaid layout) conflates "imported a lot" (a constants
leaf) with "architecturally central" (the orchestrator). A spike showed TypeSafe/Jev
supplies the missing semantic role: a Score over a leaf→orchestrator ladder ranked
the tetris fixture perfectly, and on a live run the two signals *disagreed usefully* —
`settings.py` was degree-#1 (imported everywhere) but Jev-role-last (a leaf), while a
low-degree entry point ranked high. This is the half of the "pass 2 to Jev" idea that
worked; the refine feedback loop (a second generative pass) is a separate, still-open
question, not part of this.

### Decisions

1. **Two raw components on `ModuleDescription`, not a pre-fused float.** `role`
   (Jev, 0–3) and `degree` (structural) are stored separately in `GRAPH.json`. The
   fusion (`0.6·role + 0.4·degree_norm`, a named renderer constant) happens in
   `render.py` at display time — so the weighting can be retuned without rewriting
   every module's value, and a reader can audit each signal. Mirrors how
   `Faithfulness`/`EvidenceSupport` expose raw counts, not just a derived ratio.
   Additive-optional → schema unchanged; an old `GRAPH.json` diffs `NORMAL`.
2. **No `--no-redact` gate** (unlike `evidence.score`). Importance sends module
   *descriptions* + an integer *degree*, never file source, so redaction is
   irrelevant; gating on it would deny importance to `--no-redact` users for no
   reason. Gated on key + `typesafe` extra + `--no-importance` only.
3. **Best-effort, never costs the graph.** `score_importance` never raises (broad
   `except → None`), and `generate_graph` wraps the fill in a second guard — a Jev
   failure leaves `role`/`degree` `None`, never a fake zero, and the paid graph is
   always returned. Filled locally after pass 2, never LLM-emitted (not in the
   pass-2 instruction block).
4. **Byte-identical fallback.** When importance is off (no module scored), the
   `## Modules` table renders exactly as before (no column, path sort) — pinned by a
   golden test, not just asserted.

### Consequences

- The rendered map's Modules section changes shape when Jev is on (an Importance
  column, load-bearing-first) and is untouched when off.
- The renderer-side fusion weight is uncalibrated beyond one fixture; keeping the
  components raw means changing it later is a render change, not a data migration.
- HTML node-sizing by importance is a deliberate follow-up, not in this change.

---

## ADR-014 — Prose evidence-support scoring via TypeSafe/Jev

**Date:** 2026-09-21
**Status:** Accepted — implemented (`graphlm/evidence.py`, `meta.evidence_support`)

### Context

graphlm had an exact oracle for its import *edges* (`faithfulness.py`: LLM
`import_edges` vs. AST `deterministic_edges`) but **none** for its *prose* — the
`file_summaries` the model writes. Live spikes showed TypeSafe's Jev (a Noul →
calibrated probability) discriminates a supported summary from a hallucinated one
cleanly (real+own-source 0.78, mismatched-file 0.012, injected-fake-symbol 0.051;
12/12 real beat their corrupted twin), and even flagged a real defect (a
truncated-mid-symbol summary scored 0.23). The `graphlm[typesafe]` extra exists
(ADR-013) but nothing consumed it.

### Decisions

1. **Score prose, not edges.** A new `meta.evidence_support` (sibling of
   `faithfulness`), filled locally after pass 2, one **SOFT Noul** per
   `file_summary`. SOFT ("role + real symbols match; minor unverifiable detail
   acceptable; a wrong role or invented symbol is not") — validated to keep
   discrimination while tolerating the detail a skeletonised fragment can't
   confirm. It is a **trust weight, not a hallucination verdict**: skeleton/
   truncation lowers it by design, same framing as `faithfulness`.
2. **Score against the evidence the model saw** — `pass2_files` `frag.content`
   (redacted/skeletonised/truncated), **never** disk bytes or
   `parsers._source_bytes` (unredacted → a third-party leak, and it would score
   the file rather than the claim-vs-evidence). A summary with no pass-2 fragment
   is **skipped**, never scored against content the model never received.
3. **Off unless safe and wanted.** `None` (never a fake zero) when **any** of:
   `--no-evidence`, `--no-redact` (TypeSafe is a *different* third party than
   graphlm's own endpoint — unredacted source must not reach it), no
   `TYPESAFE_API_KEY`, the `typesafe-sdk` extra absent, or a dry run.
4. **Best-effort, never costs the graph.** It runs after the paid pass-2 call, so
   `evidence.score` never raises (broad `except → None`, like `diff.load_baseline`)
   **and** `generate_graph` wraps the fill in a second guard. A failure leaves the
   field `None`; the finished graph is always returned. Additive optional →
   `GRAPH_META_SCHEMA_VERSION` stays 1 (ADR-001).
5. **Injectable client, function-local SDK import.** `score(..., client=None)`
   lets the no-network test suite drive a fake; the `typesafe_sdk` import is
   inside the run path so a base install without the extra never ImportErrors, and
   nothing outside `evidence.py` imports it (the `mcp_server.py` rule).

### Consequences

- A user on the base install (no extra / no key) sees no change and no new
  dependency; the field is simply absent.
- Expose per-file low outliers, not just a mean — a mean of 0.81 is not
  actionable, but "`models.py` 0.23" is (it caught a real truncation).
- The `typesafe-sdk` extra is now a real dev-test dependency (the unit tests need
  the real `Noul` types); the CLAUDE.md Commands block gains its `--extra typesafe`
  line.

---

## ADR-013 — First-run setup wizard; TypeSafe is an opt-in extra

**Date:** 2026-09-21
**Status:** Accepted — implemented (`graphlm/setup.py`, `--setup` + auto-first-run on the CLI)

### Context

graphlm's capabilities are gated behind optional extras (`[js]`…`[php]`, `[mcp]`)
so the base install stays lean and vendor-neutral (bring-your-own OpenAI-compatible
endpoint). The cost is discoverability: a user never learns their JS/TS files
produce zero parser edges because the `[js]` wheel isn't installed. Adding
TypeSafe/Jev prose scoring sharpened the tension — its SDK talks to a *commercial
third-party API* and needs a key, so it must never be a base dependency forced on
every `pip install graphlm`. An earlier draft that auto-enabled it on key presence
was rejected: it designed around one developer's local machine, not the shipped
product.

### Decisions

1. **A first-run setup wizard** (`graphlm --setup`, and an automatic prompt on the
   first interactive analysis) lists the not-yet-installed optional packs and
   installs the chosen ones. Optional packs stay optional — **nothing here becomes
   a base dependency**.
2. **Reuse the upgrade installer machinery, don't reinvent it.** `setup.py` imports
   `upgrade.detect_installer` / `installed_extras` / `_abs_no_follow` / `_spec` /
   `_collapse_extras`. The install spec is the **union** of already-installed +
   selected extras (`uv tool install --force` / `uv pip install --python <exe>` /
   `pipx install --force` / `python -m pip install`), so no existing pack is dropped.
   Same #71 rule: never `Path.resolve()` the interpreter. A source checkout is
   refused (→ `uv sync --extra`).
3. **Auto-trigger is interactive-only and fires once.** Marker absent AND stdin a
   TTY → run the wizard; non-TTY (piped/CI) → print a one-line `--setup` hint and
   proceed, never blocking. A `~/.config/graphlm/.setup-done` marker (XDG-aware,
   symlink-refusing #33) records completion so it does not re-prompt.
   **A piped first run consumes the one-shot**: the non-TTY path writes the marker
   too, so a CI/piped first invocation sees the hint once and never again on that
   machine. This is deliberate — re-emitting a setup hint on every automated run
   would be noise exactly where it is least wanted; a user who wants the wizard runs
   `graphlm --setup`. If the packs were **actually installed** in an auto-trigger run
   (only possible interactively), graphlm **exits** with a "re-run" message instead of
   continuing: the forced reinstall rebuilt the venv this process runs from, so the
   new packs are not importable in-process and continuing would silently omit the
   edges the user just asked for.
4. **`run_setup` never raises past its boundary** — installer failure → manual
   command, detection crash → fall back to `pip`. A first-run helper must not block
   the actual analysis.
5. **TypeSafe ships as the opt-in extra `graphlm[typesafe]`**, NOT a core dependency
   and NOT part of `graphlm[all]` (which is languages only, like `mcp` is separate).
   The wizard offers it and, on selection, instructs adding `TYPESAFE_API_KEY` to
   `~/.config/graphlm/.env` (the only config `.env` graphlm loads — ADR-009). Nothing
   imports the SDK yet; it lands ahead of the prose evidence-scoring feature.

### Consequences

- A random `pip install graphlm` gains no new paid dependency and no vendor coupling;
  the base tool stays endpoint-neutral.
- `_union_extras` must re-append `typesafe` after `_collapse_extras` (that helper only
  knows languages/`all`/`mcp` and would silently drop an unknown extra).
- The wizard is fully injectable (`runner`/`prompt`/`which`/`home`), so
  `tests/test_setup.py` covers every installer branch with no network; a dev checkout
  detects as `source`, so CI never runs a real install.

---

## ADR-012 — `graphlm --upgrade` is a flag, not a subcommand

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/upgrade.py`, `--upgrade` on the CLI)

### Context

Users install graphlm with `uv tool install`, pipx, or pip, often with extras
(`graphlm[all]`, `graphlm[mcp]`). Asking them to remember the original installer
and extra set to get a new release is friction. A `graphlm upgrade` *subcommand*
would flip the Typer app into subcommand mode and break `graphlm <project>`
(ADR-003).

### Decisions

1. **`--upgrade` flag**, same shape as `--serve` / `--install-skill`. No
   `PROJECT_DIR`. Not eager (so `--help` still lists it).
2. **Upgrade with the same installer that installed.** Identity first:
   `uv-receipt.toml` → `uv tool upgrade`; pipx metadata → `pipx upgrade`;
   dist-info `INSTALLER=uv` → `uv pip install --upgrade`; else pip
   (`python -m pip`). **Do not follow the `bin/python` symlink**
   (`Path.resolve()` walks a uv-tool venv into uv's managed CPython and
   mis-classifies it as pip — #71). uv-tool and pipx keep extras via their
   own upgrade. pip / `uv pip` restate extras on the spec. A source checkout
   is refused (tell the user to pull/`uv sync`, or `uv tool install`).
3. **No shell.** Argv list only. Extras from the uv receipt / pipx metadata
   when present, else by probing the optional extra modules.

### Consequences

- `graphlm --upgrade` on a `uv tool install 'graphlm[all]'` install becomes
  `uv tool upgrade graphlm` and keeps `[all]`.
- `graphlm upgrade` (no dashes) still means "analyze a directory named
  `upgrade`".

---

## ADR-011 — PHP pack: `use` FQN→file plus quoted require/include

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/php.py`, extra `php`)

### Context

PHP is the last GitHub-Octoverse top-10 language without a pack. It has two
dependency forms: `use App\Models\User` (FQN, like Java) and
`require`/`include` of a path (like C). Concatenated `__DIR__ . "/x.php"` is
the common form and is not a static string.

### Decisions

1. **`use` is FQN→`.php`.** `App\Models\User` → `App/Models/User.php` under
   grammar-free `src/` roots plus `""`. `use function` / `use const` strip
   the last segment and try the parent file. `kind` is `"import"`.

2. **Quoted `require`/`include`/`require_once`/`include_once` of a string
   literal** resolve relative to the importer. `kind` is `"include"`.
   Concatenated paths are a policy drop and mark known-partial.

3. **Grammar accessor is `language_php`**, not `language()` — the wheel also
   ships `language_php_only`.

### Consequences

- PSR-4 vendor prefixes that do not match the on-disk path miss (under-resolve).
- `vendor/autoload.php` only becomes an edge if that file is in the scan.

---

## ADR-010 — Go pack: unique-file package dirs, suffix-stripped import paths

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/go.py`, extra `go`)

### Context

Go is in the GitHub top 10. `import "a/b"` names a *package directory*, not a
file. Fanning out to every `.go` in the directory would churn GRAPH_DIFF when
a file is added (ADR-005/007). `go.mod` is often not in the scan (not a
source ext), so the module prefix cannot be required.

### Decisions

1. **Unique non-test `.go` in the package directory, else drop.** Two or more
   files mark known-partial. `_test.go` is ignored. `kind` is `"import"`.

2. **Import path tried longest-first, then suffixes.** `example.com/mod/pkg`
   tries `example.com/mod/pkg/`, then `mod/pkg/`, then `pkg/`. First unique
   hit wins. `import "fmt"` misses and is third-party, not partial.

3. **`import "./rel"` is path-relative** to the importing file.

### Consequences

- One-file packages (common in small modules) get real edges. Multi-file
  packages trip the non-exhaustive framing.

---

## ADR-009 — LLM config from the environment and user-level `.env` only

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/config.py`)

### Context

#45 taught graphlm to load a project `.env` from the working directory upward so
an installed binary would see the user's project config. That also meant
`graphlm /path/to/untrusted-repo` (or `cd that-repo && graphlm .`) loaded that
repo's `.env` into the process — injecting whatever secrets it holds, and using
any `GRAPHLM_*` keys it happens to define as the LLM endpoint. The scanned tree
is hostile input; its dotenv files are already never sent to the model
(`_is_sensitive_file`). They should not become graphlm's own configuration
either.

### Decisions

1. **Only two config sources.** Exported `GRAPHLM_*` (highest) and
   `~/.config/graphlm/.env` / `$XDG_CONFIG_HOME/graphlm/.env`. Built-in defaults
   for numeric budgets and timeout only.
2. **Never search cwd, ancestors, or the scanned project** for a `.env`.
   `find_dotenv` is gone. The user file is loaded only when `is_file()`.
3. **`load_dotenv(..., override=False)`** still, so a real shell env wins over
   the user file.

### Consequences

- A project `.env` that used to configure graphlm (the #45 cwd walk) is ignored.
  Move those values to the user-level file or export them.
- Scanning a third-party repo cannot redirect the LLM call or dump its secrets
  into graphlm's environment.
- Developers hacking on graphlm itself must use the user-level file or exported
  vars, not a repo-root `.env`.

---

## ADR-008 — C/C++ pack: quoted `#include` only, one extra two grammars

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/cpp.py`, extra `cpp`)

### Context

C++ is in GitHub's "80% of new repos" six; C sits beside it in the top 10.
Both languages' file-level dependency is `#include`. Angle-bracket includes
are the stdlib/SDK. Quoted includes are project files. Macro includes
(`#include FOO`) are not statically resolvable. C and C++ need different
Tree-sitter grammars (``.h`` is parsed as C).

### Decisions

1. **One extra, two grammars.** `graphlm[cpp]` pulls `tree-sitter-c` and
   `tree-sitter-cpp`. ``.c``/``.h`` → language `c`; ``.cpp``/``.cc``/``.cxx``/
   ``.hpp``/``.hh``/``.hxx`` → `cpp`. Same resolver, like JS/TS.

2. **Quoted includes only.** `#include "foo.h"` resolves relative to the
   importing file, then an extension probe (``.h``.c``.hpp``.cpp``…).
   `#include <stdio.h>` is dropped as third-party, not partial. A macro
   include marks known-partial. `kind` is `"include"` (already a diff-contract
   value from Rust `mod`).

3. **`source_roots` is `("",)`.** Includes are path-relative, not module-root
   relative. `-I` search paths are out of scope (under-resolve, #19).

### Consequences

- A C/C++ repo on a base install still gets an LLM map; parser edges appear
  after `graphlm[cpp]`.
- Compiler `-I` / generated headers are missed by design.

---

## ADR-007 — C# pack: unique-file namespace usings, no fan-out

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/csharp.py`, extra `csharp`)

### Context

C# is the next GitHub-Octoverse language after the shipped packs (TypeScript,
Python, JavaScript, Java, then C#). `using MyApp.Models;` names a *namespace*,
not a file. Fanning it out to every `.cs` in that namespace would mutate every
importer's edge set when a file is added — the same GRAPH_DIFF churn that
made Java package wildcards a policy drop (ADR-005). `using static Type` and
`using Alias = Type` do name a type.

### Decisions

1. **`using static Ns.Type` and `using Alias = Ns.Type` resolve to a file.**
   Probe `Ns/Type.cs` then `Ns.cs` (nested type) under grammar-free `src/`
   roots plus `""`. `kind` is `"static"` for `using static`, `"import"`
   otherwise. `(from,to,kind)` remains the diff identity.

2. **A namespace `using Ns;` / `global using Ns;` resolves only when exactly
   one scanned `.cs` lives in the `Ns/` directory (or `Ns.cs` itself).** Two
   or more files in that directory are a policy drop and mark the language
   known-partial. Zero files is third-party / stdlib (`using System;`) and
   is *not* partial.

3. **No namespace→every-file fan-out.** A missing intra-project edge is
   preferred to a false or churny one (#19).

### Consequences

- A C# repo on a base install still gets an LLM map; parser edges appear
  after `graphlm[csharp]`.
- Small one-file-per-namespace projects get real edges. Folders with several
  types under one namespace trip the non-exhaustive framing; the model still
  infers them.

---

## ADR-006 — Rust pack: `mod` include + `use crate/super/self` against a filesystem module tree

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/rust.py`, extra `rust`; Phase 3 of #42)

### Context

Rust is not FQN→file (Java) or path-relative (JS). `mod foo;` *includes* a
child file; `use crate::foo::bar` then names a module path that has to be
looked up in the crate's module tree. External crates (`use serde::…`) are
the stdlib/third-party analog. Inline `mod foo { … }` and `#[path]` modules
are real but not a 1:1 file mapping.

### Decisions

1. **Two edge kinds.** `mod foo;` → kind `"include"` (parent file →
   `foo.rs` / `foo/mod.rs`). `use crate::` / `super::` / `self::` → kind
   `"import"`. Unprefixed `use foo` and `extern crate` are dropped as
   external (not partial). `(from,to,kind)` remains the diff identity.

2. **Filesystem module tree from the scan.** `lib.rs` / `main.rs` (lib wins
   in the same directory) and `bin/*.rs` are crate roots. Files under the
   crate directory map to module paths (`src/foo.rs` → `foo`,
   `src/foo/mod.rs` → `foo`, `src/foo/bar.rs` → `foo::bar`). `use` of an
   *item* (`use crate::foo::helper`) resolves to the longest matching
   module file (`foo.rs`), not a phantom `helper.rs`.

3. **Under-resolve inline and `#[path]` modules.** They mark the language
   known-partial rather than guessing a file. Glob `use crate::foo::*`
   resolves to `foo`'s file (one module, not a fan-out).

### Consequences

- A Rust repo on a base install still gets an LLM map; parser edges appear
  after `graphlm[rust]`.
- 2015-edition relative `use foo` without `self::`/`super::`/`crate::` is
  dropped as external — missing intra-crate edges, never a false ones (#19).
- This is the last planned pack. Further languages only on demand.

---

## ADR-005 — Java pack: drop package wildcards, `static` kind, Maven source roots

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/java.py`, extra `java`; Phase 2 of #42)

### Context

Phase 1 proved the extra/degradation machinery. Java is FQN → file (`import
com.acme.User` → `<root>/com/acme/User.java`), the Python analog. Two Java
forms are not a 1:1 file: `import pkg.*;` (a package = a directory of
`.java` files) and `import static Type.member;` (a member of a type).
`(from,to,kind)` is the GRAPH_DIFF identity key (ADR-002 decision 3).

### Decisions

1. **Drop package wildcards** (`import com.acme.util.*;`). Fanning out to
   every `.java` in the package means adding one file mutates the edge set of
   every wildcard importer — structurally correct, noisy diffs. A dropped
   wildcard marks the language known-partial (same framing as JS/TS bare
   packages). **Static star-imports** (`import static Type.*;`) still resolve
   to `Type.java`: the class is known, it is not a package fan-out.

2. **`kind` values:** `"import"` for a type import, `"static"` for
   `import static`. Changing either is a diff-contract change.

3. **Source roots** are `src/main/java`, `src/test/java`, and a bare `src/`
   (not `src/main` or `src/test` without `/java`). Computed from path shape
   only (grammar-free). The file's `package` declaration is a disambiguator:
   a mismatch with the path does not invent a root (#19).

4. **Nested types** (`import com.acme.Foo.Bar`) try `Foo/Bar.java` then
   `Foo.java`. One extra candidate, only emitted if that file is in the scan.

### Consequences

- A Java repo on a base install still gets an LLM map; the parser contributes
  no edges until `graphlm[java]` is installed.
- Wildcard-heavy code (some generated sources) will under-count edges and
  trip the non-exhaustive prompt framing — accepted, same honesty rule as #19.
- Rust (Phase 3) copies this extra + in-tree resolver + degradation template.

---

## ADR-004 — Python-core language packs: opt-in extras, bundled resolvers, relative-only JS/TS

**Date:** 2026-09-04
**Status:** Accepted — implemented (`graphlm/parsers/javascript.py`, `pyproject.toml` extras `js`/`all`; Phase 1 of #42)

### Context

The scanner already ingested `.js`/`.ts`/`.jsx`/`.tsx` and packed them into the
pass-2 prompt, but the Tree-sitter pass only extracted Python imports. The LLM
was guessing JS/TS edges with no ground truth. A runtime plugin API was
considered and rejected (locked-forever public contract, untrusted code). Putting
every grammar in the base install would bloat a Python-only user's install.

`(from_path, to_path, kind)` is the GRAPH_DIFF identity key (ADR-002 decision 3),
so new `kind` values are a contract, not a comment.

### Decisions

1. **Python is the only core language.** Its grammar is a base dependency. Every
   other language is an opt-in pip extra (`graphlm[js]`, later `java`/`rust`,
   aggregated as `graphlm[all]`) whose extra only pulls the grammar *wheel*.
   The resolver is graphlm-authored, in-tree, always registered. No plugin API.

2. **A missing extra degrades that language to zero edges, never the run.**
   `_GrammarUnavailable` is caught inside `build_dependency_graph` /
   `parse_file`, logged once per language, and Python edges on a mixed repo
   stay intact. `deterministic_edges` remains a list (not `None`) so the diff
   does not read the run as `ast=False` (ADR-002 decision 5). Resolvers must
   re-raise `_GrammarUnavailable` before a generic `except Exception`.

3. **JS/TS v1 resolves relative specifiers only.** `./foo` / `../bar`, with
   probe order `<spec>`, `<spec>.{ext}`, `<spec>/index.{ext}`. Extension order
   follows the importer: a `.js` file prefers `.js/.jsx/.mjs/.cjs` then
   `.ts/.tsx`; a `.ts` file prefers `.ts/.tsx` then the JS set. (A global
   TS-first list would make `require("./b")` from `a.js` hit `b.ts` over
   `b.js`.) Bare specifiers (`react`, `@scope/pkg`) and tsconfig
   `paths`/`baseUrl` aliases are dropped. A dropped bare specifier marks the
   language known-partial so the pass-2 edge table uses the non-exhaustive
   framing even when it was not size-capped.

4. **`kind` values for JS/TS:** `"import"` for `import` / `export … from` /
   dynamic `import()`; `"require"` for `require()` and TypeScript
   `import x = require(…)`. Changing a kind later is a diff-contract change.

5. **Grammar selection is `(language, suffix)`.** `detect_language` maps both
   `.ts` and `.tsx` to `"typescript"`, so a name-only registry cannot pick
   `language_tsx()` vs `language_typescript()`. Cache key is
   `(language, accessor)`. `.jsx` uses the javascript grammar (it handles JSX).

6. **JS/TS `source_roots()` is grammar-free** and returns `("",)`. Relative
   paths resolve from the importing file. This keeps a missing extra from
   escaping into `generate_graph`'s `except Exception → deterministic_edges=None`.

### Consequences

- A TypeScript repo on a base install still gets an LLM map; the parser just
  contributes no edges. Installing `[js]` turns those edges on with no other
  config.
- Under-resolving is load-bearing: a false edge in the do-not-contradict table
  is worse than a missing one (#19). Aliases stay out of scope until a later
  phase that can do them honestly.
- Java/Rust packs copy this template (extra + in-tree resolver + degradation
  tests with two fragments of the missing language). Do not start Phase 2
  until this one is merged and green.

---

## ADR-003 — Distribution: PyPI + GitHub Releases from one tag, no distro packages

**Date:** 2026-08-30
**Status:** Accepted — implemented (`pyproject.toml` metadata, `.github/workflows/release.yml`; v0.1.0)

### Context

graphlm's first release needs to land on a user's PATH on Linux. The options
considered: a Python package on PyPI, GitHub-Release artifacts, native distro
packages (`.deb`/`.rpm`), or a single frozen binary (PyInstaller).

The PATH requirement was *already* satisfied by the existing packaging — the
`[project.scripts]` entry point (`graphlm = "graphlm.cli:app"`) makes any wheel
install drop a `graphlm` launcher on PATH. So the real question was only the
*channel*, not how to get a command onto PATH.

### Decisions

1. **Publish to PyPI, installable via `uv tool install` / `pipx`.** graphlm is
   pure Python with a hatchling build already configured; a wheel is the native
   artifact. Verified before committing: the name `graphlm` is free on PyPI
   (HTTP 404), and the built wheel/sdist both include the `_html_template.html`
   data file (hatchling picks it up automatically) — the one thing the test
   suite can't catch, since tests run from the source tree where the file is
   always present.

2. **Also publish a GitHub Release from the same tag, with the same artifacts.**
   These are not competing channels — one `v*` tag builds once, then attaches
   the wheel + sdist to a GitHub Release *and* publishes them to PyPI. GitHub
   gives a versioned, downloadable source of truth (and an install path for
   anyone who can't/won't use PyPI); PyPI gives `uv tool install graphlm`. Same
   bytes in both places.

3. **PyPI publish uses Trusted Publishing (OIDC), not a stored API token.**
   GitHub Actions authenticates to PyPI via OIDC (`id-token: write` +
   `pypa/gh-action-pypi-publish`), so there is no long-lived credential to
   store, leak, or rotate. Requires a one-time Trusted-Publisher config on PyPI
   and TestPyPI (done in the browser).

4. **The release gates on a clean-venv smoke test.** Between build and publish,
   the workflow installs the built wheel into a fresh venv (never `.venv`) and
   runs `graphlm --version` + `graphlm <fixture> --dry-run`. That single check
   catches a missing entry point, a missing data file, or a missing dependency —
   with no network. A broken artifact fails the release instead of reaching
   users.

5. **No native distro packages (`.deb`/`.rpm`) for v0.1.0 — rejected, not
   deferred silently.** Verified: Debian 13 does not ship the tree-sitter Python
   bindings as system packages (`python3-tree-sitter` /
   `python3-tree-sitter-python` are not in the archive), so a `.deb` would have
   to vendor a whole virtualenv into `/opt` — which is really the frozen-binary
   path (PyInstaller) with extra steps, and a heavier maintenance burden. A
   zero-Python-install artifact (single binary) is a legitimate **fast-follow**
   if demand appears; it is out of scope for the first release.

### Consequences

- Publishing to PyPI is **irreversible** (the name is permanent, a version
  number can never be reused). The release is therefore rehearsed against
  **TestPyPI** first — same workflow, `workflow_dispatch` → TestPyPI — to
  validate the Trusted-Publishing wiring without burning the real name/version,
  and the real publish is a separate, explicitly-approved step.
- The tree-sitter deps carry upper bounds (`tree-sitter<0.27`,
  `tree-sitter-python<0.26`) so a future API-breaking release of those
  C-extension bindings can't break installs of a shipped 0.1.0.

### Output location: `.graphlm/` (bundled into the 0.1.0 shaping)

Two user-observable defaults were changed in the same release, while it's still
free to do so (no prior version shipped either default):

1. **Default output moved to `<project>/.graphlm/`** (was the project root). One
   tidy folder instead of five files scattered at the repo root. `-o` still
   overrides and is honored **literally** (no `.graphlm` appended — the user
   named the directory). CLI-only: the library API
   (`generate_graph(output_dir=...)`, `result.write(dir)`) still writes to the
   literal directory given.
2. **Self-ingestion guard is now directory-level.** `.graphlm` is added to
   `scanner._ALWAYS_EXCLUDE`; since `_should_exclude` matches on any path
   component, the whole output directory (and its contents) is excluded from the
   scan in one entry. The exact-filename `GRAPH.*` / `GRAPH_DIFF.*` entries are
   **kept** for the `-o`-into-the-scanned-tree case. Verified with a real
   two-run test (a fixture with a populated `.graphlm/` re-scanned → the map
   files never appear in the scanned fragments or the pass-1 tree), because a
   unit assertion alone wouldn't catch a walk-level regression (#28 must stay
   closed).
3. **Clean break on the diff baseline, no compat shim.** `diff.load_baseline`
   reads the prior `GRAPH.json` from the output dir, which is now `.graphlm/`.
   graphlm adds **no** fallback read of an old project-root `GRAPH.json`; a user
   who mapped a project before this release simply gets one `FIRST_RUN` diff on
   the next run, then normal diffs. (Consistent with the project's standing "do
   migrations completely, no back-compat shims" rule.)
4. **The diff needs no code change** — it reads whatever output dir
   `write_outputs` is handed, so it follows `.graphlm/` for free.
5. **Rendered-map paths stay project-root-relative.** File paths in
   `GRAPH.md`'s body (e.g. `graphlm/cli.py`) are left relative to the repo root
   — correct for a human or agent reasoning about the repo — rather than
   rewritten with `../` to be click-through from inside `.graphlm/`.

### Agent-skill install (`--install-skill`)

`graphlm --install-skill <harness>` drops a guide teaching a coding agent to use
graphlm and to look for `.graphlm/GRAPH.md` when loading a codebase.

- **Eager flag, not a subcommand.** `graphlm` is a single-command Typer app with
  a required `project_dir`; a second `@app.command()` would flip it into
  subcommand mode and break the primary `graphlm <project>` interface. The flag
  is `is_eager=True` (like `--version`), so `graphlm --install-skill claude`
  works with no `project_dir`.
- **Never edits a file graphlm didn't create.** For Claude it writes a fresh
  `~/.claude/skills/graphlm/SKILL.md` (a dir graphlm owns). For Codex it writes
  `~/.codex/graphlm.md` and **prints** a one-line snippet for the user to
  include from their own `AGENTS.md` — graphlm does not append to the user's
  existing `AGENTS.md` / `CLAUDE.md`. It also **refuses to write through a
  symlink** at the target (checked before write, catches broken links too), so a
  dotfiles-managed `~/.claude/skills/` can't be silently clobbered (#33).
- **User-global by default**, `--skill-local` writes into the scanned project.
  Idempotent (skip-if-exists unless `--force`). The guide is written to no-op
  gracefully when a repo has no `.graphlm/GRAPH.md` (it tells the agent to
  generate one with `graphlm .`), so global install doesn't misfire in unmapped
  repos.

---

## ADR-002 — `GRAPH_DIFF.*`: graph-vs-graph diff artifact

**Date:** 2026-08-30
**Status:** Accepted — implemented (`graphlm/diff.py`, `write_outputs`, `--no-diff`; #28)
**Context / plan:** `docs/plans/self-refreshing-graph.md` (Fast-follow section)
**Depends on:** ADR-001 (the versioned `meta` stamp is the input contract this reads)

### Context

With the graph now self-stamped and regenerated as code moves (ADR-001), the
next question is "what changed in the *map* since last time?" `GRAPH_DIFF.*`
answers it with a **structural graph-vs-graph diff** — modules, edges, cycles,
data-flows, entry-points, and file-summaries added and removed between the prior
`GRAPH.json` and the new one. It is deliberately **not** a code diff (git does
that better); it reads graphlm's *own* prior output, which is why ADR-001 made
`meta.schema_version` a versioned input contract.

This ADR settles the decisions the plan left open. Building (a `diff.py`, tests,
`write_outputs` changes) is gated on it — nothing is implemented yet.

### Decisions

1. **The diff runs in `write_outputs` (`render.py`), not `generate_graph`.**
   Grounded in the merged code: the CLI calls `generate_graph(output_dir=None)`
   and writes via `result.write(output_destination(...))` (the #8 split, which
   CLAUDE.md says not to simplify), so `generate_graph` never learns the
   destination. `write_outputs` is the *only* place holding both the graph and
   the output dir. Therefore:
   - **Ordering:** the baseline `GRAPH.json` is read (and parsed) *before*
     `json_path.write_bytes(...)` overwrites it — after `mkdir`, before the write.
   - **`--dry-run` writes no diff, by design.** The CLI exits before
     `result.write` (`cli.py`), so dry-run never reaches the diff path. This is
     intended (dry-run makes no LLM call and produces no authoritative graph);
     nobody should "fix" it later.

2. **Added/removed only — no "changed" bucket in cut one.** Identity keys are
   *structural* (`path`, edge tuples, node sets), so a run where only prose
   changed — a module `description`, a `file_summary` summary, a `data_flow`
   description, a cycle `risk_score` — reports *no change* for that entity. This
   is deliberate: those fields are LLM-regenerated every run, so a "changed"
   bucket over them would be dominated by nondeterministic prose churn and drown
   the structural signal the diff exists to surface. The cost (a pure
   description rewrite is invisible) is accepted for cut one and named here so it
   is a decision, not a silent consequence of the key choice. A future "changed"
   bucket, if added, must be scoped to structural sub-fields, not free text.

3. **Identity keys are the diff's contract** (verified against the merged
   models):
   - `modules` → `path`
   - `import_edges`, `deterministic_edges` → `(from_path, to_path, kind)`
   - `import_cycles` → frozenset of `nodes` (order-independent)
   - `data_flow` → `(source, destination)`
   - `entry_points` → `(path, name)`
   - `file_summaries` → `path`
   Renames are **remove + add** (no rename-matching heuristics — locked in the
   plan). Changing a key later changes what counts as "the same entity", so key
   choices are as load-bearing as a wire format.

4. **Three baseline states, not two.** An agent must distinguish them, so each is
   an explicit, labeled state in the diff — never silently collapsed:
   - **first run** — no prior `GRAPH.json` ("initial graph — no prior version").
   - **uncomparable baseline** — the prior file exists but can't be parsed
     (corrupt JSON) or carries an *unrecognized* `meta.schema_version`. This must
     NOT masquerade as a first run; it is its own state ("prior graph could not
     be read — not compared"). A newer graphlm reading an older, *known* version
     still compares; only an unknown/future version is uncomparable.
   - **normal** — both parsed; emit added/removed.
   The `-o <new dir>` case (no baseline at the destination) falls under "first
   run" for that destination, correctly.

5. **`deterministic_edges` `None` vs `[]` is meaningful.** `None` means AST was
   off (`--no-ast`) — "not compared"; `[]` means AST ran and found none. If
   either side is `None`, the `deterministic_edges` dimension is reported "not
   compared", never "all N removed" — so toggling `--no-ast` between runs does
   not fabricate a mass deletion.

6. **Artifact shape** — `GRAPH_DIFF.md` + `GRAPH_DIFF.json`, **no HTML** in cut
   one (fewer moving parts; the ADR-001 directive already skips HTML). The JSON
   carries its **own** `diff_schema_version` (independent of the graph's), the
   added/removed lists per dimension, the baseline-state label, and a header with
   the **SHA range** (`old meta.commit_sha` → `new meta.commit_sha`). When either
   side's `commit_sha` is `null` (non-git, or an old graph), the header says so
   ("unknown → `<sha8>`") rather than omitting the range. It follows the existing
   `*_suffix` convention (default suffix `GRAPH` → `GRAPH_DIFF`; a custom
   `--json-suffix graph` yields `graph_DIFF`).

7. **On by default, opt-out via flag.** The plan's "still WRITE it" phrasing is
   honored: a real run always writes `GRAPH_DIFF.*` (including the first-run
   marker), with a `--no-diff` / `include_diff=False` escape hatch mirroring
   `--no-html`. On-by-default is the point — the diff is only useful if it is
   always there to read.

### Consequences

- **`write_outputs` signature churns.** It returns a 3-tuple
  (`md, json, html`) unpacked positionally at ~10 call sites (CLI + tests). Diff
  paths are surfaced **without breaking that tuple** — e.g. appended as a 4th/5th
  element only when diff is enabled would still break positional unpackers, so
  instead the diff paths are returned via a small result object or a separate
  accessor. The exact mechanism is an implementation choice for the build PR; the
  constraint recorded here is: **do not silently change the arity the existing
  callers unpack.** Prefer an additive, non-breaking surface.
- graphlm now *reads* its own prior output on every run — the ADR-001 versioned
  contract is load-bearing from here on. A future `CodebaseGraph` field change
  must keep old graphs readable (already guaranteed by optional/defaulted fields)
  **and** bump `meta.schema_version` if the *meaning* changes, so the
  uncomparable-baseline path (decision 4) can trigger deliberately.
- No new network, no new LLM call — the diff is pure local computation over two
  already-materialized graphs.

### Non-goals (locked — do not reopen when building)

- Not a code diff. No rename-matching. No dirty-working-tree handling. graphlm
  never declines to run. No "changed" bucket over free-text fields in cut one.

---

## ADR-001 — Self-refreshing graph: provenance stamp + agent-scheduled refresh

**Date:** 2026-08-30
**Status:** Accepted (Release one implemented; the `GRAPH_DIFF.*` fast-follow is now implemented — ADR-002, #28)
**Context / plan:** `docs/plans/self-refreshing-graph.md`

### Context

A generated graph goes stale the moment the code moves on, but nothing tells a
reader (human or coding agent) that it has. We want the output to be
*self-refreshing without any hook or flag*: it should carry its own provenance
and a directive that prompts regeneration when the code has changed.

### Decision

1. **The agent is the scheduler, not graphlm.** The generated `GRAPH.md` carries
   a top-of-file *refresh directive*; a coding agent reading the file decides
   whether to regenerate. graphlm stays dumb — when invoked it always
   regenerates and re-stamps; it never declines, and holds no staleness logic.
   Rejected alternatives: a git hook (taxes every session with a ~200s paid LLM
   run) and a `--if-stale` flag (puts scheduling in the wrong place).

2. **Staleness = git SHA mismatch.** The stamp records the git `HEAD` the graph
   was generated against; `HEAD != stamped_sha` is the trigger. `created_at` is
   human context only, never the trigger.

3. **The stamp is authoritative in `GRAPH.json`; the `GRAPH.md` directive is
   rendered from it.** One source of truth, so the two cannot drift.

4. **The directive is advisory.** The agent may ignore it. Best-effort staleness
   is the correct trade — better than forcing a regen — and the docs say so.

5. **Non-git repos degrade, they don't error.** No SHA → `commit_sha = null`
   (preserved explicitly in JSON, so "no git tracking" is distinguishable from
   "old format, field absent") and the directive falls back to the agent's
   judgment. Git capture is failure-tolerant: not a repo, git absent, or an
   empty repo all yield `null`, never an exception.

6. **Wording: "generated against commit X", never "reflects X".** The graph is
   built from files on disk, which may include uncommitted changes, so a graph
   can be SHA-fresh yet not match the working tree. We stamp `HEAD` and do not
   chase the dirty-working-tree case (over-engineering for release one); the
   honest phrasing avoids overclaiming.

7. **The metadata block is versioned (`schema_version`), making the output
   format an *input* contract.** This is the load-bearing, ADR-worthy decision.
   The fast-follow `GRAPH_DIFF.*` feature reads graphlm's *own* prior
   `GRAPH.json` to diff two graphs; for that read to be safe across future
   format changes, the persisted meta is versioned so a format change is
   *detected*, not silently misparsed. Coupling output shape into an input
   contract is a deliberate, named cost, accepted for the diff capability.

8. **Meta is filled locally, never emitted by the LLM.** Like `directory_tree`
   and `deterministic_edges`, `generate_graph` sets `graph.meta` after pass 2
   (and in `--dry-run`), overwriting anything the model may have hallucinated
   into a `meta` field. It is not in the pass-2 instruction block, so it costs
   no output tokens and cannot be spoofed by scanned file content.

### Consequences

- `graphlm/provenance.py` is the first and only module that reads git / shells
  out. Kept isolated and failure-tolerant.
- A git SHA is accepted only when `git rev-parse HEAD` exits 0 **and** stdout is
  a 40- or 64-hex hash — this rejects the empty-repo case, where git prints the
  literal `HEAD` with a non-zero exit.
- **Known limitation (committing the graph → self-invalidation):** the check is
  `stamped_sha != HEAD`, so committing `GRAPH.*` invalidates the stamp on the
  very commit that ships it — `HEAD` advances to that commit and the map reads
  as one commit stale, permanently one behind. This repo gitignores `GRAPH.md`
  / `GRAPH.json` / `GRAPH.html` (`.gitignore`) and regenerates on demand, which
  keeps the stamp on a real current SHA. Consumers who commit the graph should
  regenerate it as the final step of the same commit and accept one-commit
  staleness until the next regen. The failure mode to avoid is committing a
  graph with no regeneration step, which reintroduces the per-session refresh
  tax this design removed. Documented (README + here), not engineered around —
  a dirty-tree/uncommitted-graph escape hatch is out of scope for release one.
- **Known limitation (`-o`):** when output is written somewhere other than the
  scanned repo (`-o <elsewhere>`), an agent that reads that `GRAPH.md` and runs
  `git rev-parse HEAD` in *its own* directory compares against the wrong repo.
  Documented, not engineered around, for release one.
- **Known behavior (subdirectory):** pointing graphlm at a subdirectory of a
  repo stamps the *containing* repo's `HEAD` — git's own behavior, and the
  correct staleness anchor for that subtree.
- `--dry-run` fills the stamp on the graph object but the CLI still prints stats
  and does not write files (unchanged contract); the stamp is exercised on any
  real run and via the library `write()` path.
- **Additive fields, no bump (2026-09-02, innovation #6).** `meta` gained two
  optional fields — `usage` (real vs estimated tokens per pass) and
  `faithfulness` (LLM `import_edges` vs AST `deterministic_edges`) — and
  `schema_version` stayed at **1**. The rule above is "bump if the *meaning*
  changes": these are purely additive, default to `null`, and change nothing
  about how the existing fields are read, so an old graph without them still
  loads as a `NORMAL` baseline and a new graph read by an older graphlm simply
  ignores them (Pydantic drops unknown keys). Bumping for an additive field
  would make every prior graph `UNCOMPARABLE` for no safety gain.

### Fast-follow

`GRAPH_DIFF.*` — a graph-vs-graph diff (not a code diff). **Scoped in ADR-002
(above) and tracked by #28**; design settled and now implemented
(`graphlm/diff.py`).
