"""Registry-driven Tree-sitter backend and language-agnostic dispatch.

This module owns the shared parsing machinery: the single ``_TreeSitterBackend``
instance, the ``ParsedFile`` / ``_ParsedImport`` data carriers, the grammar
registry (``_GRAMMARS``), the resolver registry (``_RESOLVERS``), the
group-by-language dispatch in ``build_dependency_graph`` / ``parse_file``, and
the cycle detector. Language-specific extraction/resolution lives in per-language
modules (``graphlm.parsers.python``, ``graphlm.parsers.javascript``,
``graphlm.parsers.java``, ``graphlm.parsers.rust``), which register themselves
through the resolver registry (see ``_ensure_resolvers``). Python is the only
core language (grammar in the base install). Other languages ship as extras
(``graphlm[js]`` / ``graphlm[java]`` / ``graphlm[rust]`` / ``graphlm[csharp]``): resolvers are always
registered, grammar wheels are optional, and a missing extra degrades to zero
edges for that language.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from graphlm.models import ImportEdge
from graphlm.scanner import FileFragment

logger = logging.getLogger(__name__)

# Supported languages
PYTHON = "python"
JAVASCRIPT = "javascript"
TYPESCRIPT = "typescript"
JAVA = "java"
RUST = "rust"
CSHARP = "csharp"
C = "c"
CPP = "cpp"
GO = "go"
PHP = "php"

SUPPORTED_LANGUAGES = {
    PYTHON, JAVASCRIPT, TYPESCRIPT, JAVA, RUST, CSHARP, C, CPP, GO, PHP,
}

# Mapping from file extension to language name
EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": PYTHON,
    ".js": JAVASCRIPT,
    ".ts": TYPESCRIPT,
    ".jsx": JAVASCRIPT,
    ".tsx": TYPESCRIPT,
    ".java": JAVA,
    ".rs": RUST,
    ".cs": CSHARP,
    ".c": C,
    ".h": C,
    ".cpp": CPP,
    ".cc": CPP,
    ".cxx": CPP,
    ".hpp": CPP,
    ".hh": CPP,
    ".hxx": CPP,
    ".go": GO,
    ".php": PHP,
}


@dataclass(frozen=True, slots=True, eq=True)
class ParsedFile:
    """AST-derived information from a single source file."""

    imports: list[ImportEdge] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    call_sites: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ParsedImport:
    """Unresolved import statement extracted from a Python AST."""

    module: str
    names: tuple[str, ...]
    level: int
    kind: str


# --- Grammar registry ------------------------------------------------------


@dataclass(frozen=True)
class _GrammarSpec:
    """How to load a Tree-sitter grammar: its pip module and language accessor."""

    pip_module: str  # e.g. "tree_sitter_python"
    accessor: str  # e.g. "language"


# Grammar selection is (language, suffix) -> spec. ``detect_language`` maps
# both ``.ts`` and ``.tsx`` to ``"typescript"``, so a registry keyed by
# language name alone cannot pick ``language_tsx()`` vs ``language_typescript()``.
# A callable entry receives the file suffix; a plain spec ignores it.
# Cache key is ``(language, accessor)`` so TS and TSX stay distinct.
_GrammarEntry = _GrammarSpec | Callable[[str], _GrammarSpec]

_GRAMMARS: dict[str, _GrammarEntry] = {
    "python": _GrammarSpec("tree_sitter_python", "language"),
    "javascript": _GrammarSpec("tree_sitter_javascript", "language"),
    "typescript": lambda suffix: _GrammarSpec(
        "tree_sitter_typescript",
        "language_tsx" if suffix.lower() == ".tsx" else "language_typescript",
    ),
    "java": _GrammarSpec("tree_sitter_java", "language"),
    "rust": _GrammarSpec("tree_sitter_rust", "language"),
    "csharp": _GrammarSpec("tree_sitter_c_sharp", "language"),
    "c": _GrammarSpec("tree_sitter_c", "language"),
    "cpp": _GrammarSpec("tree_sitter_cpp", "language"),
    "go": _GrammarSpec("tree_sitter_go", "language"),
    "php": _GrammarSpec("tree_sitter_php", "language_php"),
}


def _spec_for(language: str, suffix: str = "") -> _GrammarSpec:
    """Resolve the grammar spec for a language, using suffix when the entry is suffix-sensitive."""
    entry = _GRAMMARS.get(language)
    if entry is None:
        raise ValueError(f"Unsupported language: {language}")
    return entry(suffix) if callable(entry) else entry


class _GrammarUnavailable(Exception):
    """A grammar's pip module is not importable (opt-in pack not installed).

    Caught by ``parse_file`` / ``build_dependency_graph``, which degrade to zero
    edges for that language rather than poisoning the whole run. NOT a subclass
    of ``ValueError`` (an unregistered language) — the two are handled
    differently by the dispatch.
    """


class _TreeSitterBackend:
    """Thin wrapper around tree-sitter parsing. Lazy-imports on first use."""

    _language_cache: dict[tuple[str, str], object] = {}
    _ts: object | None = None

    def _import_ts(self):
        if self._ts is None:
            import tree_sitter as ts

            self._ts = ts
        return self._ts

    def _get_language(self, language: str, suffix: str = ""):
        spec = _spec_for(language, suffix)
        cache_key = (language, spec.accessor)
        if cache_key in self._language_cache:
            return self._language_cache[cache_key]

        try:
            mod = importlib.import_module(spec.pip_module)
        except ImportError:
            raise _GrammarUnavailable(language)
        ts = self._import_ts()
        lang = ts.Language(getattr(mod, spec.accessor)())
        self._language_cache[cache_key] = lang
        return lang

    def parse_source(self, code: bytes, language: str, suffix: str = ""):
        ts = self._import_ts()
        lang = self._get_language(language, suffix)
        parser = ts.Parser(lang)
        return parser.parse(code)

    def build_query(self, language: str, query_str: str, suffix: str = ""):
        ts = self._import_ts()
        lang = self._get_language(language, suffix)
        return ts.Query(lang, query_str)

    def run_query(self, tree, query):
        ts = self._import_ts()
        cursor = ts.QueryCursor(query)
        return cursor.matches(tree.root_node)


_backend = _TreeSitterBackend()

# Grammars already warned about as unavailable, so the "once per language"
# degradation warning is not repeated per file. Module-level so the dedupe holds
# across every call.
_WARNED_GRAMMARS: set[str] = set()


def _warn_grammar_unavailable(language: str) -> None:
    if language not in _WARNED_GRAMMARS:
        _WARNED_GRAMMARS.add(language)
        logger.warning(
            "Tree-sitter grammar for %s is not installed; contributing zero "
            "edges for that language. Install the matching language pack to "
            "enable it.",
            language,
        )


def detect_language(path: Path) -> str | None:
    """Auto-detect language from file extension.

    Args:
        path: File path to inspect.

    Returns:
        Language name (e.g. 'python') or None if unsupported.
    """
    suffix = path.suffix.lower()
    return EXT_TO_LANGUAGE.get(suffix)


def _module_to_path(module_str: str, language: str) -> str:
    """Convert a dotted module name to a naive file path (parse_file placeholder).

    Args:
        module_str: Dotted module name (e.g. 'app.routes.users').
        language: Language name.

    Returns:
        Path string (e.g. 'app/routes/users.py').
    """
    parts = [p for p in module_str.split(".") if p]
    if not parts:
        return ""
    if language == PYTHON:
        return "/".join(parts) + ".py"
    if language in (JAVASCRIPT, TYPESCRIPT):
        return "/".join(parts)
    if language == JAVA:
        return "/".join(parts) + ".java"
    if language == RUST:
        return "/".join(parts) + ".rs"
    return ""


def _node_text(node) -> str:
    return node.text.decode("utf-8")


def _field_nodes(node, field: str) -> list:
    return list(node.children_by_field_name(field))


def _posix_rel(path: str) -> str:
    return path.replace("\\", "/")


def _first_known(candidates: tuple[str, ...], known: set[str]) -> str | None:
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


def _first_known_rooted(
    candidates: tuple[str, ...], known: set[str], roots: tuple[str, ...]
) -> str | None:
    """Like _first_known, but tries each candidate under every source root.

    Root "" (project root) is tried first via the candidate order, so a
    root-layout project keeps its existing exact-match behavior.
    """
    for candidate in candidates:
        for root in roots:
            rooted = root + candidate
            if rooted in known:
                return rooted
    return None


# --- Resolver registry -----------------------------------------------------
#
# Each language module registers a resolver here. A resolver is the small,
# uniform surface base.py's dispatch drives without knowing the language:
#   - parse_file(code, path)   -> ParsedFile  (for parse_file())
#   - imports_from_source(code, path) -> list of language import carriers
#   - source_roots(known)      -> tuple[str, ...]  (source-root prefixes)
#   - resolve(imp, from_path, known, roots) -> list[str]  (resolved targets)
#   - edge_kind(imp)           -> str  (the ImportEdge.kind for a carrier)
#   - skeleton(code)           -> str  (optional: signature skeleton, see
#                                 skeleton_for; packs without one keep the
#                                 scanner's head-truncation for big files)


@dataclass(frozen=True)
class _Resolver:
    """The language-agnostic resolver surface base.py's dispatch calls."""

    parse_file: Callable[[bytes, Path], ParsedFile]
    imports_from_source: Callable[[bytes, Path], list]
    source_roots: Callable[[set[str]], tuple[str, ...]]
    resolve: Callable[..., list[str]]
    edge_kind: Callable[..., str]
    skeleton: Callable[[bytes], str] | None = None


