"""The never-read list: files graphlm skips because they hold secrets.

``scan_project`` (and the symlink guard in ``scan_guards``) call
:func:`_is_sensitive_file` on every candidate before reading it, so a
matching file is never opened, listed, or sent anywhere. Content inside the
files that *are* read is handled by :mod:`graphlm.redact`.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


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
    # PGP / GPG keyrings; password-manager vaults. (Not `.asc`: it is also
    # AsciiDoc and detached signatures; an armored *private* key inside any
    # file is caught by the PGP content rule instead.)
    ".gpg",
    ".pgp",
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
    parent = path.parent.name.lower()
    if any(
        parent == cred_dir and _is_named_or_backup(name, {cred_name})
        for cred_dir, cred_name in _CREDENTIAL_PATHS
    ):
        return True

    stem = path.stem.lower()
    # Only apply name-based patterns to non-source files
    if suffix not in _SOURCE_EXTS_FOR_SECRET_NAMES:
        if suffix not in {".md", ".rst", ".adoc"} and _is_named_or_backup(
            name, _DOTLESS_CREDENTIAL_NAMES
        ):
            return True
        # Renamed private keys: id_rsa_backup, id_ed25519_old.
        if not name.endswith(".pub") and any(
            name.startswith(base + "_") for base in _SSH_PRIVATE_KEY_NAMES
        ):
            return True
        # A secret extension that is not the last one: server.key.txt,
        # terraform.tfstate.backup, prod.tfvars.json. (.env forms are handled
        # above, with their template exemptions.)
        # A public key or a template is not secret (`server.key.pub`,
        # `prod.tfvars.example`).
        inner = {x.lower() for x in path.suffixes[:-1]}
        final = suffix.lstrip(".")
        if (
            final != "pub"
            and final not in _ENV_SAFE_SUFFIXES
            and inner & {e for e in _SECRET_EXTS if e.startswith(".") and not e.startswith(".env")}
        ):
            return True
        for pattern in _SECRET_NAME_PATTERNS:
            if fnmatch.fnmatch(stem, pattern) or fnmatch.fnmatch(path.name.lower(), pattern):
                return True
    return False
