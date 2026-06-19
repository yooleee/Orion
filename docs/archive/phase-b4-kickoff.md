# Phase B4 Kickoff — Summarizer flexibility (provider-agnostic seam)

> **Read this, then [`plans/orion-plan.md`](../../plans/orion-plan.md) in full, before doing
> anything.** Like B3, B4's design is **NOT pre-settled** — only the *framing* is. Do a full
> **plan-mode pass**: surface the open decisions below with a recommendation for each, settle them
> with Yousuf, then build checkpoint by checkpoint, stopping at each boundary for review.

## Where things stand (as of 2026-06-17)

- **Horizon A** (A1–A4) shipped & signed off. **Horizon B:** B1 (git-hook triggers), B2 (session
  skill), B6 (config-inspect), and **B3 (richer rendering)** are signed off. `origin/main` in
  sync; `pytest`: **154/154**. **B4 is next**; B5 (conditional scheduling layer) follows.
- Doc map: roadmap + design → [`plans/orion-plan.md`](../../plans/orion-plan.md); shipped →
  [`CHANGELOG.md`](../../CHANGELOG.md); open concerns → [`known-issues.md`](../known-issues.md).

## What B4 is

Make the **summarizer step flexible** instead of hardwired to one Anthropic model. Today the
single LLM call (the raw/git lane) is fixed to `claude-haiku-4-5` via a constant in
`summarize.py`, and the call is made against an `anthropic.Anthropic` client built in `cli.py`.
B4 introduces a **provider-agnostic summarizer seam** so the model — and potentially the provider,
including an optional **local model** — can be chosen by config, while keeping the
"**lightest adequate model**" principle as the default. It is an *internal flexibility* change:
no new signal, no new channel, no change to redaction or preview.

## Settled framing — do NOT re-litigate

- **The seam is the point, not a pile of providers.** The goal is a clean abstraction so swapping
  the model/provider (or running locally) is additive later — *build the seam; don't build every
  future provider now* ("build seams, not futures"). How many concrete backends ship in B4 is an
  open decision below, but the bias is minimal.
- **Default stays Haiku/Anthropic.** Existing `orion.toml` files must keep working unchanged, and
  the default model stays the lightest adequate one (`claude-haiku-4-5`). Flexibility is opt-in.
- **Redaction and preview are untouched.** The summarizer only ever sees already-redacted text,
  and the human preview-before-send is unchanged. A different provider does not relax either; a
  *local* model is strictly more private (no external call), but the redaction contract is
  identical regardless of backend (see Security must-holds).
- **Keep dependencies minimal.** Any new backend must be justified against the
  open-source-simplicity constraint. Prefer stdlib (`urllib`) for an HTTP-to-local-endpoint
  backend over a heavy SDK, unless a real need argues otherwise.
- **Existing seam to build on:** `summarize_raw(text, share_level, *, client)` is already
  dependency-injected (the client is passed in by `cli.py`, not built inside), and
  `SummarizerError` already wraps the provider's exceptions so the rest of the codebase never
  imports `anthropic`. B4 generalizes this seam rather than inventing a new one.

## Open decisions to settle in plan mode (the heart of B4)

Surface each with a recommendation; settle with Yousuf before coding:

1. **Seam shape.** What is the provider-agnostic interface? Options: (a) a small `Summarizer`
   Protocol with `summarize(text, share_level) -> str`, with `cli.py` constructing the configured
   implementation; (b) keep `summarize_raw` but inject a provider-agnostic "summarize callable" /
   thin client wrapper; (c) a provider registry. Bias: the **smallest** abstraction that lets the
   model/provider vary (likely (a) or (b)) — not a registry yet.
2. **Config surface.** How is the model/provider chosen — a global `[summarizer]` table, a
   per-project field, or both? What TOML keys (e.g. `provider`, `model`)? It must default to
   Anthropic/Haiku and validate like the existing `share_level`/`collectors` checks in
   `config.py`. Decide the *scope* (global vs per-project) deliberately — per-project is more
   flexible but more surface.
