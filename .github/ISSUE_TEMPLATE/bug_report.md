---
name: Bug report
about: Report something in Orion that behaves incorrectly
title: ""
labels: bug
assignees: ""
---

> **Redact secrets first.** Before pasting any config, logs, or output below, scrub
> real API keys, tokens, webhook URLs, and `.env` contents. Orion names secrets by the
> *environment variable* that holds them — share the variable name, never the value.
> This is a security issue, not a bug — please use the
> [Security policy](../../SECURITY.md) instead.

## Environment

- **OS and version:** (e.g. macOS 14 / Windows 11 / Ubuntu 24.04)
- **Python version:** (output of `python --version`; Orion requires 3.11+)
- **Orion commit / branch:** (output of `git rev-parse --short HEAD`, plus the branch)

## What happened

A clear description of the actual behavior.

## Steps to reproduce

1.
2.
3.

## Expected behavior

What you expected to happen instead.

## Logs / output

Paste relevant output, error messages, or the command you ran (secrets redacted — see
the note above). For git-hook issues, the hook log is at `<repo>/.git/orion-hook.log`.

```
<paste here>
```

## Anything else

Other context that might help — config shape (with secret values removed), which
collectors/channels were enabled, etc.
