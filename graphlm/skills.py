"""Install an agent guide that teaches a coding harness to use graphlm.

``graphlm --install-skill <harness>`` drops a small guide telling a coding agent
(Claude Code, Codex, …) two things: *how to run graphlm*, and *to read
``.graphlm/GRAPH.md`` when it starts working in a codebase* (regenerating the map
with ``graphlm .`` when it is missing or stale).

Design constraints (see ADR-003):

- **Never edit a file graphlm didn't create.** We only ever *create* graphlm's
  own files. For a harness whose config is a user-owned instruction file
  (Codex's ``AGENTS.md``), we write a standalone guide and *print* a one-line
  snippet for the user to include themselves — we don't append to their file.
- **Idempotent.** Skip if the target already exists, unless ``force=True``.
- **User-global by default**, project-local on request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Supported harness identifiers (for the CLI's error message and validation).
SUPPORTED_HARNESSES = ("claude", "codex")


@dataclass
class SkillInstallResult:
    """Outcome of an ``--install-skill`` run."""

    harness: str
    path: Path
    #: True when the file already existed and was left untouched (no --force).
    skipped: bool
    #: A message to print for the user (e.g. how to wire a Codex guide into
    #: their own AGENTS.md). ``None`` when there's nothing extra to say.
    note: str | None = None


def _skill_markdown_body() -> str:
    """The guide's Markdown body — shared by every harness.

    Distinguishes two invocation modes, which want opposite defaults when no map
    exists: an *explicit* invocation generates one (`graphlm .`) without asking;
    a *self-reached* invocation mid-task does not (generating streams for a minute
    or two and would stall the user's real task). This is what keeps a
    user-global install from either freezing on explicit use or misfiring on
    auto-use in an unmapped repo.
    """
    return """\
## Use graphlm's codebase map

[graphlm](https://github.com/519lab/graphLM) generates a **map of a
codebase** — modules, import/dependency edges, data flow, entry points, import
cycles, and "where do I find X?" answers. **Read `.graphlm/GRAPH.md`** — the
Markdown map is what you want as an agent (it opens with a compact orientation
block and costs far fewer tokens than the JSON's node/link arrays). A clickable
`.graphlm/GRAPH.html` is also written. The machine-readable `.graphlm/GRAPH.json`
is **opt-in** — run `graphlm . --json` if you need it — but prefer `GRAPH.md` or
the MCP tools (see `--serve` below) over parsing JSON.

**First, know which situation you're in — the right action differs.**

### A. You were invoked *explicitly* to work with the map

(The user ran the graphlm command/skill directly, with no other task — they want
the map.)

- **If `.graphlm/GRAPH.md` exists:** read it, then briefly tell the user what the
  codebase looks like (its main modules, entry points, any notable import
  cycles). Check the refresh directive (below) and offer to regenerate if stale.
- **If it does NOT exist:** **generate it — run `graphlm .` — without asking.**
  This is the expected outcome of an explicit invocation, and it's a local,
  idempotent action that only writes into `.graphlm/`. Do **not** stall with a
  menu. Then read the map you just generated and summarize it. Generation streams
  and can take a minute or two on a large repo — that's normal; let it run.
  - **One caveat worth a heads-up, not a stall:** generating a map **sends
    selected repository file content** (typically tens of thousands of tokens)
    to whatever endpoint graphlm is configured for (`GRAPHLM_BASE_URL`). If that
    endpoint is **local or otherwise trusted**, just proceed. If it's a
    **third-party API** — or you can't tell — say in one line where the code will
    be sent and get a quick OK before running, since exporting a private codebase
    is the user's call to make. Don't send code from a repo the user wouldn't
    want shared with that endpoint.
- Only stop to ask if graphlm is **not installed** or its **LLM endpoint isn't
  configured/reachable** (`graphlm .` will say so) — then tell the user what's
  missing instead of guessing.

### B. You reached for this yourself while doing *another* task

(The user asked for a feature/bug/exploration; you're orienting.)

- **If `.graphlm/GRAPH.md` exists:** read it first — it orients you faster than
  opening files one by one. Follow the refresh directive; a quick
  `git rev-parse HEAD` mismatch means it may be stale.
- **If it does NOT exist:** do **not** generate one mid-task — a fresh generation
  can take minutes and would stall the work the user actually asked for. Just
  note in one line that no map exists (and that `graphlm .` would create one),
  then proceed with the real task normally. Suggest generating it only if the
  user seems to be starting a longer effort in an unfamiliar codebase.

### The refresh directive

`.graphlm/GRAPH.md` opens with a provenance stamp naming the git commit it was
generated against. Compare it to the repo's current `git rev-parse HEAD`; if they
differ, the map may be stale — regenerate with `graphlm .` from the project root.
`.graphlm/GRAPH_DIFF.md` lists what changed (modules, edges, cycles added/removed)
since the previous run.

### If the `graphlm` MCP server is registered, prefer its tools

`graphlm --serve` exposes the same map over MCP (tools: `overview`, `find`,
`module`, `neighbors`, `dependents`, `cycles`, `entry_points`, `staleness`).
When those tools are available, call `overview` first and then ask targeted
questions (`neighbors` for "who imports X?", `dependents` with
`transitive=true` for blast radius, `find` for "where is X?") instead of
reading the whole `GRAPH.md` — each answer costs a few hundred tokens rather
than the entire document. `staleness` replaces the manual `git rev-parse HEAD`
check. Register it once per repo with
`claude mcp add graphlm -- graphlm --serve /path/to/repo` (needs
`graphlm[mcp]`).

### Notes

- The map is **advisory** and best-effort. Trust the code over the map when they
  disagree; a stale map is possible (regenerate to be sure).
- **Generating a map sends code off-box.** `graphlm .` transmits selected
  repository file content to the configured LLM endpoint (`GRAPHLM_BASE_URL`).
  That's fine for a local/trusted endpoint; for a third-party API, treat it as a
  code-export the user should be aware of before you run it (see the caveat under
  A above). graphlm redacts obvious secrets and skips sensitive files, but that's
  defense-in-depth, not a guarantee.
- graphlm needs an OpenAI-compatible LLM endpoint (`GRAPHLM_BASE_URL` /
  `GRAPHLM_API_KEY` / `GRAPHLM_MODEL`); `graphlm .` will tell you if it's not
  configured. `graphlm --help` lists every flag.
"""


def _claude_skill_file(body: str) -> str:
    """A Claude Code skill: frontmatter (name/description) + the shared body."""
    frontmatter = (
        "---\n"
        "name: graphlm\n"
        "description: >-\n"
        "  Read the graphlm codebase map (.graphlm/GRAPH.md) to get oriented in a "
        "repository. Use this at the START of working in a codebase — before reading "
        "or searching through files to understand the structure, and before answering "
        "questions about how the project is laid out. It gives you modules, "
        "import/dependency edges, entry points, and import cycles at a glance, so you "
        "don't have to explore file-by-file. Reach for it whenever you're getting your "
        "bearings in an unfamiliar or large repo. If the map is missing or stale, "
        "regenerate it with `graphlm .`.\n"
        "---\n\n"
        "# graphlm codebase map\n\n"
    )
    return frontmatter + body


def _codex_guide_file(body: str) -> str:
    """A standalone Codex guide (plain Markdown, no frontmatter)."""
    return "# graphlm codebase map\n\n" + body


def _base_dir(project_dir: Path, local: bool, home: Path) -> Path:
    """Root under which the harness config dir lives."""
    return project_dir if local else home


def _claude_target_path(base: Path) -> Path:
    # Claude Code skills live under <base>/.claude/skills/<name>/SKILL.md, for
    # both the global (~) and project-local conventions.
    return base / ".claude" / "skills" / "graphlm" / "SKILL.md"


def _codex_target_path(base: Path, local: bool) -> Path:
    if local:
        return base / "graphlm-agent.md"
    return base / ".codex" / "graphlm.md"


def _codex_note(path: Path, local: bool) -> str:
    """How to wire the standalone Codex guide into the user's AGENTS.md.

    Codex reads AGENTS.md files (root-down, one per directory) and has no
    arbitrary-file include directive, so we tell the user to paste a pointer
    into their own AGENTS.md rather than editing it for them.
    """
    if local:
        agents = "AGENTS.md (in this project)"
    else:
        agents = "~/.codex/AGENTS.md (global)"
    return (
        f"Wrote the Codex guide to {path}.\n"
        f"Codex won't read it automatically. To activate it, add a pointer to "
        f"your own {agents}, e.g.:\n\n"
        f"    ## graphlm\n"
        f"    See {path} — read `.graphlm/GRAPH.md` when working in a repo and\n"
        f"    regenerate it with `graphlm .` when missing or stale.\n\n"
        f"(graphlm won't edit your AGENTS.md for you.)"
    )


def install_skill(
    harness: str,
    *,
    project_dir: Path | None = None,
    local: bool = False,
    force: bool = False,
    home: Path | None = None,
) -> SkillInstallResult:
    """Install the graphlm agent guide for ``harness``.

    Args:
        harness: ``"claude"`` or ``"codex"``.
        project_dir: the scanned project; required when ``local=True``.
        local: write into the project instead of the user-global config dir.
        force: overwrite an existing guide instead of skipping it.
        home: base home dir (injectable for tests); defaults to ``Path.home()``.

    Returns a :class:`SkillInstallResult`. A pre-existing *regular* file is
    reported as ``skipped=True`` (not raised). Raises ``ValueError`` for an
    unknown harness, for ``local=True`` without a ``project_dir``, and when the
    target path is a symlink (graphlm won't write through it — #33).
    """
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(
            f"unknown harness {harness!r}; supported: {', '.join(SUPPORTED_HARNESSES)}"
        )
    if local and project_dir is None:
        raise ValueError("local install requires a project_dir")

    home = home if home is not None else Path.home()
    base = _base_dir(project_dir or Path.cwd(), local, home)
    body = _skill_markdown_body()

    if harness == "claude":
        path = _claude_target_path(base)
        content = _claude_skill_file(body)
        note = None
    else:  # codex
        path = _codex_target_path(base, local)
        content = _codex_guide_file(body)
        note = _codex_note(path, local)

    # Refuse to write *through* a symlink at the target — the write would land on
    # whatever the link resolves to, clobbering a file graphlm didn't create
    # (e.g. a dotfiles-managed ~/.claude/skills/), violating this module's
    # contract. `is_symlink()` catches broken links too (which `.exists()` would
    # report as absent and let the no-force path fall through to a write). #33
    if path.is_symlink():
        raise ValueError(
            f"refusing to write through a symlink at {path} — graphlm won't "
            f"overwrite a file it didn't create. Remove the symlink and re-run."
        )

    if path.exists() and not force:
        return SkillInstallResult(harness=harness, path=path, skipped=True, note=note)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return SkillInstallResult(harness=harness, path=path, skipped=False, note=note)
