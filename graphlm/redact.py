"""Content redaction for scanned files: secret-like values become markers.

``scan_project`` runs :func:`_redact_secrets` over every fragment's final
text (after skeletonisation/truncation) before anything reaches the model.
Which files are never read at all lives in :mod:`graphlm.sensitive_files`.
"""

from __future__ import annotations

import re


# Name fragments that mark a value as secret. Short words only count as a
# whole name segment (``db_pass``, ``PWD``, ``pw``, ``creds``), never inside a
# word (``bypass``, ``compass``, ``credit``), so ordinary code keeps its
# identifiers. ``passwordless`` / ``pass_through`` are not secret names.
_SEG_START = r"(?<![A-Za-z0-9])"
_SEG_END = r"(?![A-Za-z0-9])"
_PASSWORD_NAME = (
    r"(?:password(?!less)|passwd|passphrase|"
    + _SEG_START + r"(?:pass(?![_\-]?thr(?:ough|u))|pwd|pw)" + _SEG_END + ")"
)
_SECRET_NAME = (
    r"(?:" + _PASSWORD_NAME + r"|secret|token|credential|api[_\-]?key|"
    r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|"
    + _SEG_START + r"creds?" + _SEG_END + ")"
)
_IDENT_CH = r"[A-Za-z0-9_.\-]"
# A name that *describes* a secret rather than holding one: its file, path,
# URL, id, length, … (`DB_PASSWORD_FILE`, `token_url`, `secretName`).
_META_SUFFIXES = (
    "name", "names", "arn", "path", "file", "dir", "url", "uri", "id", "ids",
    "backend", "class", "type", "length", "len", "min", "max", "ttl", "header",
    "field", "label", "env", "prefix", "suffix", "expiry", "expires", "expire",
    "timeout", "count", "size", "format", "version", "var", "mode", "hint",
)
_NOT_META = "".join(rf"(?<![_.\-]{suffix})" for suffix in _META_SUFFIXES) + (
    "(?-i:"
    + "".join(f"(?<!{w})" for w in ("Name", "Path", "Url", "Uri", "Id", "Arn", "File", "Type", "Length"))
    + ")"
)


def _ident_containing(name: str) -> str:
    """An identifier run (letters, digits, ``_ . -``) that contains ``name``
    and does not end in a metadata suffix (``_name``, ``_path``, ``Url``, …).

    Linear by construction: it starts only where a run starts (a lookbehind,
    not ``\\b``, which fires at every ``.``/``-``), checks the run with a
    lookahead, then consumes it possessively — so each run is scanned once.
    ``[A-Za-z0-9_.-]*NAME[A-Za-z0-9_.-]*`` instead backtracks through every
    NAME occurrence from every boundary: quadratic on a long dotted run.
    """
    return rf"(?<!{_IDENT_CH})(?={_IDENT_CH}*?{name}){_IDENT_CH}++{_NOT_META}"


# An identifier that contains one of those fragments.
_SECRET_IDENT = _ident_containing(_SECRET_NAME)
_PASSWORD_IDENT = _ident_containing(_PASSWORD_NAME)
# Assignment operators: `=`, Go `:=`, `:` (YAML/JSON/kwargs), PHP/Ruby `=>`.
_ASSIGN = r"[\"']?\s*(?::=|=>|=|:)\s*"
# Optional string prefix (Python f/b/r/u and combinations).
_STR_PREFIX = r"(?:[bBrRfFuU]{1,2}(?=[\"']))?"
# A value that is a reference, not a secret: a URL, a template placeholder, an
# absolute/relative/Windows path, an ARN, or a dotted class/module path.
_NOT_A_REFERENCE = (
    r"(?![a-z][a-z0-9+.\-]*://)(?!\$\{|\{\{|%\()(?!/|\./|\.\./|~/|arn:|[A-Za-z]:\\)"
    r"(?![A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}[\"'])"
)
# A quoted literal that is worth hiding: 8+ characters, no whitespace, at
# least one digit and one letter (keys and tokens have both; enum-ish strings,
# field names and markup do not), and not a reference (above).
_SECRET_LITERAL_BODY = (
    _NOT_A_REFERENCE + r"(?![^\s\"'\\]*\{[^\s\"'\\]*\})"
    r"(?=[^\s\"'\\]*\d)(?=[^\s\"'\\]*[A-Za-z])[^\s\"'\\]{8,}"
)
# Values the unquoted `password = …` rule must leave alone because they are
# code: an operator/quote/bracket/sigil start, a string prefix, a keyword, a
# call, a type annotation, a dotted chain (`settings.DB_PASSWORD`), or a token
# holding brackets (`$_POST['password']`, `form.cleaned_data["password"]`).
_NOT_CODE_VALUE = (
    r"(?!none|null|false|true)"
    r"(?![=:\"'`!|>&*\[{(\\$<]|[bBrRfFuU]{1,2}[\"'])"
    r"(?!(?:lambda|await|new|not|yield|function|async)\b)"
    r"(?![A-Za-z_][\w.]*\()"
    r"(?-i:(?!(?:str|bytes|int|float|bool|Any|Optional|SecretStr|SecretBytes)\b))"
    r"(?![A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?![\w$.]))"
    r"(?![^\s\"',}]*[\[\]()])"
    r"(?![^\s\"',}]+[ \t]+(?:if|else|and|or|for|in|is)\b)"  # an expression
)
_TRIPLE_QUOTE = '(\"\"\"|' + "''')"