_RESOLVERS: dict[str, _Resolver] = {}
_resolvers_loaded = False


def _register_resolver(language: str, resolver: _Resolver) -> None:
    _RESOLVERS[language] = resolver


def skeleton_for(path: Path, code: bytes) -> str | None:
    """Signature skeleton of ``code`` for the language of ``path``, or None.

    None means "no skeleton available — keep the head of the file": the
    extension maps to no language, the language has no resolver or its
    resolver has no skeleton renderer, or the grammar pack is not installed.
    Never raises: the scanner calls this on every oversized file, and a
    renderer bug on one odd file must degrade that file to head-truncation,
    not abort the scan.
    """
    _ensure_resolvers()
    language = detect_language(path)
    if language is None:
        return None
    resolver = _RESOLVERS.get(language)
    if resolver is None or resolver.skeleton is None:
        return None
    try:
        return resolver.skeleton(code)
    except _GrammarUnavailable:
        _warn_grammar_unavailable(language)
        return None
    except Exception as e:
        logger.debug("Skeleton failed for %s: %s", path, e)
        return None


def _ensure_resolvers() -> None:
    """Import language modules so they register their resolvers.

    Called at the top of both dispatchers. A function-local import breaks the
    base <-> python module cycle (python imports names from base at module
    scope, so base must not import python at module scope).
    """
    global _resolvers_loaded
    if _resolvers_loaded:
        return
    from graphlm.parsers import python as _python  # noqa: F401
    from graphlm.parsers import javascript as _javascript  # noqa: F401
    from graphlm.parsers import java as _java  # noqa: F401
    from graphlm.parsers import rust as _rust  # noqa: F401
    from graphlm.parsers import csharp as _csharp  # noqa: F401
    from graphlm.parsers import cpp as _cpp  # noqa: F401
    from graphlm.parsers import go as _go  # noqa: F401
    from graphlm.parsers import php as _php  # noqa: F401

    _resolvers_loaded = True


