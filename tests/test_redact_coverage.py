"""Redaction coverage: credential shapes that used to reach the model, ordinary
code that must stay readable, linear running time, and the never-read list.

All "secrets" here are inert placeholders in the shape of real credentials.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from graphlm.redact import _is_sensitive_file, _redact_secrets
from graphlm.scanner import scan_project

# (case id, input, a substring of the secret that must not survive)
LEAKS = [
    ("django_key", "SECRET_KEY = 'django-insecure-9$k!x@w2#qz^v&8m*p(l)r+t=e'", "insecure-9$k"),
    ("sqlalchemy_driver", "url = 'postgresql+psycopg2://app:Pw0rdValue@db:5432/app'", "Pw0rdValue"),
    ("asyncpg", "postgresql+asyncpg://app:Pw0rdValue@db/app", "Pw0rdValue"),
    ("mssql", "mssql+pyodbc://sa:Pw0rdValue@sql/db", "Pw0rdValue"),
    ("basic_auth_url", "https://deploy:Pw0rdValue@artifacts.example.com/simple", "Pw0rdValue"),
    ("stripe", "stripe_key = sk_live_" "51HzAbCdEfGhIjKlMnOpQrStUv", "51HzAbCdEf"),
    ("openai", "OpenAI(api='sk-proj-" "AbCdEfGhIjKlMnOpQrStUvWxYz012345')", "AbCdEfGhIjKlMnOp"),
    ("slack", "SLACK = xoxb-" "1234567890-0987654321-AbCdEfGhIjKl", "0987654321"),
    ("google", "maps('AIza" "SyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW')", "SyA1b2C3"),
    ("gitlab", "glpat-" "AbCdEfGhIjKlMnOpQrSt", "AbCdEfGhIj"),
    (
        "jwt",
        "t = 'eyJhbGci" "OiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'",
        "dozjgNryP4J3",
    ),
    ("bearer_json", '{"Authorization": "Bearer abcDEF123ghiJKL456mnoPQR"}', "abcDEF123"),
    ("basic_header", "Authorization: Basic dXNlcjpQdzByZFZhbHVl", "dXNlcjpQdzBy"),
    ("suffixed_name_yaml", "prod_db_password_value: s3cr3tVal9", "s3cr3tVal9"),
    ("short_token", 'token = "shortsecret123456789"', "shortsecret"),
    ("db_pass", 'db_pass = "Pw0rdValue"', "Pw0rdValue"),
    ("passphrase", 'passphrase = "correct horse battery staple"', "horse"),
    ("setdefault", 'os.environ.setdefault("DB_PASSWORD", "Pw0rdValue")', "Pw0rdValue"),
    ("php_define", "define('DB_PASSWORD', 'Pw0rdValue');", "Pw0rdValue"),
    ("environ_subscript", 'os.environ["API_TOKEN"] = "tok3nValue99"', "tok3nValue99"),
    # Shapes a fresh-context review found leaking (fixed in the same change).
    ("go_walrus", 'password := "Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("go_walrus_token", 'token := "Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("f_string", 'password = f"Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("triple_quoted", 'password = """Zx9kQ2mP7vL4"""', "Zx9kQ2mP7vL4"),
    ("escaped_quotes", 'password=\\"Zx9kQ2mP7vL4\\"', "Zx9kQ2mP7vL4"),
    ("bytes_prefix", 'SECRET_KEY = b"Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("hyphen_header", 'headers = {"X-Api-Key": "Zx9kQ2mP7vL4"}', "Zx9kQ2mP7vL4"),
    ("creds", 'creds = "Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("pw", 'pw = "Zx9kQ2mP7vL4"', "Zx9kQ2mP7vL4"),
    ("yaml_block", "password: |\n  Zx9kQ2mP7vL4\n", "Zx9kQ2mP7vL4"),
    ("yaml_folded", "secret: >-\n  Zx9kQ2mP7vL4\nnext: 1\n", "Zx9kQ2mP7vL4"),
    ("curl_u", "curl -u admin:Zx9kQ2mP7vL4 http://x", "Zx9kQ2mP7vL4"),
    ("auth_token_header", "Authorization: token Zx9kQ2mP7vL4abcdefgh", "Zx9kQ2mP7vL4"),
    ("netrc_line", "machine x login u password Zx9kQ2mP7vL4", "Zx9kQ2mP7vL4"),
    ("npm_authtoken", "//registry.npmjs.org/:_authToken=Zx9kQ2mP7vL4", "Zx9kQ2mP7vL4"),
    ("npm_auth", "_auth = Zx9kQ2mP7vL4", "Zx9kQ2mP7vL4"),
    ("slack_webhook", "https://hooks.slack.com/services/T0001/B0002/Zx9kQ2mP7vL4", "Zx9kQ2mP7vL4"),
    (
        "pgp_block",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0AAAABCDEF\n-----END PGP PRIVATE KEY BLOCK-----",
        "lQOYBF0AAAAB",
    ),
]

# Ordinary source lines: redaction must leave them byte-identical.
SAFE = [
    "password = get_password()",
    "def check_password(self, raw_password):",
    "bypass = compute_bypass(flag)",
    "compass_heading = 270",
    "sort_key = 'created_at'",
    "TOKEN_URL = 'https://oauth.example.com/token'",
    "passthrough = True",
    "from django.contrib.auth.hashers import make_password",
    "password_field = forms.CharField(max_length=128)",
    "url = f'postgres://{user}@{host}/{db}'",
    "keyboard_layout = 'us'",
    "headers = {'Authorization': f'Bearer {token}'}",
    "git clone https://github.com/org/repo.git",
    "commit = 'd01389153b5e2c8ed3ed5b00408eae0188fe3a71'",
    'token.markup = "########"[:level]',
    '__slots__ = ("rel_path", "content", "estimated_tokens", "line_count")',
    'API_KEY_ENV = "TYPESAFE_API_KEY"',
    "LLM_MAX_OUTPUT_TOKENS = 128000",
    "self.pass1_context_tokens = pass1_context_tokens",
    "MLKEM768X25519PrivateKey = rust_openssl.hpke.MLKEM768X25519PrivateKey",
    '"""Basic introspection of modules."""',
    "pwd = os.getcwd()",
    "password: str",
    # Code a fresh-context review found altered (fixed in the same change).
    'user.password = form.cleaned_data["password"]',
    "$password = $_POST['password'];",
    'password = obj["password"]',
    "password = settings.DB_PASSWORD",
    "password = self.password",
    "password: process.env.DB_PASSWORD,",
    "password = args.password,",
    "password = x if y else z",
    "password = lambda: 1",
    "password = await get()",
    "password = new Foo()",
    "password = [a, b]",
    "password = {a: 1}",
    "password: !vault |",
    'SECRET_NAME = "my-app-secret1"',
    'secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:x1"',
    'SECRET_BACKEND = "django.contrib.auth.backends.ModelBackend1"',
    'private_key_path = "/etc/ssl/private/key1.pem"',
    'pass_through = "abc12345def"',
    "secret_name: my-secret-v2",
    "token_url: https://auth.example.com/v2/token",
    'passwordless = "magic-link-2"',
    'password_min_length = "8chars+"',
    "token: str = Field(...)",
    "max_tokens: 4096",
    "ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 8",
    "secret_string = var.db_password",
    "password_file: /run/secrets/db",
    "password = getpass.getpass()",
    "password = os.environ.get('DB_PASSWORD')",
    "credit_card = 'visa1234x'",
    "git push -u origin main",
    "docker run -u 1000:1000 img",
]


@pytest.mark.parametrize("case,text,secret", LEAKS, ids=[c[0] for c in LEAKS])
def test_credential_shape_is_redacted(case, text, secret):
    out = _redact_secrets(text)
    assert secret not in out, out
    assert "[REDACTED:" in out


@pytest.mark.parametrize("line", SAFE)
def test_ordinary_code_is_untouched(line):
    assert _redact_secrets(line) == line


def test_original_markers_are_stable():
    # The original rules keep their markers (tests and users key on them).
    assert "[REDACTED:API_KEY]" in _redact_secrets('api_key: "sk-' 'AbCdEfGhIjKlMnOpQrStUvWx"')
    assert "[REDACTED:GITHUB_TOKEN]" in _redact_secrets("ghp_" + "A" * 36)
    assert "[REDACTED:PASSWORD]" in _redact_secrets('password = "x y z"')


# --------------------------------------------------------------------------- #
# Linear time on hostile input (the PEM rule took ~109 s on 1.1 MB before)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "unit",
    [
        "-----BEGIN PRIVATE KEY-----\n",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n",
        "a.",
        "a-",
        "x_pass_",
        "token",
        "token='",
        "a://b:",
        "password_x: \n",
        # The two the review found (both were original rules).
        "aws_secret_key ",
        "redis://",
        'password = """',
        'password=\\"',
        "curl -u ",
        ":_authToken=",
        "secret_name_",
        "password = a.",
    ],
)
def test_redaction_is_linear_on_adversarial_input(unit):
    text = unit * (400_000 // len(unit))
    start = time.perf_counter()
    _redact_secrets(text)
    assert time.perf_counter() - start < 3.0


# --------------------------------------------------------------------------- #
# Never-read list
# --------------------------------------------------------------------------- #
NEVER_READ = [
    ".bash_history", ".zsh_history", ".psql_history", ".mysql_history", ".python_history",
    "prod.tfvars", "prod.tfvars.json", "terraform.tfstate", "terraform.tfstate.backup",
    ".kube/config", ".docker/config.json", ".config/gh/hosts.yml", ".git-credentials",
    "wp-config.php", "secring.gpg", "vault.kdbx",
    ".kube/config.bak", ".kube/config.yaml", ".docker/config.json.bak",
    "server.key.txt", "id_rsa_backup", "id_ed25519_old", "netrc.txt", "pgpass",
]
STILL_READ = [
    "Lib/netrc.py", "id_rsa.pub", "id_rsa_backup.pub", "config.json", "kube/config.yaml",
    "docker/config.json", "history.py", "README.md", ".env.example", "foo.env.example",
    "server.py", "my.key.py", "notes.txt",
    "README.asc", "docs/guide.asc", "SHA256SUMS.asc", "server.key.pub",
    "example.tfvars.example", "docs/netrc.md", "src/docker/config.json",
]


@pytest.mark.parametrize("name", NEVER_READ)
def test_secret_bearing_file_is_never_read(name):
    assert _is_sensitive_file(Path(name))


@pytest.mark.parametrize("name", STILL_READ)
def test_ordinary_file_is_still_read(name):
    assert not _is_sensitive_file(Path(name))


def test_scan_sends_redacted_fragments(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "settings.py").write_text("\n".join(text for _, text, _ in LEAKS) + "\n")
    (proj / ".bash_history").write_text("export X=1\n")
    frag = next(f for f in scan_project(proj).file_fragments if f.rel_path == "settings.py")
    for case, _, secret in LEAKS:
        assert secret not in frag.content, case
    assert ".bash_history" not in scan_project(proj).tree
