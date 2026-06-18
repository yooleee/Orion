## What & why

What does this change do, and why is it needed? Keep it focused — small, reviewable PRs
are preferred over one large diff.

## Linked issue / plan reference

Closes #<issue>, or the section of [`plans/orion-plan.md`](../plans/orion-plan.md) /
[`docs/known-issues.md`](../docs/known-issues.md) this relates to. Orion is built
phase-by-phase, so note which phase this fits.

## Checklist

- [ ] Tests added or updated for the change, and `python -m pytest` is green locally.
- [ ] Docstrings (`Args:` / `Returns:` / `Why:`), file headers, and inline comments
      follow the project's annotation standards (see [CONTRIBUTING.md](../CONTRIBUTING.md)).
- [ ] Cross-platform considered — `pathlib` over path strings, no `shell=True`, no
      OS-specific path/shell assumptions (works on Linux, macOS, and Windows).
- [ ] Living docs updated in this PR if behavior or design changed —
      [`plans/orion-plan.md`](../plans/orion-plan.md), [`CHANGELOG.md`](../CHANGELOG.md),
      and/or [`docs/known-issues.md`](../docs/known-issues.md).
- [ ] No secrets committed — no API keys, tokens, webhook URLs, or `.env` contents in
      the diff (secrets stay in a gitignored `.env`).

## Notes for the reviewer

Anything that helps review — tradeoffs you weighed, parts you're unsure about, or
follow-ups intentionally left out of scope.