# (pattern, replacement), applied in order. The original rules keep their
# markers and relative order; the added ones close gaps a review found. Every
# pattern is linear in the input: name matches anchor at the start of an
# identifier run and are possessive, quoted bodies stop at the next quote or a
# length bound, and no two adjacent quantifiers can share characters.
# Compiled once at import.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, flags), repl)
    for pattern, repl, flags in (
        # AWS access key IDs (AKIA...)
        (r"(AKIA[0-9A-Z]{16})", r"[REDACTED:AWS_ACCESS_KEY]", 0),
        # AWS secret access keys. Separator: `=`/`:` with optional spaces, or
        # spaces alone — possessive, since `\s*[=\s:]\s*` was cubic on a long
        # run of spaces.
        (
            r'(?i)(aws_secret[_\s]*(?:access_)?key)(?:\s*+[=:]\s*+|\s++)["\']?([A-Za-z0-9/+=]{40})["\']?',
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
        # Slack / Discord webhook URLs: the path is the credential.
        (
            r"(?i)(hooks\.slack\.com/services/|discord(?:app)?\.com/api/webhooks/)[A-Za-z0-9_/\-]{8,}",
            r"\1[REDACTED:WEBHOOK]",
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
        # Triple-quoted password values (`password = """…"""`), before the
        # single-quote rules would match the empty `""` and leave the rest.
        (
            r"(?i)(" + _PASSWORD_IDENT + _ASSIGN + r"[bBrRfFuU]{0,2})" + _TRIPLE_QUOTE + r"([\s\S]{1,4096}?)\2",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        # …and an unterminated one (a truncated slice): hide the rest of the line.
        (
            r"(?i)(" + _PASSWORD_IDENT + _ASSIGN + r"[bBrRfFuU]{0,2})" + _TRIPLE_QUOTE + r"(?!\[REDACTED)([^\n]+)$",
            r"\1\2[REDACTED:PASSWORD]",
            re.MULTILINE,
        ),
        # Password assignments, including JSON `"password": "hello world"`,
        # Go `:=`, and string prefixes (`f"…"`, `b'…'`). Quoted values may
        # contain spaces; unquoted values stop at whitespace.
        (
            r'(?i)(["\']?(?:password|passwd|pwd)["\']?\s*(?::=|[=:])\s*[bBrRfFuU]{0,2})(")(?!""|/|~/)((?:\\.|[^"\\])*)(")',
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        (
            r"(?i)([\"']?(?:password|passwd|pwd)[\"']?\s*(?::=|[=:])\s*[bBrRfFuU]{0,2})(')(?!''|/|~/)((?:\\.|[^'\\])*)(')",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        # JS/TS template literals: password = `…` (not a `${…}` placeholder).
        (
            r"(?i)(" + _PASSWORD_IDENT + _ASSIGN + r")`(?!\$\{)([^`\n]{1,512})`",
            r"\1`[REDACTED:PASSWORD]`",
            0,
        ),
        (
            r"(?i)(" + _SECRET_IDENT + _ASSIGN + r")`(?!\$\{)" + _NOT_A_REFERENCE
            + r"(?=[^`\s]*\d)(?=[^`\s]*[A-Za-z])([^`\s]{8,512})`",
            r"\1`[REDACTED:SECRET]`",
            0,
        ),
        # Escaped quotes inside a string or shell line: `password=\"…\"`.
        (
            r"(?i)((?:password|passwd|pwd)\\?[\"']?\s*(?::=|[=:])\s*)(\\[\"'])((?:(?!\2)[^\n]){1,512}?)\2",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        (
            r"(?i)([\"']?(?:password|passwd|pwd)[\"']?\s*[=:]\s*)" + _NOT_CODE_VALUE + r"([^\s\"',}]+)",
            r"\1[REDACTED:PASSWORD]",
            0,
        ),
        # Connection strings with embedded passwords. The password is bounded
        # (512): unbounded, `redis://redis://…` rescanned to the end of the
        # text from every scheme — quadratic.
        (
            r"((?:mysql|postgres|postgresql|mongodb(?:\+srv)?|rediss?|amqps?)://(?:[^:@/\s]+)?:)([^@\s]{1,512})(@)",
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
        # Authorization headers: `Bearer <token>`, GitHub-style `token <t>`
        # and `Basic <base64>` after `Authorization`. (A bearer token holds a
        # digit; prose like "Bearer authentication" does not.)
        (r"(?i)(\bBearer\s+)(?=[A-Za-z0-9_\-.~+/]*\d)([A-Za-z0-9_\-.~+/]{16,}=*)", r"\1[REDACTED:BEARER]", 0),
        (
            r"(?i)(\bAuthorization[\"']?\s*[:=]\s*[\"']?token\s+)(?=[A-Za-z0-9_\-.]*\d)([A-Za-z0-9_\-.]{16,})",
            r"\1[REDACTED:TOKEN]",
            0,
        ),
        (
            r"(?i)(\bAuthorization[\"']?\s*[:=]\s*[\"']?Basic\s+)([A-Za-z0-9+/]{8,}={0,2})",
            r"\1[REDACTED:BASIC_AUTH]",
            0,
        ),
        # npm registry auth (`//registry.npmjs.org/:_authToken=…`, `_auth=`,
        # `_password=`): a copied .npmrc under another name is still read.
        (
            r"(?im)((?:^|[\s:])_(?:authToken|auth|password)[ \t]*=[ \t]*)([^\s\"'`,;]+)",
            r"\1[REDACTED:SECRET]",
            0,
        ),
        # netrc syntax: `machine host login user password SECRET`.
        (
            r"(?im)(^[ \t#]*machine[ \t]+\S+[ \t]+(?:login[ \t]+\S+[ \t]+)?password[ \t]+)([^\s\"'`,;]+)",
            r"\1[REDACTED:PASSWORD]",
            0,
        ),
        # curl basic auth: `curl -u user:secret …` / `--user user:secret`.
        (
            r"(\bcurl\b[^\n]{0,200}?\s(?:-u|--user)[ \t]*[\"']?[^\s:\"']+:)(?!\$)([^\s\"'@]+)",
            r"\1[REDACTED:PASSWORD]",
            0,
        ),
        # YAML block scalars for a secret-ish key (`password: |` then indented
        # lines): the indicator line holds no value, the lines below do.
        (
            r"(?im)^([ \t]*)(" + _SECRET_IDENT + r"[ \t]*:[ \t]*(?:!\S+[ \t]+)?[|>][-+0-9]*[ \t]*\r?\n)"
            r"((?:\1[ \t]+[^\r\n]*(?:\r?\n|$))+)",
            r"\1\2\1  [REDACTED:SECRET]\n",
            0,
        ),
        # Password-family names with a prefix/suffix (`db_pass`,
        # `smtp_password_value`, `passphrase`): any quoted value, spaces
        # included, like the plain `password = "…"` rule above.
        (
            r"(?i)(" + _PASSWORD_IDENT + _ASSIGN + _STR_PREFIX + r")([\"'])(?!\[REDACTED)"
            + _NOT_A_REFERENCE + r"((?:\\.|(?!\2)[^\\\n])+)\2",
            r"\1\2[REDACTED:PASSWORD]\2",
            0,
        ),
        # A quoted literal assigned to any secret-ish name, including names
        # with a prefix/suffix (`SECRET_KEY`, `X-Api-Key`) and values with
        # punctuation (Django keys).
        (
            r"(?i)(" + _SECRET_IDENT + _ASSIGN + _STR_PREFIX + r")([\"'])(?!\[REDACTED)"
            + _SECRET_LITERAL_BODY + r"\2",
            r"\1\2[REDACTED:SECRET]\2",
            0,
        ),
        # The same in call-argument / subscript form:
        # `setdefault("DB_PASSWORD", "…")`, PHP `define('DB_PASSWORD', '…')`,
        # `os.environ["API_TOKEN"] = "…"`.
        (
            r"(?i)([\"']" + _SECRET_IDENT + r"[\"']\s*(?:,|\]\s*=)\s*" + _STR_PREFIX + r")([\"'])(?!\[REDACTED)"
            + _SECRET_LITERAL_BODY + r"\2",
            r"\1\2[REDACTED:SECRET]\2",
            0,
        ),
        # Unquoted config values (YAML / INI) on a line of their own for a
        # secret-ish key. The value must hold a digit and a letter, end the
        # line, and not look like code or a reference: no call/collection
        # syntax or trailing comma, not a URL or path, not a plain lowercase
        # identifier (`x = pass1_tokens`) or a dotted attribute path.
        (
            r"(?im)^([ \t]*" + _SECRET_IDENT + r"[ \t]*[:=][ \t]*)(?!\[REDACTED)"
            + _NOT_A_REFERENCE
            + r"(?-i:(?![a-z_][a-z0-9_.]*[ \t]*$))"
            r"(?![A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+[ \t]*$)"  # a.b.Attr: code
            r"(?=[^\s\"'(\[{,]*\d)(?=[^\s\"'(\[{,]*[A-Za-z])([^\s\"'(\[{}#,]{6,})[ \t]*$",
            r"\1[REDACTED:SECRET]",
            0,
        ),
        # Long random-looking strings assigned to secret-ish names (`=` or
        # JSON `:`). Same matches as `["']?NAME[^\s"'=:]*…` but anchored at
        # the start of the name run and possessive, so it is linear (that form
        # was quadratic on a long run repeating NAME).
        (
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
    input, so a hostile file cannot make it take longer than a pass over its
    text per rule. Known gaps, by design: tokens shorter than 8 characters
    and space-containing values under non-password names (catching those
    would redact too much ordinary code).
    """
    redacted = content
    for pattern, repl in _RULES:
        redacted = pattern.sub(repl, redacted)
    return redacted