3. **Which backends ship in B4.** Just the seam + the existing Anthropic backend refactored
   behind it (proving the abstraction)? Or also **one** additional backend — e.g. an
   OpenAI-compatible **local endpoint** (Ollama / llama.cpp server) reachable by stdlib
   `urllib` — as proof the seam is real? Building an actual second backend is the difference
   between "seam" and "seam + future." Recommend the minimum that proves the seam.
4. **"Per-step model choice" — in scope now?** The plan lists per-step model choice, but there is
   currently **one** LLM step (the git summary). Per-step selection may be premature until a
   second LLM step exists. Decide whether B4 builds it now or just keeps the seam so it is an
   additive change later.
5. **Secrets for non-Anthropic providers.** Per-provider API keys live in `.env` and resolve via
   `secrets.get_required`; decide the naming/lookup convention. A local model needs no key —
   `check` (B6) should reflect that a local-backend project requires no API key.
6. **Error handling + dependency surface.** Each backend must translate its failures into
   `SummarizerError` (fail-closed, no state advance). Confirm a local/HTTP backend can be built
   without a new runtime dependency (stdlib `urllib`), keeping the net-new-deps bar at 0 where
   possible.

## Seams / files likely involved (confirm in plan mode)

- `src/orion/summarize.py` — the main change: generalize the provider behind the seam; the
  hardcoded `MODEL` constant becomes configurable; `SummarizerError` stays the uniform failure type.
- `src/orion/cli.py` — build the *configured* summarizer/client instead of always
  `anthropic.Anthropic(...)`; the lazy "only construct if a raw collector has activity" behavior
  should be preserved.
- `src/orion/config.py` — new model/provider field(s) + validation (mirror `SHARE_LEVELS`-style
  constants and the existing per-project checks).
- `src/orion/secrets.py` — per-provider key lookup (only when a provider needs one).
- `orion.toml.example`, `.env.example` — document the new keys (and that local needs no key).
- Tests + docs — `test_summarize.py` (the DI pattern already supports a fake client; extend for
  the seam), `test_config.py` (new field validation), `docs/testing.md`, `README`, `CHANGELOG`,
  `plans/orion-plan.md` (flip the B4 row), and the multi-LLM memory note.

## Security / safety must-holds (non-negotiable)

- **Redaction is backend-independent.** The summarizer only ever receives already-redacted text;
  no backend (cloud or local) changes that, and none is trusted as a redaction layer. The
  "report outcomes, not code/secrets" system prompt should apply to every backend.
- **A third-party provider is still an external call** — the same privacy posture as Anthropic
  applies (redaction + preview). A **local** model keeps data on-machine (a privacy gain) but does
  not relax any guarantee.
- **Fail closed.** Every backend translates failures into `SummarizerError` so the run aborts
  before sending and without advancing state (a retry re-reports the same delta).
- **Preview-before-send and the `--yes`/`auto_send` gates are unchanged.**

## How to work this phase (project rules)

- **Plan before code:** a real plan-mode pass (genuine design decisions here). Surface the open
  decisions with recommendations; settle them; then build.
- **Smallest reviewable unit**; checkpoint after each and wait for review.
- **Keep docs living**; every change made cross-platform-minded (Windows / macOS / Linux).
- **Sign-off pattern:** implement (mark "awaiting sign-off"), then a separate "Sign off Phase B4"
  commit flips the markers. Commit/push only when Yousuf asks.

## First commands to run next session

```bash
# Confirm the baseline is still green (expect 154 passing).
.venv/bin/python -m pytest -q     # or: uv run --no-sync python -m pytest -q
```

Then read [`plans/orion-plan.md`](../../plans/orion-plan.md) (the B4 row) and `summarize.py` +
`cli.py`'s summarizer client construction, and start the **plan-mode pass**.
