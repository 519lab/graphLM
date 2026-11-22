<!--
Thanks for contributing to graphLM! Please fill this in. See CONTRIBUTING.md for
what "done" looks like here.
-->

## Summary

<!-- What does this change do, and why? One or two sentences. -->

## What changed

<!-- The notable changes. Reference an issue with "Closes #NN" when relevant. -->

-

## Verification

<!-- "Done" here means you ran the checks and saw green. Paste the results. -->

- [ ] `uv run pytest -q` passes (paste the count, e.g. `392 passed`)
- [ ] `uv run mypy graphlm --ignore-missing-imports` is clean
- [ ] Coverage held at or above its previous level (if you touched logic)

```
<!-- paste test / mypy output here -->
```

## Checklist

- [ ] Branched off `develop`; one coherent change per PR
- [ ] Tests added or updated for the change
- [ ] CHANGELOG updated under `## [Unreleased]` (if externally observable)
- [ ] Docs updated in lockstep (README / `CLAUDE.md` / prompt text) if a flag,
      output field, or default changed
- [ ] If a data-model field changed: pass-2 prompt (`context.py`) **and**
      `render.py` updated too
- [ ] I did **not** weaken a security invariant (sensitive-file skip, redaction,
      symlink guard, prompt-injection clause) — or I called it out explicitly below

## Notes for reviewers

<!-- Anything to flag: trade-offs, a security invariant touched, follow-ups. -->
