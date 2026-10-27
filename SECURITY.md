# Security Policy

## Supported versions

graphLM is pre-1.0 and released from a single line of development. Security fixes
land on `main` and ship in the next release on [PyPI](https://pypi.org/project/graphlm/).

| Version | Supported          |
| ------- | ------------------ |
| latest release (`main`) | ✅ |
| older releases | ❌ (please upgrade) |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Report privately through GitHub's **Private Vulnerability Reporting**:

1. Go to the **[Security tab](https://github.com/519lab/graphLM/security)** of
   the repository.
2. Click **"Report a vulnerability"**.
3. Fill in what you found.

This opens a private advisory visible only to you and the maintainer — no details
are exposed publicly, and nothing is sent over plain email.

Please include, as best you can:

- What the issue is and which file/function it's in.
- Steps to reproduce (a minimal project directory or command that triggers it).
- The impact — what an attacker gains.
- Any suggested fix.

You can expect an acknowledgement within about a week. If a fix is warranted,
we'll work it privately, ship it in a release, and credit you in the advisory
unless you'd rather stay anonymous.

## What's in scope

graphLM's core job is to **read a directory of code it did not write** and feed
parts of it to an LLM. It therefore treats every scanned file as untrusted input,
and the defenses around that are the security-sensitive surface. In scope:

- **A sensitive file being read or sent to the LLM** despite the guards — e.g. a
  secret-bearing file (`.env`, a private key) that gets scanned, or a secret that
  survives redaction and lands in the prompt.
- **A symlink escaping the project** to read a file outside the scanned directory.
- **Prompt injection** — content in a scanned file that manipulates the model into
  ignoring the "treat file content as data, not instructions" guard in a way that
  changes graphlm's behavior or exfiltrates data.
- **Path traversal or writing outside the intended output directory.**
- Any way to make graphlm execute code from, or leak data about, a scanned project
  beyond producing its intended map.

## What's *not* in scope

- **The content of a map you asked graphLM to generate.** graphlm sends selected
  file contents to whatever LLM endpoint you configure — that's the tool working
  as designed. Vet the endpoint you point it at; don't run graphlm against code you
  aren't willing to send to that endpoint. The redaction and sensitive-file guards
  are defense-in-depth, not a guarantee that nothing sensitive ever reaches the
  model.
- **Vulnerabilities in the LLM endpoint or in third-party dependencies.** Report
  those upstream (dependency issues via the relevant project). We'll bump a
  dependency when a fixed version exists.
- **The generated `GRAPH.html` loading D3 from a CDN.** This is documented
  behavior (the graph won't render offline); it is not a vulnerability report.
- Findings from automated scanners with no demonstrated, realistic impact.

## Handling of secrets

graphLM never intentionally persists or transmits secrets beyond the single LLM
request needed to build the map, and it tries hard *not* to read secret-bearing
files at all. LLM settings come only from the process environment and
`~/.config/graphlm/.env` — a `.env` in the working directory or the scanned
project is not loaded as graphlm's own configuration. If you find a case where a
secret is read, redaction is bypassed, or credentials are written somewhere
unexpected, that's exactly the kind of report we want — please send it privately
as above.