def _source_bytes(frag: FileFragment, project_dir: Path | None) -> bytes | None:
    if project_dir is not None:
        fpath = project_dir / frag.rel_path
        if fpath.is_file():
            try:
                return fpath.read_bytes()
            except (OSError, PermissionError) as e:
                logger.warning("Could not read %s: %s", fpath, e)
                return None
    if frag.content:
        return frag.content.encode("utf-8")
    return None


def parse_file(path: Path, language: str | None = None) -> ParsedFile | None:
    """Parse a single source file and extract AST-derived data.

    Args:
        path: Path to the source file.
        language: Language name (auto-detected from extension if None).

    Returns:
        ParsedFile with import edges, exports, functions, and call sites.
        Returns None for unsupported file types or parse errors.
    """
    _ensure_resolvers()

    if not path.exists() or not path.is_file():
        return None

    if language is None:
        language = detect_language(path)
        if language is None:
            return None

    if language not in SUPPORTED_LANGUAGES:
        return None

    try:
        code = path.read_bytes()
    except (OSError, PermissionError) as e:
        logger.warning("Could not read %s: %s", path, e)
        return None

    if not code.strip():
        return ParsedFile()

    resolver = _RESOLVERS.get(language)
    if resolver is None:
        # A supported language with no resolver yet (a future pack before it
        # registers): empty ParsedFile, matching the historic facade — not None.
        logger.debug(
            "Parsing %s with language %s not yet fully implemented",
            path,
            language,
        )
        return ParsedFile()

    try:
        return resolver.parse_file(code, path)
    except _GrammarUnavailable as e:
        _warn_grammar_unavailable(str(e) or language)
        return ParsedFile()
    except Exception as e:
        logger.warning("AST parse failed for %s: %s", path, e)
        return None


