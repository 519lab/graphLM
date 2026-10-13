# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to Semantic Versioning.

## [Unreleased]

### Changed

- **Scanning a large tree does less work for the same map.** Exact exclude names (built-ins, `--exclude`, `.graphlmignore`) are a set lookup; `fnmatch` runs only for patterns that contain a glob character. The directory tree and the file list come from one `os.scandir` walk. Which files are selected, their order, the tree text, and the symlink / nested-checkout / sensitive-file checks are unchanged.
- **Startup no longer imports httpx.** `import graphlm`, `graphlm --help`, `--version`, `--dry-run`, and `--serve` do not load the HTTP client (or Rich, which httpx pulls in). httpx loads on the first real LLM call. The output-token ceiling moved to `graphlm.limits` so configuration does not import the client to read an integer; `graphlm.llm.LLM_MAX_OUTPUT_TOKENS` still re-exports it. Redaction and the parsers still load at import.

## [0.6.0] - 2026-09-23

### Changed

- **A fresh run leaves the output dir with exactly the artifacts it produced.** When graphlm regenerates a map — especially after an upgrade, or when you toggle a flag like `--json` — it now removes its *own* stale artifacts (e.g. a `GRAPH.json` left over from a prior version) so an agent can't glob an outdated machine-readable map beside a fresh `GRAPH.md`. Only graphlm's own exact artifact names are ever removed (never a directory, never a glob, symlinks skipped) — your `GRAPHICS.md` / `graph.json` / `GRAPH_notes.md` are untouched. When the prior map came from a **different graphlm version**, `GRAPH_DIFF` now says so explicitly ("prior graph from graphlm 0.5.0 — regenerated fresh under 0.6.0, not compared") instead of misreporting it as a first run or a corrupt file; the diff JSON gains `old_graphlm_version` / `new_graphlm_version` and its `diff_schema_version` is bumped to 2. `GRAPH.md`'s provenance line now names the graphlm version that produced it, and `GRAPH.html` embeds it as machine-readable data.
- **`GRAPH.json` is now opt-in (`--json`); the default outputs are `GRAPH.md` + `GRAPH.html`.** The Markdown map is the artifact a coding agent should read — the JSON's node/link arrays fill a context window far faster for the same information — so the CLI no longer writes `GRAPH.json` (or `GRAPH_DIFF.json`) unless you pass `--json`. Nothing else changes: graphlm keeps an internal JSON working copy (`.graphlm/.graph-state.json`) on every run, so the graph-vs-graph diff and `graphlm --serve` (MCP) work exactly as before with or without `--json`. The **library** default is unchanged — `generate_graph(output_dir=...)` and `GraphResult.write(...)` still write `GRAPH.json` by default (new `include_json=` param to opt out), so `md, json, html = result.write(dir)` and `load_map(".graphlm/GRAPH.json")` keep working (`write_outputs` now also runs the fresh-run cleanup described above). Upgrading users: a prior-version `.graphlm/GRAPH.json` is cleaned up automatically on the next run rather than left stale (and the diff reports the version change instead of a false "first run"). Note `--no-html` / `--no-diff` now also remove a prior `.html` / `_DIFF.*` they used to leave in place.

### Added

- **`GRAPH.md` opens with a compact orientation block.** The map's highest-signal facts — entry points, whether there are any production import cycles, and the highest-fan-in (blast-radius) files — used to sit hundreds of lines down, after a full directory tree, so an agent (or a human verifying the map) burned context scrolling to find the shape of the repo. A short `## Orientation` section now sits right under the provenance directive, before the directory tree. Fan-in is labelled "most imported (blast radius)", never "importance" — raw import degree measures reach, not architectural weight (ADR-015). The block is a summary of data still rendered in full below, omits any line it has no data for, and is absent entirely when the graph has no entry points, cycles, or edges. The MCP `overview` tool gains matching `production_cycles` / `test_only_cycles` counts and shares the same fan-in computation.
- **Test-code import cycles are labelled, not hidden.** graphlm's map treated a cycle among test files (fixtures, test helpers) exactly like a production cycle — so a repo with deliberately-cyclic parser fixtures showed "5 import cycles" at the top of its map, misleading an agent into thinking the *product* had circular imports. Cycles whose every member is a test file are now flagged (`Cycle.test_only`, and `test_only` / `production_count` / `test_only_count` on the MCP `cycles` tool) and rendered distinctly across all outputs: `GRAPH.md` lists production cycles first and groups test-code cycles under their own heading (with a clear "no production cycles" banner when *all* cycles are among tests); the Mermaid module graph outlines and colours test-only cycles in muted amber instead of alarming red; and `GRAPH.html` mutes test-file nodes. Tests remain fully in the map — this labels them, it does not drop them (`--no-tests` still does that). The label is derived deterministically from the file path and is exactly the set `--no-tests` would remove.

### Infrastructure

