# graphLM

**Point it at a codebase. Get back a map.**

You've cloned an unfamiliar repo and you're staring at 400 files wondering where anything *is*. graphLM reads the project the way you would — but faster — and hands you a map: what the modules are, how they depend on each other, where data flows, which imports form nasty little cycles, and "where do I find X?" answers. It comes out as **Markdown** to read, **JSON** to script against, and an **interactive HTML graph** to click around in.

It's built for the age of coding agents, too: the map stamps itself with the git commit it was generated against, so an agent (or you) can tell at a glance when it's gone stale and regenerate. Under the hood it pairs an OpenAI-compatible LLM with deterministic Tree-sitter parsing — the AST is ground truth the model isn't allowed to contradict, so the dependency edges are real, not hallucinated.

```console
$ graphlm ~/code/some-project
Scanning ~/code/some-project...
Wrote .graphlm/GRAPH.md, .graphlm/GRAPH.html
```

By default graphlm writes the Markdown map (`GRAPH.md`) and the interactive HTML — the `.md` is the map you feed a coding agent (the JSON's node/link arrays burn a context window faster). Add `--json` for the machine-readable `GRAPH.json`. The map is written into a `.graphlm/` folder inside the project (so it stays out of your way); point it elsewhere with `-o`.

## What it produces

- **Directory tree** — annotated tree of the project
- **Import edges** — dependency relationships between files
- **Modules** — named components and what they do
- **Data flow** — how data moves through the system
- **Database schema** — tables and columns (if applicable)
- **Test organization** — test files mapped to what they cover
- **Architecture notes** — key decisions and patterns
- **Quick reference** — "where do I find X?" lookups
- **Import cycles** — strongly-connected components with SLOC-based risk scores
- **Mermaid module graph** — a directory-level `flowchart` of the parser's import edges inside `GRAPH.md`, with import-cycle members in red. GitHub renders it natively, so a committed map shows a picture with no CDN and no extra file
- **Interactive HTML** — D3 force graph (`GRAPH.html`) with zoom/pan, search, theme toggle, and layer toggles for parser-proven imports vs LLM-inferred imports vs data flow; cycle members are ringed red
- **Provenance stamp** — `GRAPH.json` records when and against which git commit the map was generated, and `GRAPH.md` opens with a refresh directive so a coding agent can tell when the map is stale (see [Self-refreshing graph](#self-refreshing-graph))
- **Graph-vs-graph diff** — every run also writes `GRAPH_DIFF.md` (and `GRAPH_DIFF.json` with `--json`): what changed in the *map* (modules, edges, cycles, data flows, entry points, file summaries added and removed) since the prior run, so you see a new entry point or a broken import cycle at a glance without re-reading the whole graph (see [Graph diff](#graph-diff))

## Install

graphLM is a Python 3.11+ CLI. The friendliest way to get it on your PATH is a tool installer that keeps it in its own isolated environment:

```bash
uv tool install graphlm      # via uv (https://github.com/astral-sh/uv)
# or
pipx install graphlm         # via pipx
```

Either one gives you a global `graphlm` command. Prefer plain pip? `pip install graphlm` works too — just mind your virtualenvs. Later, `graphlm --upgrade` bumps that install to the latest PyPI release and keeps extras.

Want your coding agent to *query* the map over MCP (see [Serve the map to your agent](#serve-the-map-to-your-agent-mcp))? Install the `mcp` extra: `uv tool install 'graphlm[mcp]'`. Parser edges for languages other than Python are opt-in extras — see [Language packs](#language-packs).

**First run.** The first time you run graphlm interactively it offers a quick setup — pick the optional packs you want (language grammars, the MCP server, TypeSafe prose-scoring) and it installs them with the installer you used. Run it again any time with `graphlm --setup`. It never installs anything without asking, and a piped/CI run skips it with a one-line hint.

**No PyPI, no problem.** Every release also ships the wheel and sdist as [GitHub Release](https://github.com/ggrace519/graphLM/releases/latest) assets. Grab the latest `graphlm-*.whl` from that page and `pipx install` the file (or its URL).

**Hacking on graphLM itself?** Clone it and let `uv` sync the dev deps. Add the extras if you want the MCP and language-pack tests to run rather than skip:

```bash
git clone https://github.com/ggrace519/graphLM && cd graphLM
uv sync --group dev --extra mcp --extra all
uv run graphlm --version
```

## Language packs

Python import edges ship in the base install. JavaScript/TypeScript, Java, Rust, C#, C/C++, Go, and PHP are **opt-in extras** that pull only the Tree-sitter grammar wheel; the resolver is in-tree. Without the extra, those files still go to the model but contribute no parser edges (one log line per language, never a crash). `graphlm[all]` installs every language pack; it does **not** include `mcp`.

| Extra | Languages | What the parser resolves |
|---|---|---|
| *(base)* | Python | `import` / `from … import` against files in the scan |
| `graphlm[js]` | JavaScript, TypeScript (`.js` / `.jsx` / `.ts` / `.tsx`) | relative `import` / `export … from` / `require()` / dynamic `import()` (bare packages like `react` are dropped) |
| `graphlm[java]` | Java | fully-qualified `import` against Maven/Gradle source roots; `import static` as kind `static`; package wildcards are dropped |
| `graphlm[rust]` | Rust | `mod foo;` as an `include` edge; `use crate::` / `super::` / `self::` against the filesystem module tree (external crates dropped) |
| `graphlm[csharp]` | C# | `using static` / type aliases to `Type.cs`; a namespace `using` only when exactly one scanned file lives in that namespace directory (multi-file namespaces dropped) |
| `graphlm[cpp]` | C, C++ | quoted `#include "foo.h"` relative to the importer (angle-bracket system headers dropped) |
| `graphlm[go]` | Go | `import "mod/pkg"` to a unique-file package directory (multi-file packages dropped); `./rel` is path-relative |
| `graphlm[php]` | PHP | `use App\Models\User` to `User.php`; quoted `require`/`include` relative to the importer |
| `graphlm[all]` | all of the above | |

```bash
uv tool install 'graphlm[js]'         # or [java], [rust], [csharp], [cpp], [go], [php], [all]
uv tool install 'graphlm[mcp,all]'    # MCP server + every language pack
```

## Quick start

graphLM needs an OpenAI-compatible LLM endpoint to do its thing. Point it at one with three environment variables (or the matching `-b` / `-k` / `-m` flags):

```bash
export GRAPHLM_BASE_URL="https://your-endpoint/v1"
export GRAPHLM_API_KEY="sk-..."
export GRAPHLM_MODEL="your-model-name"

graphlm ~/code/some-project        # writes the map into ~/code/some-project/.graphlm/
```

Want to see what it *would* send the model without spending a token? Add `--dry-run` — it scans, parses the AST, and prints the context stats, no network call. See [Configuration](#configuration) for the full list of settings and the user-level `~/.config/graphlm/.env`.

## Usage

### CLI

```bash
# Analyze a project; writes GRAPH.md + GRAPH.html into <project>/.graphlm/ (add --json for GRAPH.json)
graphlm /path/to/project

# Write to a different directory
graphlm /path/to/project -o ./output

# Dry run — see context stats without calling the LLM
graphlm /path/to/project --dry-run

# Override LLM settings from the command line
graphlm /path/to/project -b https://api.example.com/v1 -k sk-xxx -m my-model

# Exclude test files and custom patterns
graphlm /path/to/project --no-tests --exclude __pycache__ --exclude .git

# Skip a project-level .graphlmignore
graphlm /path/to/project --no-graphlmignore

# Skip Tree-sitter AST import edges
graphlm /path/to/project -o ./output --no-ast

# Skip writing GRAPH.html
graphlm /path/to/project -o ./output --no-html

# Skip writing the GRAPH_DIFF.* graph-vs-graph diff
graphlm /path/to/project --no-diff

# Upgrade this install to the latest PyPI release (keeps extras)
graphlm --upgrade
```

### Library API

```python
from graphlm import generate_graph
from pathlib import Path

result = generate_graph("/path/to/project")
written = result.write(Path("./output"))
md_path, json_path, html_path = written
# html_path is Path | None (None if include_html=False)
diff_md = written.diff_md      # Path | None (None if include_diff=False)
diff_json = written.diff_json  # Path | None
```

`GraphResult.write` accepts `str | Path` and returns a `WriteResult` — the `(Markdown, JSON, HTML)` path tuple you can unpack three ways, with `.diff_md` / `.diff_json` attributes for the graph diff (`None` when `include_diff=False`).

```python
result = generate_graph(
    "/path/to/project",
    base_url="https://api.example.com/v1",
    api_key="sk-xxx",
    model="your-model-name",
    output_dir="./output",
    ast=True,              # Tree-sitter import edges + SLOC cycle scores (default)
    include_html=True,     # skip GRAPH.html when False (default: write it)
    include_diff=True,     # skip GRAPH_DIFF.* when False (default: write it)
    show_cycles=True,      # skip the cycle section when False
    cycle_threshold=0.0,   # min cycle risk score
)

print(len(result.graph.modules), "modules found")
```

AST parsing is on by default: graphLM runs Tree-sitter, attaches `graph.deterministic_edges`, passes those edges into the pass-2 prompt as ground truth, and runs cycle detection on the AST edges with SLOC-based risk scores. Pass `ast=False` or `--no-ast` to skip. `include_html=False` skips writing `GRAPH.html` when `output_dir` is set (HTML is on by default).

#### Embedding graphLM in another application

Three pieces let a host (an agent framework, an IDE, a desktop app) use graphLM without handing it credentials:

```python
from graphlm import LLMReply, generate_graph
from graphlm.view import graph_view

# 1. Instant, free map: directory tree + Tree-sitter import edges + import
#    cycles. No LLM call, no endpoint settings, no config file read.
quick = generate_graph("/path/to/project", structure_only=True)

# 2. Full map through the host's own model access. graphLM builds the
#    prompts and validates the reply; the host only moves text.
def my_backend(system_prompt, user_prompt, *, json_schema, schema_name,
               timeout, max_output_tokens):
    text, usage = host.chat(system_prompt, user_prompt, schema=json_schema)
    return LLMReply(text, {"prompt_tokens": usage.input,
                           "completion_tokens": usage.output})

full = generate_graph("/path/to/project", llm=my_backend,
                      include_evidence=False, include_importance=False)

# 3. The drawable view any renderer can use — the same data GRAPH.html embeds.
view = graph_view(full.graph)   # {"nodes": [...], "links": [...], "cycles": n, "provenance": {...}}
```

With `llm=` or `structure_only=True`, graphLM never resolves `GRAPHLM_*` endpoint settings and never reads `~/.config/graphlm/.env`. `import graphlm` itself never reads that file; only the `graphlm` CLI and a `generate_graph()` call that uses graphLM's own HTTP client do.

## How it works

graphLM uses a **two-pass LLM strategy** to stay within context windows while still producing comprehensive graphs:

1. **Pass 1** — The directory tree (no file contents) is sent to the LLM, which identifies the most important files to read.
2. **Pass 2** — The tree + those key files are sent to the LLM, which produces the final structured graph.

This keeps the first pass lightweight (~tree tokens) and ensures the second pass only includes files that matter.

A Tree-sitter pass runs by default (Python imports always; JavaScript/TypeScript with `graphlm[js]`; Java with `graphlm[java]`; Rust with `graphlm[rust]`; C# with `graphlm[csharp]`; C/C++ with `graphlm[cpp]`; Go with `graphlm[go]`; PHP with `graphlm[php]`). It does not replace the LLM: the two-pass analysis still runs, and AST edges are extra ground truth plus cycle detection. Pass `--no-ast` to skip. Without a language extra, those files are still sent to the model but contribute no parser edges.

Big files are sent as **signature skeletons**, not heads. A file longer than `--max-file-chars` (default 4000) used to be cut at the cap, so the model saw the imports and the first class of a large module and guessed the rest. Now a Python file over the cap is rendered with Tree-sitter as its API surface — every import, every class/def signature (decorators and multi-line headers intact), the first line of each docstring, short constants — with bodies elided to `...`. That is exact where the head was partial, and usually smaller. The skeleton starts with a `# [graphlm skeleton: …]` marker, and the pass-2 prompt tells the model to summarize the API from it rather than invent behaviour for the elided bodies. Secret redaction runs on the skeleton too. Python only for now (other languages still send the head); `--no-skeleton` restores head-truncation.

## Teach your coding agent to use it

The map is most useful when your coding agent reads it *automatically* before it starts spelunking through a codebase. One command sets that up:

```bash
graphlm --install-skill claude    # writes ~/.claude/skills/graphlm/SKILL.md
graphlm --install-skill codex     # writes ~/.codex/graphlm.md + a snippet to paste into AGENTS.md
```

It drops a short guide telling the agent to look for `.graphlm/GRAPH.md` when it opens a repo, follow the map's refresh directive, and regenerate with `graphlm .` when the map is missing or stale. Installs **user-global** by default (so every repo benefits); add `--skill-local` to write into the current project instead, and `--skill-force` to overwrite an existing guide.

graphLM only ever creates its *own* files — it will **never** edit your existing `CLAUDE.md` or `AGENTS.md`. For Codex (whose config is a user-owned `AGENTS.md`), it writes a standalone guide and prints the one line for you to paste in yourself.

## Serve the map to your agent (MCP)

Reading `GRAPH.md` costs an agent the whole document — tens of thousands of tokens — to answer one question. `graphlm --serve` exposes the same map as a stdio [MCP](https://modelcontextprotocol.io) server with typed, zero-LLM tools, so the agent asks "who imports `scanner.py`?" and gets a few hundred tokens back:

| Tool | Answers |
|---|---|
| `overview` | counts, provenance, most-imported files, entry points, architecture notes |
| `find` | "where is X?" — ranked hits across quick-reference, modules, symbols, summaries |
| `module` | everything the map knows about one file (accepts a unique suffix like `cli.py`) |
| `neighbors` | what a file imports / what imports it, each edge labelled `ast` (parser-proven), `llm`, or `both` |
| `dependents` | blast radius — direct importers, or transitive with distances |
| `cycles`, `entry_points` | the import cycles (by risk) and every entry point |
| `staleness` | stamped commit vs current `HEAD`: `fresh` / `stale` / `unknown` |

```bash
uv tool install 'graphlm[mcp]'                       # the extra pulls in the MCP SDK
graphlm .                                            # generate the map first (serving never calls the LLM)
claude mcp add graphlm -- graphlm --serve /path/to/repo   # register with Claude Code (once per repo)
```

The server reads graphlm's internal working copy `<project>/.graphlm/.graph-state.json` (or the `-o` directory) — written on every run regardless of `--json` — and picks up a regenerated map automatically — no restart. It never runs the LLM: if there is no map yet it says so and tells the agent to run `graphlm .`. The `--install-skill` guide tells the agent to prefer these tools when they are registered.

## Self-refreshing graph

A generated graph goes stale the moment the code moves on. graphLM makes the
output *self-refreshing without any hook or flag*: it stamps its own provenance
and rides the refresh nudge along in the loop an agent already uses to read
`GRAPH.md`.

- **The stamp.** `GRAPH.json` carries a versioned `meta` block — `created_at`
  (UTC), `commit_sha` (the git `HEAD` the graph was generated against, or `null`
  outside a git repo), `graphlm_version`, and `schema_version`. `GRAPH.md` opens
  with a short **refresh directive** rendered from that stamp.
- **The agent is the scheduler.** graphLM has no staleness logic — invoked, it
  always regenerates and re-stamps. The directive tells a reading agent to
  compare the repo's current `git rev-parse HEAD` to the stamped commit and, if
  they differ, regenerate with `graphlm .`. Staleness = SHA mismatch.
- **It's advisory.** The agent may ignore the directive; the map is best-effort,
  not guaranteed current. Non-git projects have no SHA, so the directive falls
  back to "regenerate when you believe the code has changed."
- **Honest wording.** The stamp says "generated *against* commit X", not
  "reflects X": the graph is built from files on disk, which may include
  uncommitted changes, so a graph can be SHA-fresh yet not match the working
  tree.

### Run telemetry

The stamp also records what the run actually cost and how far to trust the
LLM's edge table. Directly under the refresh directive, `GRAPH.md` carries one
line like:

> **Run telemetry.** pass 2 prompt: 41,920 tokens (graphlm estimated 47,300); output: 9,812 tokens. LLM import edges vs parser ground truth: precision 0.93, recall 0.81 (n=15 LLM / 16 AST, 14 matched).

- **Usage** is the endpoint's own token count (requested via
  `stream_options.include_usage`), shown beside graphlm's estimate for the same
  prompt so you can see how the built-in estimator tracks *your* model. An
  endpoint that reports no usage shows "not reported by endpoint".
- **Faithfulness** scores the model's `import_edges` against the parser's
  deterministic edges: precision is the share of the model's comparable import
  edges the parser confirms, recall the share of the parser's edges the model
  reproduced. Comparable means kinds the parser emits (`import` / `from` /
  `require` / `static` / `include`) between files whose extensions the parser
  actually produced edges for on this run (`.py` always; `.js`/`.ts`/… when
  `graphlm[js]` ran, and so on). Low precision means invented dependencies.
  Absent under `--no-ast` (no ground truth) and on `--dry-run` (no LLM edges).

Both live in `GRAPH.json` under `meta.usage` / `meta.faithfulness` as additive,
optional fields — older graphs without them still diff normally — and the CLI
echoes them as `Usage:` / `Faithfulness:` lines.

**Adoption — one line for an `AGENTS.md` / rules file** (or just run `graphlm --install-skill claude` / `--install-skill codex`, below):

> A codebase map lives at `.graphlm/GRAPH.md` — read it before exploring the
> code, and follow its refresh directive (regenerate with `graphlm .` when the
> stamped commit differs from the current `HEAD`, or when the map is missing).

## Graph diff

Once the map is self-stamped and regenerated as the code moves, the natural next
question is "what changed in the *map* since last time?" Every real run answers
it by also writing **`GRAPH_DIFF.md`** and **`GRAPH_DIFF.json`** — a
graph-vs-graph diff, not a code diff (git already does code diffs better).

- **What it reports.** Per dimension — modules, import edges (LLM and AST),
  import cycles, data flows, entry points, file summaries — the entities
  **added** and **removed** since the prior `GRAPH.json`. So a new entry point, a
  dropped module, or a newly broken/resolved import cycle is visible at a glance
  without re-reading the whole graph.
- **Added/removed only.** Identity is *structural* (a module's `path`, an edge's
  `(from, to, kind)`, a cycle's node set), so a pure prose rewrite — a
  description or summary the LLM regenerates every run — is intentionally
  invisible. It would otherwise drown the structural signal in nondeterministic
  churn. Renames show as remove + add (no rename-matching).
- **Three baseline states, never conflated.** *First run* ("initial graph — no
  prior version to compare"), *uncomparable* (the prior file is corrupt or an
  unrecognized `schema_version` — this is **not** silently treated as a first
  run), and *normal*. An agent can always tell "nothing changed" from "never
  compared."
- **Commit range.** The diff header shows the old→new `commit_sha` range (a
  `null` side — non-git or an old graph — reads as `unknown`).
- **`--no-ast` safety.** Toggling AST parsing off between runs reports the AST
  edge dimension as "not compared" rather than fabricating a mass deletion.
- **Reads graphlm's own prior output.** This is *why* the `meta` block is a
  versioned input contract (above): a future format change is detected, not
  misparsed.

On by default. Pass `--no-diff` (or `include_diff=False`) to skip it. `--dry-run`
writes no diff — it makes no LLM call and produces no authoritative graph. The
diff is pure local computation over the two graphs: no extra network or LLM call.
A re-run's fresh-run cleanup removes graphlm's *own* stale artifacts, so opting a
format out (`--no-diff`, `--no-html`, or dropping `--json`) now also removes a
`GRAPH_DIFF.*` / `.html` / `GRAPH.json` a previous run left — no stale artifact
lingers. (Only graphlm's own exact filenames are touched; user files are never
removed.)

**Committing vs. gitignoring the graph.** The refresh check is `stamped_sha !=
HEAD`, so **if you commit `GRAPH.*`, the stamp is invalidated by the very commit
that ships it** — `HEAD` moves to that commit, so the map immediately reads as
one commit stale, and stays perpetually one commit behind. Two sane options:

- **Gitignore `.graphlm/`** (this repo's own choice) and regenerate on demand.
  The stamp then always reflects a real, current SHA. (One line —
  `echo '.graphlm/' >> .gitignore` — covers the whole output folder.)
- **Commit it and regenerate as the final step of the same commit** so the map
  ships fresh — but expect it to show one-commit staleness until the next regen,
  and treat that as normal.

Committing a graph that goes stale on every push (with no regeneration step) is
the one workflow to avoid — it reintroduces exactly the per-session refresh tax
this design set out to remove.

**Note on `-o`:** the default (`.graphlm/` inside the scanned project) keeps the
map in the repo it describes, so the staleness check works. If you redirect
output elsewhere (`-o <elsewhere>`), an agent reading that `GRAPH.md` and running
`git rev-parse HEAD` in its own directory will compare against the wrong repo —
keep the graph in the project it describes for the staleness check to work.

## Configuration

graphLM reads its LLM settings from environment variables. Copy `.env.example` to `~/.config/graphlm/.env` and fill in **your** endpoint, key, and model — those three have no built-in defaults:

```bash
mkdir -p ~/.config/graphlm
cp .env.example ~/.config/graphlm/.env
```

Settings are resolved in this order (first non-empty wins):

1. **Exported shell environment** — `export GRAPHLM_BASE_URL=…` etc.
2. **User-level `.env`** at `~/.config/graphlm/.env` (or `$XDG_CONFIG_HOME/graphlm/.env`).
3. Built-in defaults for the numeric budgets and timeout only.

A `.env` in the working directory or in the project being scanned is **not** read. graphLM's LLM config is yours, not the target repo's.

| Variable | Description | Default |
|---|---|---|
| `GRAPHLM_BASE_URL` | OpenAI-compatible API endpoint | *(required)* |
| `GRAPHLM_API_KEY` | API key for authentication | *(required)* |
| `GRAPHLM_MODEL` | Model name the endpoint serves | *(required)* |
| `GRAPHLM_MAX_CONTEXT` | Pass-2 **input** token budget (tree + files) | `120000` |
| `GRAPHLM_MAX_OUTPUT_TOKENS` | Per-pass **output** token ceiling; applies to both LLM calls and is independent of the input budget | `128000` |
| `GRAPHLM_TIMEOUT` | LLM request timeout in seconds (pass 2 is streamed) | `300` |

CLI flags (`-b`, `-k`, `-m`, `--max-context`, `--max-output-tokens`, `--timeout`) and the matching `generate_graph(...)` arguments override the env var.

### Project ignore file

Drop a **`.graphlmignore`** at the project root to record patterns that should stay out of every scan — a big sibling worktree, a game-engine cache, generated files — without passing `--exclude` on every run.

```
# one glob per line; # comments and blanks are skipped
.worktrees/
.godot/
*.generated.py
```

Patterns are merged with the built-in exclude set and any `--exclude` flags (union). Matching is the same as `--exclude`: the full relative path **or** any path component. A trailing slash is stripped, so `.godot/` matches a directory named `.godot`. The file itself is never sent to the model (same as `.gitignore`). Missing file → no change. `--no-graphlmignore` opts out.

## Options

| Flag | Description | Default |
|---|---|---|
| `-o, --output-dir` | Output directory for `GRAPH.md`, `GRAPH.html` (and `GRAPH.json` with `--json`) | `<project>/.graphlm/` |
| `-b, --base-url` | LLM API base URL | `GRAPHLM_BASE_URL` env var |
| `-k, --api-key` | LLM API key | `GRAPHLM_API_KEY` env var |
| `-m, --model` | Model name | `GRAPHLM_MODEL` env var |
| `--max-files` | Maximum files to scan initially | 200 |
| `--max-file-chars` | Maximum characters per file (a longer Python file is sent as its signature skeleton) | 4000 |
| `--no-skeleton` | Send the head of an oversized file instead of its Tree-sitter signature skeleton | Skeletons on |
| `--max-pass2-files` | Max files in pass 2 context | 80 |
| `--max-context` | Token budget for pass-2 context | `GRAPHLM_MAX_CONTEXT` env var, else 120000 |
| `--max-output-tokens` | Per-pass output-token ceiling for both LLM calls (independent of input) | `GRAPHLM_MAX_OUTPUT_TOKENS` env var, else 128000 |
| `--timeout` | LLM request timeout in seconds | `GRAPHLM_TIMEOUT` env var, else 300 |
| `--no-tests` | Exclude test files | Tests included by default |
| `--exclude` | Exclude pattern (repeatable) | — |
| `--no-graphlmignore` | Do not read `.graphlmignore` from the project root | File is read |
| `--no-redact` | Skip secret redaction | Redaction on |
| `--dry-run` | Show stats without calling LLM | Disabled |
| `--no-ast` | Skip Tree-sitter AST import edges | AST on |
| `--json` | Also write the machine-readable `GRAPH.json` / `GRAPH_DIFF.json` | JSON off (`.md` + `.html` only) |
| `--no-html` | Do not write `GRAPH.html` | HTML on |
| `--no-diff` | Do not write `GRAPH_DIFF.*` | Diff on |
| `--no-show-cycles` | Skip the cycle section | Cycles on |
| `--cycle-threshold` | Minimum cycle risk score | 0.0 |
| `--upgrade` | Upgrade this graphlm install to the latest PyPI release (keeps extras) | — |
| `--serve` | Serve the existing map to a coding agent over MCP (stdio); needs `graphlm[mcp]` | — |
| `--install-skill <harness>` | Install an agent guide (`claude` / `codex`) and exit | — |
| `--skill-local` | With `--install-skill`: write into the project, not user-global | User-global |
| `--skill-force` | With `--install-skill`: overwrite an existing guide | Skip if exists |
| `-V, --version` | Print the version and exit | — |

## Project structure

```
graphlm/
├── __init__.py           # Library API — generate_graph()
├── _html_template.html   # D3 visualization template
├── cli.py                # CLI entry point — Typer
├── config.py             # Settings from environment variables
├── context.py            # Two-pass prompt assembly
├── cycles.py             # Import cycle detection (Tarjan + SLOC risk)
├── diff.py               # Graph-vs-graph diff (GRAPH_DIFF.*)
├── faithfulness.py       # LLM-vs-parser edge score in the stamp
├── html_render.py        # Interactive D3 HTML visualization
├── llm.py                # LLM client with retry and JSON recovery
├── mcp_server.py         # --serve: thin MCP wrapper over query.py
├── mermaid.py            # Directory-level Mermaid flowchart for GRAPH.md
├── models.py             # Pydantic v2 data models
├── parser.py             # Thin shim re-exporting parsers.base
├── parsers/              # Tree-sitter registry + per-language resolvers
│   ├── base.py
│   ├── python.py         # core (always on)
│   ├── javascript.py     # graphlm[js]
│   ├── java.py           # graphlm[java]
│   ├── rust.py           # graphlm[rust]
│   ├── csharp.py         # graphlm[csharp]
│   ├── cpp.py            # graphlm[cpp]
│   ├── go.py             # graphlm[go]
│   └── php.py            # graphlm[php]
├── prompts.py            # System prompt (injection guard)
├── provenance.py         # Git SHA / timestamp / version capture for the stamp
├── query.py              # Map-query helpers used by --serve
├── redact.py             # Sensitive-file skip + secret redaction
├── render.py             # Markdown + JSON + HTML output rendering
├── scanner.py            # Project directory scanner
├── skills.py             # --install-skill: agent-guide installer
└── upgrade.py            # --upgrade: uv tool / pipx / pip detector
tests/
├── conftest.py
├── test_*.py
└── fixtures/             # small, medium, large, cyclic, skeleton, ts, java, rust, csharp, cpp, go, php, ignore
```

## Requirements

- Python 3.11, 3.12, or 3.13
- An OpenAI-compatible LLM endpoint (base URL + API key + model name)
- [uv](https://github.com/astral-sh/uv) — recommended for installing (`uv tool install`) and required for development

## Contributing

Contributions are welcome — bug reports, fixes, docs, and new language support especially. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the test/mypy commands, and the security invariants to preserve. Please also read the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? **Please don't open a public issue.** Report it privately via GitHub's [Report a vulnerability](https://github.com/ggrace519/graphLM/security/advisories/new) button — see [SECURITY.md](SECURITY.md) for scope and details. graphLM reads code it didn't write, so its sensitive-file, redaction, symlink, and prompt-injection guards are the surface that matters most.

## License

GPLv3 — see [LICENSE](LICENSE).