def _dedupe_edges(edges: list[ImportEdge]) -> list[ImportEdge]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[ImportEdge] = []
    for edge in edges:
        key = (edge.from_path, edge.to_path, edge.kind)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    return unique


def _cached_python_imports(
    frag: FileFragment, project_dir: Path | None, code: bytes
) -> list | None:
    """Import carriers from the skeleton parse of these same bytes, or None.

    The slot is cleared on every call so a fragment cannot reuse a parse
    against later bytes, and so the carriers do not outlive the graph build.
    A hit requires ``project_dir`` (the resolver's raw disk bytes, not the
    redacted fragment text), the same resolved path, and a matching hash.
    """
    cached = frag._import_cache
    frag._import_cache = None
    if cached is None or project_dir is None:
        return None
    resolved, digest, carriers = cached
    try:
        current = str((project_dir / frag.rel_path).resolve())
    except OSError:
        return None
    if current != resolved or hashlib.sha256(code).hexdigest() != digest:
        return None
    return carriers


def build_dependency_graph(
    fragments: list[FileFragment],
    max_files: int = 200,
    project_dir: Path | None = None,
    *,
    partial_languages: set[str] | None = None,
) -> list[ImportEdge]:
    """Build a deterministic dependency graph from file fragments.

    Resolves imports against files that exist in ``fragments``. Stdlib,
    third-party, and missing modules are dropped. Files whose language has no
    registered resolver (or whose grammar pack is not installed) contribute zero
    edges and never abort the run — a missing grammar degrades that one language,
    never the whole graph.

    Args:
        fragments: File fragments from a scan.
        max_files: Maximum number of files to parse as import sources.
        project_dir: Base directory for resolving relative file paths.
        partial_languages: Optional set the dispatcher fills with language
            names whose resolver dropped a specifier by policy (JS/TS bare
            packages, not a failed relative lookup). Callers pass this through
            to the pass-2 edge-table framing so a known-partial list is never
            presented as exhaustive ground truth.

    Returns:
        List of ImportEdge instances (never None when called).
    """
    _ensure_resolvers()

    known_files = {_posix_rel(frag.rel_path) for frag in fragments}
    # Source-root prefixes (e.g. "src/") so imports resolve in src-layout
    # projects, where scanned files live under src/ but are imported by their
    # package name (#19). Computed once per language; "" (project root) is always
    # included.
    roots_by_language: dict[str, tuple[str, ...]] = {}
    all_edges: list[ImportEdge] = []

    for frag in fragments[:max_files]:
        rel_path = _posix_rel(frag.rel_path)
        language = detect_language(Path(rel_path))
        if language is None:
            continue
        resolver = _RESOLVERS.get(language)
        if resolver is None:
            continue
        code = _source_bytes(frag, project_dir)
        if not code or not code.strip():
            continue
        if language not in roots_by_language:
            roots_by_language[language] = resolver.source_roots(known_files)
        roots = roots_by_language[language]
        try:
            cached = _cached_python_imports(frag, project_dir, code)
            imports = (
                cached
                if cached is not None
                else resolver.imports_from_source(code, Path(rel_path))
            )
            for imp in imports:
                targets = resolver.resolve(imp, rel_path, known_files, roots)
                if (
                    not targets
                    and partial_languages is not None
                    and not getattr(imp, "is_relative", True)
                ):
                    # Policy drop (bare/aliased specifier), not a missed file.
                    partial_languages.add(language)
                for to_path in targets:
                    all_edges.append(
                        ImportEdge(
                            from_path=rel_path,
                            to_path=to_path,
                            kind=resolver.edge_kind(imp),
                        )
                    )
        except _GrammarUnavailable as e:
            _warn_grammar_unavailable(str(e) or language)
            continue

    for frag in fragments:
        frag._import_cache = None
    return _dedupe_edges(all_edges)


def detect_import_cycles(edges: list[ImportEdge]) -> list[list[str]]:
    """Detect import cycles from dependency edges using Tarjan's algorithm.

    Args:
        edges: List of ImportEdge instances.

    Returns:
        List of cycles, each cycle being a list of file paths.
    """
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.from_path, set()).add(edge.to_path)

    index_counter = [0]
    stack: list[str] = []
    lowlinks: dict[str, int] = {}
    index: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True

        for w in graph.get(v, set()):
            if w not in index:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif on_stack.get(w, False):
                lowlinks[v] = min(lowlinks[v], index[w])

        if lowlinks[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                sccs.append(sorted(scc))

    for v in sorted(graph.keys()):
        if v not in index:
            strongconnect(v)

    return sccs
