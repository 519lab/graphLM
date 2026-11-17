"""Secret handling for scanned files: the never-read list and content redaction.

Both halves were relocated verbatim from ``scanner.py`` (the patterns are
unchanged) so the scanner stays under the module size limit once it grew
skeletonisation and the nested-checkout guard. ``scan_project`` re-imports
``_is_sensitive_file`` and ``_redact_secrets`` from here, so every candidate
file is still screened and every fragment still passes through redaction —
the security invariants live in ``scan_project``, not in where the patterns
are defined.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


# Name fragments that mark a value as secret. ``pass`` and ``pwd`` only count
# as a whole name segment (``db_pass``, ``PWD``), never inside a word
# (``bypass``, ``compass``), so ordinary code keeps its identifiers.
_SECRET_NAME = (
    r"(?:password|passwd|passphrase|secret|token|credential|api_?key|"
    r"access_?key|private_?key|client_?secret|"
    r"(?<![A-Za-z0-9])(?:pass|pwd)(?![A-Za-z0-9]))"
)
_PASSWORD_NAME = (
    r"(?:password|passwd|passphrase|(?<![A-Za-z0-9])(?:pass|pwd)(?![A-Za-z0-9]))"
)
_IDENT_CH = r"[A-Za-z0-9_.\-]"


def _ident_containing(name: str) -> str:
    """An identifier run (letters, digits, ``_ . -``) that contains ``name``.

    Linear by construction: it starts only where a run starts (a lookbehind,
    not ``\b``, which fires at every ``.``/``-``), checks the run with a
    lookahead, then consumes it possessively — so each run is scanned once.
    ``[A-Za-z0-9_.-]*NAME[A-Za-z0-9_.-]*`` instead backtracks through every
    NAME occurrence from every boundary: quadratic on a long dotted run.
    """
    return rf"(?<!{_IDENT_CH})(?={_IDENT_CH}*?{name}){_IDENT_CH}++"


# An identifier that contains one of those fragments.
_SECRET_IDENT = _ident_containing(_SECRET_NAME)
_PASSWORD_IDENT = _ident_containing(_PASSWORD_NAME)
# A quoted literal that is worth hiding: 8+ characters, no whitespace, at
# least one digit and one letter (keys and tokens have both; enum-ish strings,
# field names and markup do not), and not a template placeholder (``${X}``,
# ``{{x}}``, ``%(x)s``) or a plain URL.
_SECRET_LITERAL_BODY = (
    r"(?![a-z][a-z0-9+.\-]*://)(?!\$\{|\{\{|%\()"
    r"(?=[^\s\"'\\]*\d)(?=[^\s\"'\\]*[A-Za-z])[^\s\"'\\]{8,}"
)

# (pattern, replacement), applied in order. The original rules are unchanged
# and keep their relative order (so their markers are stable); the added ones
# close gaps a review found (vendor token prefixes, any-scheme basic-auth URLs, bearer headers,
# PGP keys, more secret names, call-argument forms, short or punctuated
# values). Compiled once at import.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, flags), repl)
    for pattern, repl, flags in (
        # AWS access key IDs (AKIA...)
        (r"(AKIA[0-9A-Z]{16})", r"[REDACTED:AWS_ACCESS_KEY]", 0),
        # AWS secret access keys
        (
            r'(?i)(aws_secret[_\s]*(?:access_)?key)\s*[=\s:]\s*["\']?([A-Za-z0-9/+=]{40})["\']?',
            r"\1=[REDACTED:AWS_SECRET_KEY]",
            0,
        ),
        # GitHub tokens
        (r"(?i)((?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,})", r"[REDACTED:GITHUB_TOKEN]", 0),
        # Generic API key assignments (JSON `"api_key": "…"` too)
        (
            r'(?i)(["\']?(?:api[_-]?key|apikey)["\']?\s*[=:]\s*)["\']?([A-Za-z0-9_\-/+=]{20,})["\']?',
            r'\1"[REDACTED:API_KEY]"',
            0,
        ),
        # Vendor token prefixes: Stripe secret/restricted, OpenAI/Anthropic
        # `sk-…`, Slack, Google API, GitLab, PyPI, npm, SendGrid, JWTs.
        (
            r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
            r"|sk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_\-]{20,}"
            r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
            r"|AIza[0-9A-Za-z_\-]{35}"
            r"|glpat-[0-9A-Za-z_\-]{20,}"
            r"|pypi-AgE[A-Za-z0-9_\-]{50,}"
            r"|npm_[A-Za-z0-9]{36}"
            r"|SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}"
            r"|eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})",
            r"[REDACTED:TOKEN]",
            0,
        ),
        # Whole PEM / PGP private-key block (headers + body). The body may not
        # contain another BEGIN: without that, many BEGIN lines and no END made
        # the lazy scan quadratic (1 MB took minutes).
        (
            r"-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r"(?:(?!-----BEGIN )[\s\S])*?"
            r"-----END (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
            r"[REDACTED:PRIVATE_KEY]",
            0,
        ),
        (
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----"
            r"(?:(?!-----BEGIN )[\s\S])*?"
            r"-----END PGP PRIVATE KEY BLOCK-----",
            r"[REDACTED:PRIVATE_KEY]",
            0,
        ),
        # Leftover header lines if the block was truncated.
        (
            r"(-----BEGIN (?:(?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----)",
            r"[REDACTED:PRIVATE_KEY_HEADER]",
            0,
        ),
        (
            r"(-----END (?:(?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----)",
            r"[REDACTED:PRIVATE_KEY_HEADER]",
            0,
        ),
        # Truncated scan slice: BEGIN redacted, END gone, base64 body remains.
        (
            r"(\[REDACTED:PRIVATE_KEY_HEADER\](?:\r?\n[A-Za-z0-9+/=]{16,})+)",
            r"[REDACTED:PRIVATE_KEY_HEADER]\n[REDACTED:PRIVATE_KEY_BODY]",
            0,
        ),
        # Password assignments, including JSON `"password": "hello world"`.
        # Quoted values may contain spaces; unquoted values stop at whitespace.
        (
            r'(?i)(["\']?(?:password|passwd|pwd)["\']?\s*[=:]\s*)(")((?:\\.|[^"\\])*)(")',
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        (
            r"(?i)([\"']?(?:password|passwd|pwd)[\"']?\s*[=:]\s*)(')((?:\\.|[^'\\])*)(')",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        (
            # Not a call (`password = get_password()`) or a type annotation
            # (`password: str`, Pydantic fields): that is code, not a value.
            r"(?i)([\"']?(?:password|passwd|pwd)[\"']?\s*[=:]\s*)(?!none|null|false|true)(?![A-Za-z_][\w.]*\()"
            r"(?-i:(?!(?:str|bytes|int|float|bool|Any|Optional|SecretStr|SecretBytes)\b))([^\s\"',}]+)",
            r"\1[REDACTED:PASSWORD]",
            0,
        ),
        # Connection strings with embedded passwords
        (
            r"((?:mysql|postgres|postgresql|mongodb(?:\+srv)?|rediss?|amqps?)://(?:[^:@/\s]+)?:)([^@\s]+)(@)",
            r"\1[REDACTED:CONN_STRING_PASSWORD]\3",
            0,
        ),
        # Any other `scheme://user:password@host`: SQLAlchemy `+driver`
        # schemes, MSSQL / Oracle / MariaDB, and basic-auth HTTP(S) URLs.
        (
            r"((?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*+://[^:@/\s\"']*+:)(?!\[REDACTED)([^@/\s\"']++)(@)",
            r"\1[REDACTED:CONN_STRING_PASSWORD]\3",
            0,
        ),
        # Authorization headers: `Bearer <token>` and `Basic <base64>`.
        # (A bearer token holds a digit; prose like "Bearer authentication"
        # does not. `Basic` only counts after `Authorization`.)
        (r"(?i)(\bBearer\s+)(?=[A-Za-z0-9_\-.~+/]*\d)([A-Za-z0-9_\-.~+/]{16,}=*)", r"\1[REDACTED:BEARER]", 0),
        (
            r"(?i)(\bAuthorization[\"']?\s*[:=]\s*[\"']?Basic\s+)([A-Za-z0-9+/]{8,}={0,2})",
            r"\1[REDACTED:BASIC_AUTH]",
            0,
        ),
        # A quoted literal assigned to any secret-ish name, including names
        # with a prefix/suffix (`prod_db_password_value`, `db_pass`,
        # `SECRET_KEY`) and values with punctuation (Django keys).
        (
            r"(?i)(" + _SECRET_IDENT + r"[\"']?\s*(?:=|:|=>)\s*)([\"'])(?!\[REDACTED)" + _SECRET_LITERAL_BODY + r"\2",
            r"\1\2[REDACTED:SECRET]\2",
            0,
        ),
        # Password-family names with a prefix/suffix (`db_pass`,
        # `smtp_password_value`, `passphrase`): any quoted value, spaces
        # included, like the plain `password = "…"` rule above.
        (
            r"(?i)(" + _PASSWORD_IDENT + r"[\"']?\s*(?:=|:|=>)\s*)([\"'])(?!\[REDACTED)((?:\\.|(?!\2)[^\\\n])+)\2",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        # The same in call-argument / subscript form:
        # `setdefault("DB_PASSWORD", "…")`, PHP `define('DB_PASSWORD', '…')`,
        # `os.environ["API_TOKEN"] = "…"`.
        (
            r"(?i)([\"']" + _SECRET_IDENT + r"[\"']\s*(?:,|\]\s*=)\s*)([\"'])(?!\[REDACTED)" + _SECRET_LITERAL_BODY + r"\2",
            r"\1\2[REDACTED:SECRET]\2",
            0,
        ),
        # Unquoted config values (YAML / INI) on a line of their own for a
        # secret-ish key. The value must hold a digit and a letter, end the
        # line, and not look like code: no call/collection syntax or trailing
        # comma, and not a plain lowercase identifier (`x = pass1_tokens`),
        # so annotations, constants and assignments stay readable.
        (
            r"(?im)^([ \t]*" + _SECRET_IDENT + r"[ \t]*[:=][ \t]*)(?!\[REDACTED)"
            r"(?-i:(?![a-z_][a-z0-9_.]*[ \t]*$))"
            r"(?![A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+[ \t]*$)"  # a.b.Attr: code
            r"(?=[^\s\"'(\[{,]*\d)(?=[^\s\"'(\[{,]*[A-Za-z])([^\s\"'(\[{}#,]{6,})[ \t]*$",
            r"\1[REDACTED:SECRET]",
            0,
        ),
        # Long random-looking strings assigned to secret-ish names (`=` or JSON `:`)
        (
            # Same matches as `["']?NAME[^\s"'=:]*…` but anchored at the start
            # of the name run and possessive, so it is linear (that form was
            # quadratic on a long run repeating NAME).
            r'(?i)((?<![^\s"\'=:])["\']?(?=[^\s"\'=:]*?(?:secret|token|key|password|credential|auth))'
            r'[^\s"\'=:]*+["\']?\s*[=:]\s*)'
            r'["\']?([A-Za-z0-9_\-/+=]{32,})["\']?',
            r'\1"[REDACTED:SECRET]"',
            0,
        ),
    )
)


def _redact_secrets(content: str) -> str:
    """Replace secret-like patterns in file content with redaction markers.

    This is a best-effort redaction — it catches common patterns but is not
    a substitute for proper secret management. Every rule is linear in the
    input (no nested unbounded repetition), so a hostile file cannot make it
    take longer than a pass over its text per rule.
    """
    redacted = content
    for pattern, repl in _RULES:
        redacted = pattern.sub(repl, redacted)
    return redacted


# Secret-bearing file extensions — never read these
_SECRET_EXTS = {
    # TLS / certificate / key files
    ".pem",
    ".key",
    ".crt",
    ".cert",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".cer",
    # SSH keys
    ".ppk",
    # PGP / GPG keyrings and armored keys; password-manager vaults
    ".gpg",
    ".pgp",
    ".asc",
    ".kdbx",
    # Terraform variables and state (state files hold every resource secret)
    ".tfvars",
    ".tfstate",
    # Database credentials / connection strings
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.dev",
    ".env.secret",
    ".env.override",
    "env.local",
    "env.production",
    "env.staging",
    "env.secret",
}

# Glob patterns for secret-bearing file stems/names.
# Checked against both the stem and the full filename.
_SECRET_NAME_PATTERNS = {
    "*secrets*",
    "*secret*",
    "*credentials*",
    "*private*",
    "*password*",
    "*token*",
    "*api*key*",
    "*auth*key*",
}

# Any dotenv-style file (.env, .env.<anything>) is treated as secret-bearing,
# EXCEPT these deliberately-committed, non-secret template variants which are
# scanned normally (they document required vars without holding real values).
_ENV_SAFE_SUFFIXES = ("example", "sample", "template", "dist")

# Source-code extensions exempt from the name patterns above (token.py is code).
_SOURCE_EXTS_FOR_SECRET_NAMES = {".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java", ".cs", ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx", ".hh", ".hxx", ".php"}

# OpenSSH private-key filenames (no extension). `id_rsa.pub` is a public key
# and is not in this set. The `*private*` name glob misses these because the
# stem is `id_rsa`, not `private_key`.
_SSH_PRIVATE_KEY_NAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_ecdsa_sk",
    "id_ed25519_sk",
}

# Credential files whose secret is not `password = ...` (so redaction misses
# them) and whose names miss the *password* / *token* globs.
_CREDENTIAL_FILE_NAMES = {
    ".netrc",
    "_netrc",
    ".pgpass",
    ".htpasswd",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".pypirc",
    ".git-credentials",
    # WordPress keeps DB credentials and auth salts in this PHP file.
    "wp-config.php",
}

# The same credential files without the leading dot (`netrc.txt`, a copied
# `pgpass`). Only for non-source names: CPython ships `Lib/netrc.py`.
_DOTLESS_CREDENTIAL_NAMES = {"netrc", "pgpass", "htpasswd", "pypirc", "git-credentials"}

# Credential files known by their directory: (parent dir, name).
_CREDENTIAL_PATHS = {
    (".kube", "config"),
    (".docker", "config.json"),
    ("gh", "hosts.yml"),  # GitHub CLI tokens: ~/.config/gh/hosts.yml
}


def _is_history_file(lname: str) -> bool:
    """Shell / REPL / database client histories (`.bash_history`,
    `.psql_history`, `.python_history`, …): typed passwords and tokens."""
    return lname.startswith(".") and (
        lname.endswith("_history") or lname in {".history", ".histfile", ".zhistory"}
    )

# Exact never-read names that also have editor/suffix backups (`.bak`,
# `~`, `#name#`). `.env*` is handled separately (prefix rule).
_EXACT_SECRET_NAMES = (
    _SSH_PRIVATE_KEY_NAMES | _CREDENTIAL_FILE_NAMES | {".flaskenv"}
)


def _is_named_or_backup(lname: str, bases: set[str]) -> bool:
    """Exact never-read name, or a backup of one.

    Matches ``base``, ``base.bak`` / ``base.old``, ``base-orig``,
    ``base~``, ``#base#``. ``id_rsa.pub`` is excluded by the ``.pub``
    guard so public keys stay scannable.
    """
    if lname in bases:
        return True
    if lname.endswith(".pub"):
        return False
    for base in bases:
        if (
            lname.startswith(base + ".")
            or lname.startswith(base + "-")
            or lname == base + "~"
            or lname == "#" + base + "#"
        ):
            return True
    return False


def _is_sensitive_file(path: Path) -> bool:
    """Check if a file likely contains secrets or credentials.

    Checks file extension against known secret-bearing extensions and
    filename glob patterns for common secret-credential naming conventions.
    """
    suffix = path.suffix.lower()
    if suffix in _SECRET_EXTS:
        return True

    # Check full filename for non-dot-prefixed patterns like "env.production"
    if path.name.lower() in _SECRET_EXTS:
        return True

    # Any file whose name is `.env` or starts with `.env` is secret-bearing
    # (`.env.local`, `.envrc`, vim `.env~`, `.env-local`), except templates
    # `.env.example` / `.sample` / `.template` / `.dist`. Requiring `.env.`
    # as the next character missed editor backups and hyphen/underscore
    # suffixes.

    if path.name.lower() in _SSH_PRIVATE_KEY_NAMES:
        return True
    # Backups: id_rsa.bak, id_ed25519.old, vim id_rsa~, emacs #id_rsa#.
    # Exact-name matching missed these and redaction only strips BEGIN/END,
    # leaving the key body.
    lname = path.name.lower()
    # Exact names plus editor/suffix backups. Redaction misses netrc
    # (`password SECRET`) and pgpass (`host:port:db:user:SECRET`), so
    # backups of those files must never be read either.
    if _is_named_or_backup(lname, _EXACT_SECRET_NAMES):
        return True
    # Backups of secret *extensions*: app.jks.bak, server.key~, #foo.pem#.
    dotted_exts = {e for e in _SECRET_EXTS if e.startswith(".")}
    for ext in dotted_exts:
        if (
            lname.endswith(ext + ".bak")
            or lname.endswith(ext + ".old")
            or lname.endswith(ext + "~")
            or lname.endswith(ext + "-orig")
        ):
            return True
    if lname.startswith("#") and lname.endswith("#"):
        inner_suffix = Path(lname[1:-1]).suffix.lower()
        if inner_suffix in dotted_exts:
            return True

    # Any dotenv file (.env, .env.<anything>) is secret-bearing, except the
    # non-secret template variants. A fixed allowlist (_SECRET_EXTS) missed
    # arbitrary variants like .env.qa / .env.test; this catches them all.
    name = path.name.lower()
    if name == ".env" or name.startswith(".env"):
        if name.startswith(".env."):
            env_suffix = name[len(".env.") :]
            if env_suffix not in _ENV_SAFE_SUFFIXES:
                return True
        else:
            return True
    # `config.env` / `foo.env.local` — not a leading `.env`.
    if name.endswith(".env") or ".env." in name:
        if not any(name.endswith(".env." + s) for s in _ENV_SAFE_SUFFIXES):
            return True

    if name == ".flaskenv":
        return True
    if _is_history_file(name):
        return True
    if (path.parent.name.lower(), name) in _CREDENTIAL_PATHS:
        return True

    stem = path.stem.lower()
    # Only apply name-based patterns to non-source files
    if suffix not in _SOURCE_EXTS_FOR_SECRET_NAMES:
        if _is_named_or_backup(name, _DOTLESS_CREDENTIAL_NAMES):
            return True
        # Renamed private keys: id_rsa_backup, id_ed25519_old.
        if not name.endswith(".pub") and any(
            name.startswith(base + "_") for base in _SSH_PRIVATE_KEY_NAMES
        ):
            return True
        # A secret extension that is not the last one: server.key.txt,
        # terraform.tfstate.backup, prod.tfvars.json. (.env forms are handled
        # above, with their template exemptions.)
        inner = {x.lower() for x in path.suffixes[:-1]}
        if inner & {e for e in _SECRET_EXTS if e.startswith(".") and not e.startswith(".env")}:
            return True
        for pattern in _SECRET_NAME_PATTERNS:
            if fnmatch.fnmatch(stem, pattern) or fnmatch.fnmatch(path.name.lower(), pattern):
                return True
    return False
