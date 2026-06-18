# Security Policy

Orion's leak-prevention guarantee is its top priority. This document explains which
versions get security fixes, how to report a vulnerability privately, and the security
posture you can rely on.

## Supported versions

Orion is **pre-release and unversioned** — there are **no released versions yet**.
Progress is tracked by *phase*, not SemVer (see [`CHANGELOG.md`](CHANGELOG.md)).
Security fixes therefore target the **latest `main`**. If you found an issue, please
confirm it reproduces against current `main` before reporting.

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.** A public issue
discloses the problem before there is a fix.

Instead, use **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab.
2. Click **"Report a vulnerability"** to open a private security advisory.
3. Describe the issue, how to reproduce it, and the impact you observed.

This keeps the report private between you and the maintainer until a fix is ready.

When you report, please **do not include real secrets** — no API keys, tokens, webhook
URLs, or `.env` contents. If a credential was exposed, say so and **rotate it
immediately**; the maintainer does not need the secret itself to act on the report.

Because Orion is a solo, pre-release project, there is **no guaranteed response-time
SLA** — reports are taken seriously and addressed as soon as is practical, but please
don't expect a same-day turnaround.

## Security posture

Orion is built around defense in depth, with a human as the final gate. What you can
rely on today:

- **Local-first collection.** Collectors only read local files. The single outbound
  call in the core pipeline is delivery; the only other outbound call is the optional
  LLM summarizer (and that step can run against a *local* model so nothing leaves the
  machine at all).
- **Redaction before anything leaves the machine.** Two redaction passes scrub API
  keys, tokens, private keys, JWTs, and secret-ish assignments — once before the LLM,
  once before sending. Sensitive files (`.env`, `*.pem`, `*.key`, `id_rsa*`,
  `credentials*`, `*secret*`, …) are excluded from the diff at collection time.
- **Preview-before-send is the guaranteeing layer.** No redactor is perfect, so a
  report is shown for human approval before it is sent (the preview reports how many
  secrets were redacted). For unattended paths, sending is gated behind an explicit
  per-project opt-in.
- **Secrets stay in a gitignored `.env`.** The config (`orion.toml`) holds only the
  *names* of environment variables, never secret values; `.env`, `orion.toml`, and
  `*.sqlite3` are gitignored. The state DB stores only the redacted report text.
- **The optional local relay authenticates its ingest.** Reports pushed to the
  relay carry a shared **Bearer token** that is compared in **constant time**
  (`hmac.compare_digest`), and the ingest **validates the payload's shape and version**
  before accepting it. The relay binds **loopback only** (`127.0.0.1`) at this stage.
- **The dashboard renders already-redacted content** and **HTML-escapes** its output,
  so a report's text cannot inject markup into the page.

These are real layers, not a promise of perfection. The honest guarantee is the
combination of redaction *and* the human preview — see
[`docs/known-issues.md`](docs/known-issues.md) (KI-3) for why redaction alone is treated
as one layer of defense, not a sufficient control on its own.
