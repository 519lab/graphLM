# Run telemetry — real token usage + LLM-vs-AST faithfulness in the stamp

Innovation #6. Branch `innovation/run-telemetry`.

## What it does

Every real run now stamps two more things into `meta` (the provenance block
in `GRAPH.json`), summarises them in one line at the top of `GRAPH.md`, and
echoes them on the CLI:

1. **Usage** — the endpoint's *real* `prompt_tokens` / `completion_tokens`
   per pass (via `stream_options.include_usage` on the streamed request),
   stored beside graphlm's own `estimated_prompt_tokens` for the same prompt.
   The real-vs-estimated ratio is what calibrates the `estimate_tokens`
   heuristic (`* 2 // 5`, #17) — now it is auditable from the artifact
   instead of measured by hand.
2. **Faithfulness** — precision/recall of the LLM's `import_edges` against
   the parser's `deterministic_edges` (the "do not contradict" ground truth
   injected into the pass-2 prompt). Low precision = the model invented
   dependencies; low recall = it dropped ones it was told about.

Also fixed on the way: `--dry-run` printed `0 import edges` on every run
(the LLM's field, never filled on a dry run). It now prints the AST count.

## How to run it

```bash
# Dry run — no LLM. Shows the corrected AST edge count; no telemetry
# (nothing measured), and none is stamped.
uv run graphlm tests/fixtures/cyclic_project --dry-run
#   ...
#   AST import edges: 4
#   Graph sections: tree, 0 modules, ...

# Real run against the configured endpoint (.env / GRAPHLM_* env vars).
uv run graphlm . 
#   ...
#   Modules: 18 | Import edges: 31 | ...
#   Usage: pass 2 prompt: 41920 tokens (graphlm estimated 47300); output: 9812 tokens
#   Faithfulness: LLM import edges vs parser ground truth: precision 0.93, recall 0.81 (n=15 LLM / 16 AST, 14 matched)
#   Done.

# The same facts in the artifacts:
head -5 .graphlm/GRAPH.md          # the "> **Run telemetry.**" line under the directive
python -c "import json; print(json.load(open('.graphlm/GRAPH.json'))['meta'])"
```

`meta` shape (additive, `schema_version` still 1):

```json
"meta": {
  "schema_version": 1,
  "created_at": "...", "commit_sha": "...", "graphlm_version": "...",
  "usage": {
    "pass1": {"prompt_tokens": 1180, "completion_tokens": 62, "estimated_prompt_tokens": 1390},
    "pass2": {"prompt_tokens": 41920, "completion_tokens": 9812, "estimated_prompt_tokens": 47300}
  },
  "faithfulness": {"precision": 0.93, "recall": 0.81, "llm_edges": 15, "ast_edges": 16, "matched": 14}
}
```

## What works

- `llm.py`: `stream_options.include_usage` sent on every request; the final
  empty-`choices` usage chunk is read *before* the empty-choices skip (it
  was silently dropped otherwise); plain JSON bodies read top-level `usage`.
  `_read_streamed_completion` returns a typed `StreamResult`; `call_llm`
  exposes usage via a keyword-only `on_usage` callback so its return type
  and every existing caller are untouched. Missing / malformed usage never
  raises.
- `models.py`: `PassUsage`, `RunUsage`, `Faithfulness`; `GraphMeta.usage` /
  `.faithfulness` — additive optionals, **no `GRAPH_META_SCHEMA_VERSION`
  bump** (ADR-001 consequence added). Old `GRAPH.json` files still load as a
  `NORMAL` diff baseline (tested).
- `faithfulness.py`: pure `score(llm_edges, ast_edges)`. Compares
  `(from, to)`, ignores `kind`, restricts the LLM side to `.py`↔`.py` edges
  of kind `import`/`from`, normalises `\` and `./`. `None` when AST is off.
- `render.py`: one `> **Run telemetry.**` blockquote under the directive;
  `usage_summary` / `faithfulness_summary` are shared with the CLI so the
  wording is single-sourced. Each half omitted when unmeasured.
- `cli.py`: `Usage:` / `Faithfulness:` lines after the stats line; the
  dry-run fix.
- Tests: 450 passing (was 404); new code 100% covered
  (`tests/test_faithfulness.py`, `TestUsageCapture` in `test_llm.py`,
  `TestRunTelemetry` in `test_integration.py`, `TestRunTelemetryLine` in
  `test_render.py`, dry-run/telemetry cases in `test_cli.py`, baseline
  round-trip in `test_diff.py`). mypy clean.

## What's stubbed / limits

- **Not measured against the live endpoint in this session** — the usage
  chunk shape is the OpenAI `stream_options` contract, exercised with mocked
  SSE bodies. Whether the Qwen endpoint used for these runs
  honours `stream_options` is unverified; if it ignores the option the
  stamp shows `"prompt_tokens": null` and the line reads "not reported by
  endpoint" — the designed degradation, not a failure.
- Faithfulness is Python-only by construction (the AST covers only Python
  today). A correct LLM edge between two `.ts` files is excluded, not
  penalised. When the JS/TS pack (#42, Phase 1) lands, widen
  `_comparable()` to the extensions the registry actually resolves.
- Pass-1 usage is stamped in JSON but not shown in the prose line (tree-only
  prompt; rarely interesting).
- The `n=` clause counts *distinct* `(from, to)` pairs after de-duplication,
  so it can be lower than `len(import_edges)`.

## Next increment

1. Run it once against the real endpoint and record the measured
   real/estimated ratio in `docs/` (and the `qwen-endpoint-context-window`
   memory) — that's the number #17 guessed at.
2. Trend line: `GRAPH_DIFF.*` could carry old→new faithfulness and usage so a
   regression in the model's edge accuracy (or a prompt that suddenly costs
   30% more) shows up in the diff, not just the stamp.
3. Optional `--min-faithfulness <p>` warning: print a loud stderr note when
   precision drops below a threshold, so a bad model run is flagged at
   generation time rather than discovered by the next reader.
