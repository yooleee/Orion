<!-- =========================================================================
docs/archive/README.md
---------------------------------------------------------------------------
Responsible for: Explaining what lives in docs/archive/ and the convention for
                 moving docs here, so the docs/ folder stays tidy as the
                 project runs on.
Role in project: Housekeeping. Active reference docs and runbooks live at the
                 top of docs/; consumed planning docs are archived here.
========================================================================= -->

# docs/archive — consumed planning docs

This folder holds **planning documents that have served their purpose** but are kept for the
historical record. They are no longer actively maintained.

**The convention:** a **kickoff doc moves here once its phase is signed off** (or, for a
non-phase planning pass, once that pass has been executed). Its durable conclusions live on in
[`plans/orion-plan.md`](../../plans/orion-plan.md) (the execution roadmap) and
[`docs/orion-strategy.md`](../orion-strategy.md) (the strategy overlay); the archived kickoff is
the *as-it-was-launched* record.

**What stays at the top of `docs/` (active):** reference docs and runbooks still used day to day —
`known-issues.md`, `new-project-setup.md`, `git-hooks.md`, `scheduling.md`, `testing.md`,
`portability-smoke-test.md`, `test-messages.md`, and `orion-strategy.md`.

Links inside these archived docs were rewritten when they moved (one extra `../` level), so they
still resolve. If you add a doc here by hand, fix its relative links the same way.
