# =============================================================================
# summarize.py
# -----------------------------------------------------------------------------
# Responsible for: The conditional LLM step — turning redacted raw git activity
#                  into a concise, audience-appropriate progress narrative.
# Role in project: Runs ONLY on the raw lane (git). Structured-lane updates
#                  (Phase 2) skip this entirely. Input is ALREADY redacted by the
#                  caller; this module redacts nothing itself.
# Model: Claude Haiku 4.5 (`claude-haiku-4-5`) — the lightest model adequate for
#                  summarization, per the project's "lightest adequate model"
#                  rule. Haiku does not support the `thinking`/`effort` params
#                  (they error), and a progress summary needs neither, so we make
#                  a plain Messages call.
# Security note: the model is the WEAKEST redaction layer and is never trusted as
#                  a control — but we still instruct it to report outcomes, not
#                  code or secrets. The real guarantees are the pre-LLM redaction
#                  and the human preview-before-send.
# =============================================================================

from __future__ import annotations

import anthropic

# The configured model id. Kept as a constant so a future bump (e.g. to Sonnet
# 4.6 if Haiku misses nuance on real diffs) is a one-line change in one place.
MODEL = "claude-haiku-4-5"

# A progress summary is short; bounding max_tokens keeps cost down and stays well
# under the SDK's non-streaming timeout guard (so we don't need streaming).
MAX_TOKENS = 1024


class SummarizerError(Exception):
    """Raised when the summarizer cannot produce a usable summary.

    Why:
        Lets the CLI catch summarization failures specifically and FAIL CLOSED —
        abort the run before sending anything, and without advancing state, so a
        later retry re-reports the same delta. Wrapping the SDK's exceptions in
        our own type also keeps the rest of the codebase from importing anthropic.
    """


# Base instructions shared by both share levels (DRY): the security-relevant
# rules that must hold regardless of detail level.
_SYSTEM_BASE = (
    "You write short progress updates for a developer's supervisor based on raw "
    "git activity (commit messages, a diffstat, and possibly a code diff).\n"
    "Rules:\n"
    "- Report OUTCOMES and PROGRESS (what was accomplished), not code.\n"
    "- Never reproduce code, file contents, secrets, keys, or tokens, even if they "
    "appear in the input.\n"
    "- Be factual and grounded in the provided activity; do not invent work.\n"
    "- Write in plain prose. No preamble like 'Here is the summary'."
)

# The two share levels differ only in how much technical specificity they expose.
# This is the privacy dial made concrete at the prompt level.
_SYSTEM_BY_LEVEL = {
    "high_level": (
        "Keep it high-level: 1-3 sentences focused on what was achieved and why "
        "it matters. Avoid file names, function names, and technical detail."
    ),
    "detailed": (
        "Provide a detailed but still abstracted update: you may mention which "
        "areas or components changed and the nature of the changes, but still no "
        "raw code or secrets."
    ),
}


def _build_system_prompt(share_level: str) -> str:
    """Assemble the system prompt for a given share level.

    Args:
        share_level: "high_level" or "detailed".

    Returns:
        The full system prompt string.

    Why:
        Splitting this out makes the share-level behavior unit-testable on its own
        and keeps summarize_raw focused on the API call. We fall back to the
        high_level (safer) instructions if an unexpected level slips through.
    """
    level_instructions = _SYSTEM_BY_LEVEL.get(share_level, _SYSTEM_BY_LEVEL["high_level"])
    return f"{_SYSTEM_BASE}\n\n{level_instructions}"


def summarize_raw(
    text: str,
    share_level: str,
    *,
    client: anthropic.Anthropic,
) -> str:
    """Summarize redacted raw git activity into a progress narrative.

    Args:
        text: The ALREADY-REDACTED git activity (commit messages + diffstat +
            optional capped diff). This function does not redact.
        share_level: "high_level" (outcomes only) or "detailed" (more specifics).
        client: An anthropic.Anthropic instance, injected by the caller. Passing
            it in (rather than constructing it here) keeps secret handling in the
            CLI and lets tests substitute a fake client — no network, no API key.

    Returns:
        The summary text produced by Claude.

    Why:
        This is the single LLM call in the pipeline. We use a plain (non-thinking,
        non-streaming) Messages request because the task is a short summarization
        and Haiku does not accept the thinking/effort params. We extract the text
        from the response's content blocks (the API returns a list of typed
        blocks; we want the text ones) and treat any API failure or empty result
        as a SummarizerError so the caller can fail closed.
    """
    system_prompt = _build_system_prompt(share_level)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as exc:
        # anthropic.APIError is the base for status (4xx/5xx), connection, and
        # timeout errors — one catch covers them all. The SDK already retried
        # transient failures (429/5xx) before raising.
        raise SummarizerError(f"Anthropic API call failed: {exc}") from exc

    # response.content is a list of content blocks; collect the text ones. A
    # normal summary is a single text block, but joining is robust to more.
    summary = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    if not summary:
        # An empty summary is useless and would otherwise become an empty report.
        raise SummarizerError("Summarizer returned an empty summary.")

    return summary