- **Module-size refactor: no module exceeds the 600-line limit.** `scanner.py`'s exclusion patterns and filesystem guards split into `exclusions.py` (`_ALWAYS_EXCLUDE`, `_should_exclude`) and `scan_guards.py` (`_symlink_hides_sensitive`, `_is_nested_checkout`, `_path_is_inside`, `load_graphlmignore`); `__init__.py`'s post-pass-2 enrichment helpers split into `enrich.py` (`build_meta`, `pass_usage`, `is_directory_granular`, `score_file_importance` — now public, un-underscored). A dead duplicate `_is_nested_checkout` in `redact.py` (unused since #54) was removed. Zero behaviour change; the public `graphlm` import surface (`generate_graph`, `GraphResult`, `WriteResult`) is unchanged.

## [0.5.0] - 2026-09-22

### Added

- **Module importance ranking (`GRAPH.md` Importance column, TypeSafe/Jev).** The map listed modules but never said which were load-bearing — and raw import degree can't tell "imported everywhere" (a constants file) from "central to the architecture" (the orchestrator). Each module now carries a semantic **role** score (Jev: leaf → orchestrator) alongside its structural **degree** (parser ground truth); `GRAPH.md`'s Modules table gains an Importance column that fuses the two and sorts load-bearing first. The two components are stored raw in `GRAPH.json` (not a pre-blended number) so the display weighting can change without rewriting the map, and the CLI prints the top load-bearing modules. On a real project this correctly demoted a constants module from degree-#1 to importance-#5 and promoted a low-degree entry point. On **large repos** where the model describes modules at *directory* granularity (`src/pkg/sub`), importance is scored over the file-level `file_summaries` instead and rendered as a separate `## File Importance` section in `GRAPH.md` (stamped into `meta.file_importance`), and the CLI prints the top load-bearing files — a pre-registered evaluation measured the ranking gap over raw degree jump from +0.01 to +0.11 when scoring files rather than packages. Best-effort and off (both fields null) without a `TYPESAFE_API_KEY` or the `graphlm[typesafe]` extra; unlike summary scoring it is **not** disabled by `--no-redact` (it sends descriptions and a degree count, never source). When off, the Modules table is unchanged. Opt out with `--no-importance`.
- **Summary evidence-support scoring (`meta.evidence_support`, TypeSafe/Jev).** graphlm could vouch for its import *edges* (the LLM's edge list vs. the parser's ground truth) but nothing checked whether the model's *prose* — the per-file summaries — was actually backed by the source it read. Each summary is now scored against the exact fragment the model saw (redacted/skeletonised text, not the file on disk) as a calibrated 0–1 support probability, and the mean plus the weakest files are stamped into `GRAPH.json` and shown on the CLI. Like edge-faithfulness it is a *trust weight*, not a hallucination verdict — a skeletonised or truncated fragment honestly scores lower. Best-effort and off by default unless the `graphlm[typesafe]` extra is installed, a `TYPESAFE_API_KEY` is set, and redaction is on (the source must not reach a third party unredacted); it never runs on a dry run and never blocks or discards a graph. Near-empty files (a bare-docstring `__init__.py`, a thin entry point that defines nothing) are skipped rather than scored — there is nothing to verify a claim against, so scoring them produced a meaningless ~0.2 that falsely flagged correct minimal summaries as weak (#168). Opt out with `--no-evidence`.
- **First-run setup wizard (`graphlm --setup`, and an automatic first-use prompt).** graphlm's optional capabilities — the language-grammar packs, the `mcp` server, and the new `typesafe` prose-scoring SDK — were undiscoverable: a user never learned their JS/TS files produced zero edges because the `[js]` wheel wasn't installed. The wizard lists the not-yet-installed packs, installs the chosen ones with the **same installer that put graphlm on PATH** (uv tool / uv pip / pipx / pip, keeping any packs already present), and — for TypeSafe — tells you to add `TYPESAFE_API_KEY` to `~/.config/graphlm/.env`. It runs automatically the first time graphlm is used interactively (a `~/.config/graphlm/.setup-done` marker stops it firing again); a non-interactive/CI run prints a one-line hint and proceeds without blocking, and a source checkout is refused. No optional pack becomes a base dependency.
- **New optional extra `graphlm[typesafe]`** (the TypeSafe/Jev SDK), offered by the setup wizard and consumed by the evidence-support scoring above. It is opt-in — never a base dependency and not part of `graphlm[all]` — because it calls a commercial third-party API and needs a key.

### Fixed

- **Empty-user connection URIs (`redis://:secret@host`) and escaped quotes inside quoted passwords are redacted.** The URI regex required a username; the quoted-password matcher stopped at the next raw `"`.
- **Truncated PEM bodies are redacted after a leftover BEGIN header.** Head-truncation can cut before `END`; the full-block regex then misses and only the BEGIN line was replaced, leaving `MIIE…` in the fragment.
- **In-project symlinks into a `.graphlmignore` / `--exclude` tree are not scanned.** `config.yaml → secrets/prod.yaml` used to be read because the symlink guard only screened never-read names and `_ALWAYS_EXCLUDE`, not the user's exclude set.
- **In-project symlinks into `.git/` (and other always-excluded dirs) are not scanned.** `utils.py → .git/config` used to be read because the never-read check only screened secret *filenames*, while the walk already refuses `.git`.
- **`tests/src/` is not treated as a Python/Java/PHP source root.** #19 rejected `tests/stub/requests` but any nested `src`/`lib`/`python` still became a root, so `import requests` resolved onto `tests/src/requests` (#161).




- **`graphlm /symlink/to/project` writes GRAPH.* into the real checkout.** The ancestor-symlink write refusal treated the symlink project path as a planted decoy and exited 2 after the paid LLM calls. The default destination now `resolve()`s the project; `-o` stays literal (#154).

- **Quoted passwords that contain spaces are redacted.** The value class was `[^\s…]`, so `"password": "hello world secret"` in JSON and `password = "hello world"` survived.
- **Rust `mod foo;` is no longer dropped when a preceding attribute merely contains the word `path`.** `#[cfg(feature = "path")]` was treated as `#[path = "..."]` because the detector was `b"path" in node.text`. Only a real `#[path = "..."]` is skipped now (#153).

- **TLS/SRV connection-string passwords are redacted.** The URI regex required `mongodb://` / `redis://` / `amqp://` with no suffix, so `mongodb+srv://`, `rediss://`, and `amqps://` (Atlas / Redis TLS / AMQP TLS) kept the password.

- **`.npmrc` / `.yarnrc` / `.pypirc` are never read.** `_auth=base64(user:pass)` and `https://user:pass@` in `.npmrc` are not `password=` assignments, so redaction left short logins intact and pass 2 could send them.
- **`.keystore` and backups of secret extensions (`app.jks.bak`, `server.key~`) are never read.** Android `debug.keystore` missed the `.jks` list, and backup matching only covered exact names, so binary keystore bytes were scanned.

- **`BEGIN ENCRYPTED PRIVATE KEY` PEM bodies are redacted.** The PKCS#8 encrypted label was not in `(RSA |EC |DSA |OPENSSH )?`, so JSON keystores sent the ciphertext body.

- **PHP interpolated double-quoted `require "config.php$id"` is not a static include.** The extractor took the first `string_content` chunk and emitted `a.php → config.php`. Interpolation is now a policy drop (same as concat) and marks known-partial (#147).

- **`.htpasswd`, singular `secret.yaml`, and `*.env` / `foo.env.local` are never read.** `*secrets*` missed `secret.yaml`; `.htpasswd` is not `*password*`; `config.env` is not a leading `.env`. Those files were scanned with hashes and keys intact.

- **JSON `"secret"` / `"token"` / `"auth"` colon assignments and PEM bodies are redacted.** PR 139 covered `"password"` / `"api_key"`; the generic secret regex still required `=`, and private-key redaction only rewrote BEGIN/END, so `client_secret.json` and a PEM body in JSON survived.


- **`write_outputs` refuses to write through a symlink, including ancestor directory links.** `GRAPH.json` (or `.graphlm` itself) as a symlink used to be followed, and a parent `decoy → victim` plus `-o decoy/out` still wrote GRAPH.* into `victim`. Same contract as skill install (#33): remove the symlink and re-run.

- **GRAPH.html no longer lets `{_PALETTE}` in graph data steal the template placeholder.** Palette substitution ran after JSON embed, so a node path or description containing `{_PALETTE}` spliced the color array into `graphData` and left `const _PALETTE = {_PALETTE}`. Palette is substituted first (#141).

- **`--serve` path resolution no longer maps `a.py` onto `data.py`.** After exact/suffix/prefix matching, `resolve_path` used unanchored `query in path`, so a unique `'a.py' in 'data.py'` returned the wrong module as `found: True`. File-like queries (basename containing `.`) skip that fallback; extensionless unique names like `core` still match (#140).

- **JSON `"password"` / `"api_key"` assignments are redacted.** The assignment regexes required the keyword to sit immediately before `=`/`:`, so `"password": "…"` in `appsettings.json` survived. Optional quotes around the key are now accepted.

- **Backups of `.netrc` / `_netrc` / `.pgpass` / `.flaskenv` are never read.** Exact-name matching skipped `.netrc.bak` / `.pgpass~` / `.flaskenv.bak`; netrc and pgpass secrets are not `password=` assignments, so redaction left them intact.

- **PHP `require 'x.php' or die()` is extracted.** The quoted path is a string literal; tree-sitter wraps it in a logical `binary_expression`. The unwraper treated every binary as concat (`__DIR__ . "/x.php"`), so a common include form produced no edge. Logical `or` / `||` are unwrapped; concat still drops (#135).

- **C# namespace alias `using Models = MyApp.Models;` no longer resolves onto a parent `MyApp.cs`.** #126 stopped the nested-type parent probe for a plain namespace using; aliases still ran it first, so a namespace alias preferred `MyApp.cs` over unique-dir `Models/User.cs`. Aliases now unique-dir first and only parent-probe when that misses (#134).

- **Dotenv editor backups and non-dot suffixes (`.env~`, `.env-local`) are never read.** Never-read required `.env` or `.env.<suffix>`, so vim `.env~` and `.env-local` / `.env_backup` were scanned; assignment redaction cannot catch arbitrary `VAR=secret` lines.

- **PHP class `use App\Models\User` no longer resolves onto `Models.php`.** The parent-file probe (last-segment strip) ran for every FQN, so a missing `User.php` became a false edge to the parent type. That probe now runs only for `use function` / `use const` (ADR-011) (#129).

- **C++ `#include "foo.h"` no longer resolves onto a sibling `foo.c`.** The extension probe stripped `.h` and retried `.c`/`.hpp`/…, so a missing local header became a false include of the implementation file (and a self-edge from `foo.c`). The probe now runs only for extensionless `#include "foo"` (ADR-008) (#128).

- **`.envrc` and `.flaskenv` are never read.** They are dotenv-class secret stores that are not `.env.<suffix>`, so they were scanned; assignment redaction cannot catch arbitrary `VAR=secret` lines.
- **GitHub fine-grained PATs (`github_pat_`) are redacted.** The dedicated GitHub regex only matched classic `ghp_` / `gho_` / … prefixes, so a bare `github_pat_…` in a comment survived.

- **Go raw-string import literals are extracted.** `import \`./rel\`` is valid Go; the extractor only accepted interpreted `"..."` strings, so those edges were missing (#122).

- **PHP `require("x.php")` and grouped `use A\{B, C}` are extracted.** Parenthesized require wrapped the string in `parenthesized_expression` and was treated as a policy drop (#120). Grouped PSR-12 `use` nested clauses under `namespace_use_group` and produced no edges (#121).


- **In-project symlinks to never-read files are not scanned.** `crypto.py → .env` (or `id_rsa`) used to be read because the never-read check used the *link name* while `read_text` followed the target. The resolved target is now screened too.

- **PHP quoted `require`/`include` inside `if` or a function is extracted.** Only root `expression_statement` nodes were visited, so `if (true) { require "b.php"; }` produced no include edge and did not mark known-partial (#114).

- **C# `using` inside `namespace { }` is extracted.** StyleCop SA1200 puts usings in the block; the walker only looked at root / file-scoped namespace children, so those edges were missing (#113).

- **Pass 1 `requested_files: null` no longer aborts after a paid LLM call.** Free-form JSON with `null` or a string used to `TypeError` in `filter_requested_files` (or silently match zero files). Non-list / non-string entries are now treated as “request nothing” and pass 2 still runs (#115).

- **`write_outputs` refuses to write through a symlink.** `GRAPH.json` (or `.graphlm` itself) as a symlink used to be followed, so a cloned repo could clobber a file outside the output dir. Same contract as skill install (#33): remove the symlink and re-run.

- **`latest.py` is ranked as source, not as a test.** After #94, `_rank_file` still used `"test" in stem`, so `latest.py` (rank 10) lost to markdown (rank 8) under a tight `max_files` cap and never reached pass 2 / AST (#109).

- **`.netrc` / `_netrc` / `.pgpass` are never read.** They hold passwords in layouts the assignment regex does not match (`machine host login user password SECRET`, `host:port:db:user:SECRET`), so they were scanned verbatim.
- **OpenSSH vim/emacs private-key backups (`id_rsa~`, `#id_rsa#`) are never read.** The `.bak` / `.old` prefix rule missed editor backup names, and redaction still only strips BEGIN/END, so the key body was scanned.
- **OpenSSH private-key *backups* are never read.** Exact-name matching skipped `id_rsa.bak` / `id_ed25519.old`, and redaction only strips BEGIN/END, so the key body was still sent. Names that start with `id_rsa.` / `id_rsa-` (except `.pub`) are now never-read too.

- **In-project directory symlinks no longer explode or ghost the pass-1 tree.** `_walk_dir` followed `Path.is_dir()` into a `sub/loop → root` cycle (165 lines of `loop/sub/loop/...`) and listed `alias/mod.py` for a `alias → pkg` link that `os.walk` never reads. Directory symlinks are skipped in the tree, matching `followlinks=False` (#97).

- **C/C++ quoted `#include` inside `#ifdef` is extracted.** Only root-child `preproc_include` nodes were visited, so a guarded `#include "bar.h"` never became an AST edge. The walker now visits nested preprocessor nodes (byte offsets only, never `start_point`) (#99).

- **Pass-2's file cap keeps the files pass 1 asked for first.** `filter_requested_files` sorted by path then sliced, so `--max-pass2-files` dropped `z.py` in favour of `a.py`. The cap now follows request order; the kept set is still sorted for deterministic output (#98).

- **GRAPH.html and the Mermaid module graph honour `./`-prefixed LLM paths.** Cycle nodes from `detect_cycles` are normalised (#84) but renderers still compared raw `./a.py`, so `--no-ast` cycles got no red ring and collapsed to a bogus `.` node. Node ids, link pairs, and directory collapse now strip `./` (#96).

- **Go stdlib imports no longer resolve onto a unique local package directory.** `import "fmt"` with a scanned `fmt/fmt.go` (or `import "encoding/json"` with `json/json.go`) emitted a do-not-contradict AST edge. Specifiers with no `.` are treated as stdlib and dropped; dotted module paths still suffix-strip per ADR-010 (#95).

- **`--no-tests` no longer drops modules whose names merely contain `test`.** `latest.py`, `contest.py`, and `testing.py` were skipped because the scanner used `"test" in stem`. It now matches real test conventions (`test_*`, `*_test`, `*.test.*`, `tests/` / `test/` / `__tests__/`) and the tree walk uses the same predicate (#94).

- **`GRAPHLM_TIMEOUT` is honoured when the LLM endpoint is passed on the CLI.** `-b/-k/-m` (or `generate_graph(base_url=..., api_key=..., model=...)`) used to build a `Settings` with the dataclass default 300s and skip the env, so a raised timeout never reached pass 2. Timeout now resolves like `--max-context`: flag > env > 300 (#92).

- **OpenSSH private-key filenames are never read.** `id_rsa` / `id_ed25519` / `id_ecdsa` (and the `_sk` variants) have no extension, so they missed both the `.key`/`.pem` list and the `*private*` name glob. The key body was scanned (only the BEGIN/END headers were redacted). Those names are now on the never-read list; `id_rsa.pub` stays scannable.

- **GRAPH.html no longer lets a `</script>` in graph data close the inline script tag.** Node paths and descriptions are hostile input (scanned files / LLM text). They are now JSON-embedded with `<`/`>`/`&` and U+2028/U+2029 escaped so the D3 payload cannot break out of `<script>`.

- **Cycle risk scores no longer collapse to 0 on `./`-prefixed edge paths.** `--no-ast` runs Tarjan on the LLM's `import_edges`, which often carry a `./` prefix; SLOC lookup is keyed by scanner paths without it, so every `sloc_map.get` missed and `log10(total_lines) * length` became 0. Paths are now normalised the same way as faithfulness before scoring (#84).

- **Pass-1 token estimate is now of the prompt actually sent.** `pass1_tokens()` used to score a two-sentence stub plus the raw tree, so `--dry-run`'s `Pass 1 context` line and `meta.usage.pass1.estimated_prompt_tokens` under-counted by ~7x and could not audit the `estimate_tokens` heuristic. It now uses `assemble_pass1_prompt` plus the same message-overhead reserve as pass 2 (#86).

- **Pass-2 file selection no longer substring-matches the wrong files.** A pass-1 request for `a.py` could pack `data.py` into the prompt (`"a.py" in "data.py"`), and two requests could duplicate the same fragment. Matching is now exact path, then `/`-suffix (so `cli.py` finds `app/cli.py` and not `tests/test_cli.py`), then a repo-prefixed request — unique by canonical path (#85).

- **C# namespace `using MyApp.Models;` no longer resolves onto a parent `MyApp.cs`.** The nested-type parent-file probe (`Ns.Type` → `Ns.cs`) ran for every using, so a namespace import preferred `src/MyApp.cs` over the unique-dir `src/MyApp/Models/User.cs`. That probe now runs only for `using static` and `using Alias = …` (ADR-007) (#126).

- **C# `using System;` no longer resolves onto a unique scanned file in `System/`.** The unique-namespace-directory fallback treated a lone `System/Console.cs` as the BCL. `System` / `Microsoft` / `Windows` roots are now dropped as third-party, matching the pack docstring (#100).

### Infrastructure

- **CI and release workflows now use current Node.js 24-based actions.** GitHub was forcibly running the older checkout, artifact, uv setup, coverage, and release-publishing actions under a compatibility runtime and warning that their Node.js 20 runtime was deprecated (#80).
- **Parallel CI jobs now use distinct, reusable uv cache keys.** Matrix and specialist jobs previously raced to reserve the same cold-cache entry, producing failed-save annotations and duplicate upload work (#82).

## [0.4.2] - 2026-09-05

### Fixed

- **Release smoke tests now exercise the built wheel instead of the checkout.** Running the clean-venv commands from the repository root let local source files shadow the installed artifact, so a wheel missing modules or package data could pass validation (#77).
- **Source distributions no longer include local Claude session metadata.** Hatch's default sdist selection could package an untracked `.claude/handoff.md` from a maintainer's workspace; the build now excludes `.claude`, and the generated handoff is ignored by Git (#76).
- **`--max-output-tokens` now applies to pass 1 as well as pass 2.** Large directory trees could exhaust pass 1 at the hard-coded 128000-token default even when the CLI flag or `GRAPHLM_MAX_OUTPUT_TOKENS` raised the documented ceiling, so graph generation failed with an error recommending an override that the failing request ignored (#73).
- **Provenance tests no longer inherit the developer's GPG-signing configuration.** Their disposable repository commits could fail or prompt when global `commit.gpgsign=true`; synthetic setup commits now disable signing locally while real project commits remain signed (#74).

### Infrastructure

- **Release tags are now created only after the release PR and resulting `main` commit pass CI.** The documented `git push --follow-tags` flow could trigger irreversible PyPI publication before the release commit was tested; version bumps now edit files only, and maintainers push one verified signed tag after merge (#78).

## [0.4.1] - 2026-09-05

This patch makes `graphlm --upgrade` use the same installer that put this binary on PATH (`uv tool` / `uv pip` / pip / pipx).

### Fixed

- **`graphlm --upgrade` uses the same installer that installed this binary.** A `uv tool install` is upgraded with `uv tool upgrade` (from `uv-receipt.toml`, without following the `bin/python` symlink out of the venv). A `uv pip install` is upgraded with `uv pip install --upgrade`. pip stays `python -m pip`. 0.4.0 followed `bin/python` into uv's managed CPython, classified a uv-tool install as pip, and failed with `No module named pip` (#71).

## [0.4.0] - 2026-09-04

This release finishes the GitHub top-10 language packs (C#, C/C++, Go, PHP), adds a project `.graphlmignore` and `graphlm --upgrade`, and fixes the pass-2 edge table dropping on deep monorepos.

### Added

- **`graphlm --upgrade`.** Detects whether this binary came from `uv tool`, pipx, or pip, preserves extras (`mcp`, language packs), and upgrades to the latest PyPI release. A source checkout is refused (pull/`uv sync`, or install a release). Flag, not a subcommand, so `graphlm <project>` is unchanged (ADR-012).
- **`.graphlmignore`** — a project-level ignore file (gitignore-lite: one glob per line, `#` comments, blanks skipped) read from the scanned project root and merged into the scanner's exclude set, so a repo can record "always skip this worktree / cache / generated tree" instead of passing `--exclude` on every run. `--no-graphlmignore` opts out. The file itself is never sent to the model. Absent file is a no-op (#38).
- **PHP import edges via `graphlm[php]`.** `use App\Models\User` resolves to `App/Models/User.php` under `src/` roots. Quoted `require`/`include` of a string literal resolve relative to the importer (`kind` `include`). Concatenated `__DIR__ . "/x.php"` paths are dropped and mark the list not exhaustive. The grammar accessor is `language_php` (not `language()`). Without the extra: zero PHP edges, one log line, never a crash.
- **Go import edges via `graphlm[go]`.** `import "example.com/mod/pkg"` resolves to a package directory when **exactly one** non-test `.go` lives there (import path tried longest-first, then suffixes so a module prefix is optional). Multi-file packages are dropped (GRAPH_DIFF fan-out) and mark the list not exhaustive. `import "./rel"` is path-relative. `import "fmt"` is third-party, not partial. Without the extra: zero Go edges, one log line, never a crash.
- **C/C++ `#include` edges via `graphlm[cpp]`.** Quoted `#include "foo.h"` resolves relative to the importing file (with an extension probe). Angle-bracket system headers (`#include <stdio.h>`) are dropped as third-party; macro includes (`#include FOO`) mark the list not exhaustive. One extra pulls both `tree-sitter-c` and `tree-sitter-cpp` (``.c``/``.h`` vs ``.cpp``/``.hpp``/…). `kind` is `include`. Without the extra: zero C/C++ edges, one log line per language, never a crash.
- **C# import edges via `graphlm[csharp]`.** `using static Ns.Type` / `using Alias = Ns.Type` resolve to `Ns/Type.cs`. A namespace `using Ns;` resolves only when exactly one scanned file lives in that namespace directory — two or more files are dropped (same GRAPH_DIFF fan-out reason as Java wildcards) and mark the list not exhaustive. `using System;` and other misses are third-party, not partial. Without the extra: zero C# edges, one log line, never a crash.

### Fixed

- **Directory tree no longer restates the full relative path on every line.** The pass-1/pass-2 tree is an indented listing (`indent + basename`, directories end with `/`). Previously each line repeated the whole path *and* indented, so the 5000-line cap still overflowed `max_context` on deep monorepos — n8n's tree was ~151k tokens against a 131k window, every pass-2 file was truncated, and the AST edge table was dropped with `edge table dropped: header does not fit the 0-token edge budget`. The same tree is now ~72k tokens, the edge table fits, and if a tree still fills the budget the warning names that cause (parser edges still used for cycle detection). Pass 1 is told to reconstruct full paths from the indent when requesting files (#69).

## [0.3.1] - 2026-09-04

This patch stops graphlm from loading a scanned project's `.env` as its own LLM config.

### Fixed

- **LLM config no longer reads a project or working-directory `.env`.** graphlm used to search from cwd upward for a `.env` (#45) so an installed binary could pick up a per-project file. That also meant `graphlm .` inside a repo would load *that repo's* dotenv into the process — including any `GRAPHLM_*` keys it happened to define, and every other secret in the file. Settings now come only from the exported environment and `~/.config/graphlm/.env` (or `$XDG_CONFIG_HOME/graphlm/.env`). A scanned project's `.env` stays a never-read sensitive file, not graphlm's own config. If you configured graphlm via a project `.env`, move those values to the user-level file or export them (#65).

## [0.3.0] - 2026-09-04

This release adds opt-in Tree-sitter language packs so JavaScript/TypeScript, Java, and Rust repos get the same parser-proven import edges Python already had — without pulling grammar wheels into the base install.

### Added

- **JavaScript/TypeScript import edges via `graphlm[js]`.** Until now the scanner already packed `.js`/`.ts`/`.jsx`/`.tsx` files into the pass-2 prompt, but the Tree-sitter pass only extracted Python imports — so the model's claims about a TypeScript repo had no parser ground truth to check against. Installing the optional extra (`uv tool install 'graphlm[js]'`, or `graphlm[all]`) pulls the `tree-sitter-javascript` and `tree-sitter-typescript` wheels; graphlm then extracts `import` / `export … from` / `require()` / dynamic `import()` and resolves **relative** specifiers (`./foo`, `../bar`, including `index.*` barrels) against files that exist in the scan. Bare packages (`react`) are dropped, same rule as Python stdlib, and the pass-2 edge table is labelled *not exhaustive* so the model still infers them. The base install is unchanged: without the extra, a JS/TS repo yields zero parser edges and one log line per language, never a crash, and Python edges on a mixed repo stay intact. `.tsx` files use the TSX grammar (JSX in a `.ts` file is the wrong tree). `require()` edges use kind `require` so they stay distinct from ESM `import` in the graph-vs-graph diff.
- **Rust import edges via `graphlm[rust]`.** `mod foo;` becomes an `include` edge to `foo.rs` / `foo/mod.rs`; `use crate::` / `super::` / `self::` resolve against a filesystem module tree built from the scanned files (`lib.rs`/`main.rs` as crate roots). External crates (`use serde::…`) are dropped like Python stdlib. Inline modules and `#[path]` modules are skipped and mark the list not exhaustive rather than guessing a file. Without the extra: zero Rust edges, one log line, never a crash.
- **Java import edges via `graphlm[java]`.** Same opt-in-extra model as JS/TS: `import com.acme.User;` resolves to `<root>/com/acme/User.java` against files in the scan, with Maven/Gradle source roots (`src/main/java`, `src/test/java`) and the file's `package` declaration as a disambiguator. `import static Type.member` uses kind `static` so it stays distinct in the graph-vs-graph diff. Package wildcards (`import com.acme.util.*;`) are **dropped** — fanning them out would make adding one file to a package mutate every wildcard importer's edge set — and trip the non-exhaustive framing; `import static Type.*;` still resolves to `Type.java` because the class is known. Without the extra, Java files contribute zero parser edges and one log line, never a crash.

### Infrastructure

- `ci.yml` now sets an explicit read-only `permissions: contents: read` at the workflow level. Neither the test nor typecheck job writes anything (no releases, no PR comments, no checks API calls), so this is least-privilege, not a behavior change; addresses CodeQL's `actions/missing-workflow-permissions` finding.

### Docs

- README documents language packs as a first-class install table (`graphlm[js]` / `[java]` / `[rust]` / `[all]`), lists `GRAPHLM_BASE_URL` / `API_KEY` / `MODEL` as required (they have no built-in defaults), and points GitHub-wheel installs at the latest-releases page instead of a stale versioned URL. `.env.example` uses placeholders, not a specific host or model.

## [0.2.0] - 2026-09-03

This release is the output of an innovation pass over the codebase (`INNOVATIONS.md`): the map becomes queryable by agents over MCP, every run stamps its real token usage and an LLM-vs-parser faithfulness score, the map gets a native Mermaid picture, and oversized files are sent as signature skeletons instead of being cut off.

### Added

- **`graphlm --serve` — the map as an MCP server for coding agents.** Until now an agent had to read the whole `GRAPH.md` (tens of thousands of tokens) to answer one question like "who imports `scanner.py`?". `--serve` exposes the generated map over a stdio [MCP](https://modelcontextprotocol.io) server with eight typed, zero-LLM tools — `overview`, `find`, `module`, `neighbors`, `dependents` (blast radius, optionally transitive), `cycles`, `entry_points`, and `staleness` (stamped commit vs current `HEAD`) — each answering in a few hundred tokens. Import edges are unified from both sources and labelled `ast` (parser-proven), `llm` (inferred), or `both`, so an agent can weight them. The server reads `.graphlm/GRAPH.json` (or the `-o` directory), reloads it automatically when a regeneration lands, never calls the LLM, and reports a missing/corrupt map as an actionable tool error rather than a crash. The MCP SDK is an opt-in extra (`uv tool install 'graphlm[mcp]'`); the base install is unchanged, and using `--serve` without it prints the install hint. Register once per repo with `claude mcp add graphlm -- graphlm --serve /path/to/repo`. The `--install-skill` guide now tells the agent to prefer these tools when they are available. The query logic (`graphlm/query.py`) is a plain library surface too, usable without MCP.
- **Run telemetry in the stamp.** Every real run now records, in `GRAPH.json`'s `meta` block, (1) the endpoint's **real token usage** per pass — `prompt_tokens` / `completion_tokens` as the server counted them, stored beside graphlm's own `estimated_prompt_tokens` for the same prompt — and (2) a **faithfulness score**: how well the LLM's `import_edges` agree with the parser's `deterministic_edges` (precision = share of the model's Python import edges the parser confirms; recall = share of the parser's edges the model reproduced). Why it matters: the estimate-vs-real pair makes the `estimate_tokens` heuristic auditable per endpoint instead of guessed at, and the faithfulness score tells a reading agent how much to trust the LLM's edge table on *this* run — a low precision means the model invented dependencies the parser can't see. Both are summarised in one `> **Run telemetry.**` line directly under the refresh directive at the top of `GRAPH.md`, and echoed by the CLI as `Usage:` / `Faithfulness:` lines. Usage comes from `stream_options.include_usage` on the streamed request; an endpoint that doesn't report usage simply leaves the counts `null` ("not reported by endpoint"). The new `meta` fields are additive and optional — no `schema_version` bump, and a prior `GRAPH.json` without them still diffs normally. Not recorded on `--dry-run` (no LLM call) and faithfulness is absent under `--no-ast` (no ground truth).
- **Mermaid module graph in `GRAPH.md`.** The map now carries a picture, not only tables: a `## Module Graph` section right after the directory tree with a Mermaid `flowchart` of the import edges. GitHub (and most Markdown viewers) render Mermaid natively, so the diagram shows up in any repo that commits its map — no CDN, no separate file, works offline, unlike `GRAPH.html`. It draws the parser's Tree-sitter edges when present (labelled *ground truth*) and falls back to the LLM's edges under `--no-ast` (labelled *inferred*), so you always know which you are looking at. To stay legible it is **directory-level** — files collapse to their parent directory, root-level files stay their own node — and caps at 40 directories (highest-degree kept, with a "… N more directories not shown" note). Import-cycle members are drawn in red: edges whose endpoints share a cycle, plus a red outline on any directory containing a cycle member so a cycle inside one package (the common case, which collapses to nothing at directory level) is still visible. Output is sorted, so a regenerated map diffs cleanly
- **Ground-truth layers in `GRAPH.html`.** The interactive graph now draws the parser's `deterministic_edges` as a distinct solid-blue "Parser imports" layer alongside the grey LLM imports and the dashed data flow, with a **Layers** control (three checkboxes) to show or hide each. Every node that is part of an import cycle gets a red ring and a "Member of an import cycle" line in its tooltip. Where the LLM reported the same edge the parser found, only the parser's line is drawn (the duplicate is dropped and the AST link marked *corroborated* in the embedded data), so one relationship is never two lines. The legend and the stats line (edges broken down by parser / LLM / data flow, plus the cycle count) reflect the new layers

### Changed

- Oversized Python files are now sent to the LLM as a **tree-sitter signature skeleton** instead of their first 4000 characters. Previously a file longer than `--max-file-chars` was cut at the cap, so for a 1500-line module the model saw the imports and the first class and nothing else — its `file_summaries`, `symbols`, `entry_points` and `quick_reference` entries for big files were guesses. The skeleton keeps every import, every class/def signature (decorators, multi-line headers, return annotations), the first line of each docstring, and short constants, with bodies elided to `...` and long constants collapsed to `NAME = {…}  # N lines elided`. It is exact where the head was partial, and smaller: on graphLM's own tree the same 80 pass-2 files pack into ~12% fewer estimated tokens (`--no-skeleton` ~73.0k → ~64.1k) while carrying every symbol. The skeleton starts with a `# [graphlm skeleton: …]` marker and the pass-2 prompt tells the model to summarize the API from it and not invent behaviour for elided bodies. Secret redaction still runs on the skeleton (docstring lines and constants can hold secrets). Python only for now — other languages still send the head. `--no-skeleton` / `skeleton=False` restores head-truncation.
- `graphlm/redact.py` now holds the secret handling relocated verbatim from `scanner.py`: the never-read extension/name lists with `_is_sensitive_file`, and the redaction regexes (`_redact_secrets`). The scanner re-imports both, so every candidate file is still screened and every fragment still passes through redaction — the security invariants are unchanged; the move only keeps `scanner.py` under the module size limit.

### Fixed

- Nested git checkouts inside the scanned project — a worktree, a submodule, or a vendored clone (anything whose directory holds a `.git` file or directory) — were scanned as if they were part of the project, so every module and import edge under them was duplicated into the map under a second path prefix (observed with agent worktrees under `.claude/worktrees/`). The scanner now treats such a subtree as a separate project and leaves it out of both the pass-1 tree and the file walk; the scan root itself is unaffected.
- `GRAPH.html` showed only the LLM's edges and never highlighted cycles. The map's *verified* structure — the Tree-sitter import edges and the Tarjan import cycles with risk scores — was computed and written to `GRAPH.md`/`GRAPH.json` but never reached the picture, so the interactive view could contradict the ground-truth table beneath it and gave no visual cue for the cycles the report called out. Both now render (see Added above)
- Import-cycle risk scores under-weighted large files. The `log10(total_lines)` term counted the lines of the *truncated* fragment (cut at `--max-file-chars`), so a 1500-line module in a cycle scored as a ~100-line one — exactly the file a risk score should weigh most. `FileFragment` now carries the real on-disk `line_count`, captured before any truncation or skeletonisation, and cycle scoring uses it.
- `--dry-run` reported `0 import edges` on every run. That figure was `len(graph.import_edges)` — the **LLM's** field, which a dry run never fills — so it always read 0 and looked like "no imports found" even on a project the parser had fully resolved. The stats now show `AST import edges: N` from the parser's `deterministic_edges` (`AST off` under `--no-ast`), and the misleading LLM-field count is gone from the dry-run line.

## [0.1.3] - 2026-08-31

### Fixed

- `--dry-run` mislabeled its file count. The line read `Files scanned: N`, but `N` was the number of files **selected for pass-2 analysis** (capped at `--max-pass2-files`, default 80), not the number of files scanned (bounded by `--max-files`, default 200). On any project with more than 80 source files the two differ, so the label under-reported the scan and read as a smaller repo than was actually walked. The line now reads `Files selected for pass-2 analysis: N`.
- Config from a `.env` file was silently ignored for installed users (#45). `config.py` loaded `.env` with a bare `load_dotenv()`, which searches upward from the *package's own directory* — so once graphLM was `uv tool install`ed, it looked next to its site-packages install, never at the project you were mapping. Two consequences, both fixed: (1) a **project `.env`** is now found from the **current working directory** upward (`find_dotenv(usecwd=True)`), so it works from an installed graphLM, not only from a source checkout; and (2) a new **user-level fallback** at `~/.config/graphlm/.env` (or `$XDG_CONFIG_HOME/graphlm/.env`) lets you configure an installed graphLM once instead of dropping a `.env` in every project. Precedence, first non-empty wins: exported shell env > project `.env` (cwd upward) > user-level `.env` > built-in defaults; higher sources are never clobbered. (Running with the repo as your working directory is unchanged.)

### Docs

- Design docs for multi-language AST support (#42): `docs/plans/multi-language-support.md` (research + recommendation) and `docs/plans/multi-language-implementation.md` (phased build plan). No code change yet — this is the approved design for adding deterministic import-edge extraction beyond Python. Model: **Python is the only core language** (the sole grammar in the base install); every other language is an **opt-in pip extra** (`graphlm[js]`, `graphlm[java]`, `graphlm[rust]`, `graphlm[all]`) with a bundled, graphlm-authored resolver — no third-party plugin API. Planned packs, in build order: JS/TS, Java, Rust.

### Infrastructure

- Release automation via [bump-my-version](https://github.com/callowayproject/bump-my-version) (`[tool.bumpversion]` in `pyproject.toml`). `uvx bump-my-version bump patch|minor|major` now bumps the version, promotes the `[Unreleased]` changelog section to a dated release header, updates the compare links, and makes a GPG-signed commit + signed tag — the mechanical release steps that were done by hand. `uv lock` and `git push --follow-tags` remain manual. See the "Releasing" section in `CONTRIBUTING.md`

## [0.1.2] - 2026-08-31

Maintenance release: `--install-skill` guide refinements from testing it across
Claude, Codex, and Grok on a real repo.

### Changed

- `--install-skill` guide improvements, from testing it across Claude, Codex, and Grok:
  - **Stronger trigger.** The skill `description` now tells the agent to reach for the map at the **start** of working in a codebase — before reading or searching through files — rather than only when explicitly asked. This is safe now that the guide handles an unmapped repo gracefully (it no longer risks a mid-task stall).
  - **Data-egress heads-up.** The guide now notes that `graphlm .` **sends selected repository code to the configured LLM endpoint** (`GRAPHLM_BASE_URL`). It still generates without stalling for a local/trusted endpoint, but says to surface the destination and get a quick OK when the endpoint is a third-party API — so a private codebase isn't exported silently. (A Codex safety layer flagged exactly this.)
  - Reinstall the updated guide with `graphlm --install-skill claude --skill-force`.

## [0.1.1] - 2026-08-31

Maintenance release: a fix to the `--install-skill` agent guide, plus the
community-health docs and role-address contact change made after 0.1.0 shipped.

### Changed

- `--install-skill` guide now distinguishes the two invocation modes so an agent doesn't stall or misfire. When it's invoked **explicitly** (the user ran the skill/command directly) and no map exists, the guide says to generate one with `graphlm .` **without asking** — a safe, local, idempotent action — then read and summarize it. When the skill is **reached for mid-task**, it says *not* to generate (a fresh generation streams for a minute or two and would stall the user's real request) — just read an existing map, or note in one line that none exists. Fixes an agent freezing into a "what do you want to do?" menu when the skill was run in a repo with no map. Reinstall the updated guide with `graphlm --install-skill claude --skill-force`
- Package author contact is now a role address (`graphlm@519lab.com`) instead of a personal one, and the Code of Conduct routes to `conduct@519lab.com`. (v0.1.0's PyPI metadata carried the old address; a published version's metadata can't be edited, so this takes effect from the next release.)

### Added

- Community-health docs for the now-public repo: `CONTRIBUTING.md` (dev setup, the real test/mypy commands, branch/PR/commit conventions, and the security invariants contributors must not weaken), `SECURITY.md` (private vulnerability reporting via GitHub's advisory flow, with an in/out-of-scope threat model given graphlm reads untrusted code), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), a PR template with a verification checklist matching this repo's conventions, and issue forms (bug / feature) that route security reports away from public issues. README gained Contributing and Security sections

## [0.1.0] - 2026-08-30

First public release. graphlm is installable from PyPI (`uv tool install graphlm`
/ `pipx install graphlm`) and from the attached GitHub Release artifacts; the
`graphlm` command lands on your PATH. Everything below shipped in 0.1.0.

### Added

- **Packaged for release**: published to PyPI and GitHub Releases from a single
  git tag via a Trusted-Publishing workflow (`.github/workflows/release.yml`) —
  no stored API token. The release build installs the wheel into a clean venv
  and smoke-tests `graphlm --version` + a `--dry-run` before publishing, so a
  missing entry point, data file, or dependency fails the release instead of
  reaching users
- `--version` / `-V` flag: prints the installed graphlm version and exits
- `--install-skill <harness>` flag: drops a guide that teaches a coding agent how
  to use graphlm and to look for its map (`.graphlm/GRAPH.md`) when loading a
  codebase — regenerating it with `graphlm .` when absent or stale. Targets
  `claude` (writes `~/.claude/skills/graphlm/SKILL.md`) and `codex` (writes
  `~/.codex/graphlm.md` and prints a one-line snippet to include from your own
  `AGENTS.md`). Installs user-global by default (`--skill-local` writes into the
  scanned project instead); idempotent (skip-if-exists unless `--force`). It only
  ever creates graphlm's own files — it never edits your existing `CLAUDE.md` /
  `AGENTS.md`, and it refuses to write *through* a symlink at the target (so a
  dotfiles-managed `~/.claude/skills/` can't be clobbered) (#33)
- **Self-refreshing graph**: every generated `GRAPH.md` now carries a top-of-file *provenance & refresh directive*, and `GRAPH.json` a versioned `meta` block, recording when the map was generated and against which git commit. A coding agent reading `GRAPH.md` can compare the repo's current `HEAD` to the stamped commit and regenerate (`graphlm .`) when they differ — the agent is the scheduler; graphlm adds no hook, flag, or staleness logic of its own. Non-git projects degrade gracefully (no SHA; the directive falls back to the agent's judgment). The directive is advisory. Wording is deliberately "generated against commit X" (not "reflects X") because the map is built from files on disk, which may include uncommitted changes — a graph can be SHA-fresh yet not match the working tree
- **Graph-vs-graph diff (`GRAPH_DIFF.md` / `GRAPH_DIFF.json`)**: every real run now also writes a structural diff of the map — modules, import edges (LLM and AST), import cycles, data flows, entry points, and file summaries **added and removed** since the prior `GRAPH.json`. This answers "what changed in the map since last time?" at a glance (a new entry point, a dropped module, a broken/added import cycle) without re-reading the whole graph. It reads graphlm's *own* prior output — which is why the `meta` block was made a versioned input contract. It is **not** a code diff (git does that better): added/removed only, so a pure prose rewrite (a description, a summary) is intentionally invisible; renames show as remove+add (no rename heuristics). Three baseline states are always distinguished so an agent can tell them apart: *first run* ("initial graph — no prior version"), *uncomparable* (a corrupt or unrecognized-`schema_version` prior file — never masqueraded as a first run), and *normal*. The diff header carries the old→new commit-SHA range. Toggling `--no-ast` between runs does not fabricate a mass edge deletion (the AST dimension reports "not compared" when either side skipped AST). On by default; `--no-diff` / `include_diff=False` opts out. `--dry-run` writes no diff (it produces no authoritative graph). No new network or LLM call — pure local computation over the two graphs (#28, ADR-002)
- `--timeout` CLI flag / `GRAPHLM_TIMEOUT` env var to configure the LLM request timeout (default raised to 300s). Resolves `--timeout` > `GRAPHLM_TIMEOUT` > 300, mirroring `--max-context` (#18)
- `--max-output-tokens` CLI flag / `GRAPHLM_MAX_OUTPUT_TOKENS` env var to configure the graph output-token ceiling. Defaults to the model's practical max (128000) so large graphs don't truncate; it is a request ceiling only (the model stops when done), independent of the input budget (#18, #25). Raise it only on an endpoint that bounds input+output together
- Streaming LLM responses: `call_llm` now sends `stream: true` and reassembles the SSE deltas. This keeps a long generation alive past proxy read-timeouts and adds a clear "output truncated (hit max_tokens)" error instead of a confusing parse failure. A server that ignores the flag and returns a buffered body is handled transparently (#18)
- Two-pass LLM strategy: pass 1 sends directory tree only to identify key files, pass 2 sends tree + selected files for full analysis
- CLI tool via Typer with `--dry-run`, `--no-tests`, `--exclude`, and `--max-files` options
- Library API via single `generate_graph()` function returning `GraphResult`
- Pydantic v2 models for structured graph data (import edges, modules, data flow, DB schema, test mapping, architecture notes, quick reference)
- Project scanner with smart file ranking (config files > package init > source > tests)
- Token estimation heuristic (~4 UTF-8 bytes per token)
- LLM client with JSON recovery (strips code fences, finds braces in text)
- Retry logic with exponential backoff on connection errors
- Markdown and JSON output rendering
- System prompt with injection guard (treats file content as data only)
- Test fixtures: small, medium, and large project directories
- Tree-sitter-based AST parser for deterministic import/edge extraction (Python, JS, TS)
- `--ast` CLI flag: enables AST-derived deterministic import edges alongside LLM analysis
- Import cycle detection via Tarjan's SCC algorithm with risk scoring based on module size and cycle length
- `--no-show-cycles` / `--cycle-threshold` CLI flags for cycle control
- Interactive D3 force-graph HTML visualization output with zoom/pan, hover tooltips, click highlighting, search, and dark/light mode
- `--no-html` CLI flag (HTML enabled by default)
- New `deterministic_edges` and `import_cycles` fields on `CodebaseGraph` model

### Changed

- CLI writes its output into a **`.graphlm/` subdirectory of the scanned project** by default (not the process working directory, and no longer the project root) — so `GRAPH.md` / `GRAPH.json` / `GRAPH.html` / `GRAPH_DIFF.*` stay out of the way in one tidy folder. `-o <dir>` still overrides and is honored literally (no `.graphlm` appended). The whole `.graphlm/` directory is excluded from scanning, so a re-run never ingests its own map. Note the map now lives at `.graphlm/GRAPH.md`; agents/tooling looking for `GRAPH.md` at the project root should look in `.graphlm/`. The library API is unchanged — `generate_graph(output_dir=...)` / `result.write(dir)` still write to the literal directory given
- Output files are `GRAPH.md`, `GRAPH.json`, and `GRAPH.html` (were `graphs.md` / `graphs.json` / `graph.html`)
- AST import parsing is on by default; pass `--no-ast` or `ast=False` to skip. The `--ast` flag is removed
- `GraphResult.write()` / `write_outputs()` now return a `WriteResult` — the `(md, json, html_or_none)` 3-tuple you can still unpack three ways, now carrying `.diff_md` / `.diff_json` attributes for the graph diff (#28). (This supersedes the earlier `tuple[Path, Path, Path | None]` return added with HTML; the positional arity is unchanged, so `md, json, html = result.write(...)` keeps working)
- `write_outputs()` now optionally writes HTML alongside Markdown and JSON

### Fixed

- graphlm produced **no graph at all** against a served model that needs a structured-output constraint (its own default Qwen endpoint): pass 2 relied on prompt-only instruction to make the model emit a `CodebaseGraph`, but never sent a `response_format` in the request. `Qwen3.6-35B` returned a near-empty `{"database_schema": null}` and the run failed with a schema-validation error, writing nothing. Pass 2 now sends `response_format: {type: json_schema}` (gated on a structured response being requested, so pass 1's free-form file-list request is untouched). The endpoint treats it as a guided-JSON hint, so the prompt's "return an empty directory_tree" and the locally-filled `meta`/`import_cycles`/`deterministic_edges` still come back empty — #18 is not reopened. Endpoints that reject the parameter (HTTP 400) fall back once to prompt-only, so a prompt-following endpoint still works; 401/403/404/429 still surface as errors. Verified end-to-end against the live Qwen endpoint (was invisible to CI, which mocks the LLM) (#31)
- graphlm ingested its own output on a re-run: `GRAPH.md` / `GRAPH.json` / `GRAPH.html` sit in the scanned directory but were never excluded, so a second run over an already-mapped project fed its own previous map into the LLM as source. These artifacts (and the new `GRAPH_DIFF.*`) are now always excluded from scanning. The exclusion is by exact name, not a broad `GRAPH*` glob, so a user's `GRAPHICS.md` / `GRAPHING.md` etc. are untouched (#28)
- Sensitive-file read gap: arbitrary `.env.<name>` files (e.g. `.env.qa`, `.env.test`) were scanned into LLM context because the sensitive-file check used a fixed allowlist. Any dotenv file is now treated as secret-bearing, except the non-secret templates `.env.example` / `.env.sample` / `.env.template` / `.env.dist` (#10)
- `GRAPHLM_MAX_CONTEXT` had no effect: it was parsed into `Settings` but never read, so the pass-2 context budget was always 120000. The budget now resolves as `--max-context` flag > `GRAPHLM_MAX_CONTEXT` env var > 120000 (#11)
- Pass-2 prompt could exceed `max_context` (#12): per-file admission respected the budget, but the AST-edge table was appended uncapped and the instruction block was never counted, so a project with many import edges produced an over-budget prompt (a 6000-edge table alone reached ~62k tokens regardless of the budget). Now every fixed section is reserved up front and the edge table is capped at a bounded share of the budget; when the table is truncated, its framing changes to tell the model the list is not exhaustive so it still infers the dropped edges. The full edge list is unaffected — it still reaches `graph.deterministic_edges` and cycle detection. Also removes spurious truncation of files that would have fit
- External symlinked *files* pointing outside the project were listed in the directory tree and consumed a `max_files` slot (only symlinked directories were guarded). Any symlink escaping the project is now skipped in the tree walk; file content was already blocked before reading
- The deterministic AST import graph came back **empty on real Python projects** (0 edges, 0 cycles), silently — the LLM's inferred edges masked it (#19). Two compounding causes: (a) file ranking gave documentation the same priority as source, so on a doc-heavy repo (argus: 91 `.md` vs 135 `.py`) markdown crowded source out of the `max_files` scan, and edges only resolve between *scanned* files — source now outranks non-source text; (b) src-layout projects (package under `src/`) import by package name (`from mypkg.core import X`) but the scanned file is `src/mypkg/core.py`, so no candidate matched — the resolver now derives source-root prefixes (`src/`, …) from the scan and tries each. argus went from 0 to 342 deterministic edges; root-layout projects are unchanged (root `""` is tried first)
- graphlm overflowed the LLM context on large / polyglot / build-heavy repos, failing with an upstream "Context size has been exceeded" error and producing no graph at all (#17). Two compounding causes, both measured against the real model server: (1) the **pass-1 directory tree was unbounded** — `max_files` capped only the files *read* in pass 2, never the tree, and `_ALWAYS_EXCLUDE` missed the big build/cache dirs (`target/`, `.hypothesis/`, `dist/`, `build/`, `.ruff_cache/`, `.next/`, `coverage/`, …), so a repo's tree alone could reach ~400 KB / ~140k tokens; (2) the **token estimate under-counted by ~28%** — `estimate_tokens` assumed 4 bytes/token but real tree+code content measures ~2.83, so a "within budget" prompt actually ran far over. Fixes: the exclude set now covers the common build/cache dirs; the tree is bounded by a per-directory cap (200 listed children, spread so nested source stays visible) plus an absolute 5000-line total ceiling; and `estimate_tokens` is recalibrated to ~2.5 bytes/token (over-estimating so the budget is a real guarantee) and unified into a single implementation (was duplicated in `scanner.py` and `context.py`). Verified end-to-end: a repo that overflowed now assembles a pass-1 prompt ~8× smaller and completes pass 1 in well under the context window
- Pass 2 on a large project failed to produce a graph, in two stages, both fixed under #18:
  - **Transport (HTTP 524):** after the context-overflow fix, the model's full-graph generation legitimately took >120s and the non-streamed request died at Cloudflare's edge read-timeout while the origin was still generating. The response is now streamed, which resets that timeout on every delta, and the client timeout default is raised to 300s (configurable via `--timeout` / `GRAPHLM_TIMEOUT`). Measured: a generation that 524'd at ~125s buffered now completes in ~200s streamed.
  - **Output truncation:** the graph was cut off mid-generation because the model's output exceeded the 16000-token `max_tokens` ceiling. Two compounding causes: (a) the pass-2 prompt told the model to *echo the entire directory tree* back inside its JSON (~20k output tokens for the argus tree — invariant to `--max-pass2-files`, since tree size doesn't depend on file count); the model is now told to return an empty `directory_tree` and `generate_graph` fills it from the scan locally (it already has it). (b) even without the echo, a real project's analysis needs more than the old 16000-token ceiling; the ceiling is now configurable (`--max-output-tokens` / `GRAPHLM_MAX_OUTPUT_TOKENS`) and defaults to the model's practical max (see #25). A response that still hits the ceiling now raises a clear "graph too large — raise --max-output-tokens" error instead of a confusing parse failure.
- The output ceiling was needlessly capped low **and** was double-counted against the input budget (#25). graphlm reserved `max_output_tokens` out of `max_context` on the assumption that input and output share one context window — **false on the target endpoint** (measured: 180k input + 200k `max_tokens` = 380k combined is accepted). Fixes: (1) the output ceiling defaults to the model's practical max (128000) so large graphs don't truncate — it's a request ceiling, not a reservation, so a high value is free; (2) input file admission no longer subtracts the output budget, only a small message-overhead reserve, which raises effective input capacity by ~38% (more files analyzed per run). The pass-2 output reserve and the requested `max_tokens` are no longer coupled. Endpoints that *do* bound input+output together (vLLM, Anthropic) can lower `--max-output-tokens`.
- `--max-output-tokens` help text was stale: it still said the default was 32000 and that the value "reserves that much of the pass-2 context" — both untrue since #25/#26 (default is 128000; the ceiling is independent of the input budget). Corrected in the CLI help and `CLAUDE.md`

### Changed

- `--max-context` CLI flag now defaults to unset (falls back to `GRAPHLM_MAX_CONTEXT`, then 120000) instead of a hardcoded 120000, so the env var is honored
- HTML threw `TypeError: e is not iterable` on load: D3 v7 `scaleOrdinal(null, palette)` iterates a null domain; use `scaleOrdinal(palette)` as the range
- HTML visualization did not initialize: `initGraph` was never called on page load, and a recursive self-call could hang the page
- `--ast` computed import edges then discarded them; cycle detection ran on LLM edges without SLOC-based risk scores
- `--no-html` still wrote `graph.html` when `-o` was set (HTML was written twice)
- `result.write()` returned three paths but the README unpacked two; string output paths failed
- AST import resolver mapped packages to non-existent `pkg.py` files and stdlib modules to `os.py`
- LLM copied test-fixture database schemas into the host project graph

### Infrastructure

- Python 3.11+ with hatchling build, uv dependency management
- pytest-httpx for mock HTTP integration testing
- pytest + coverage in CI
- `.env.example` for LLM endpoint configuration
- GitHub Actions CI testing on Python 3.11, 3.12, 3.13 with coverage upload to Codecov
- mypy type checking in CI
- Removed stale generated artifacts (`graphs.md`, `graphs.json`, `graph.html`) left over from before the `GRAPH.*` output rename, and the committed `.coverage` database; the repo no longer ships tool output. Added `.coverage`, `coverage.xml`, and the `GRAPH.*` output files to `.gitignore` so generated artifacts stay out of version control

[Unreleased]: https://github.com/ggrace519/graphLM/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/ggrace519/graphLM/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/ggrace519/graphLM/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/ggrace519/graphLM/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/ggrace519/graphLM/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/ggrace519/graphLM/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/ggrace519/graphLM/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ggrace519/graphLM/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ggrace519/graphLM/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/ggrace519/graphLM/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/ggrace519/graphLM/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ggrace519/graphLM/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ggrace519/graphLM/releases/tag/v0.1.0
