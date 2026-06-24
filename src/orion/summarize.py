# =============================================================================
# summarize.py
# -----------------------------------------------------------------------------
# Responsible for: The conditional LLM step — turning redacted raw git activity
#                  into a concise, audience-appropriate progress narrative.
# Role in project: Runs ONLY on the raw lane (git). Structured-lane updates
#                  (Phase 2) skip this entirely. Input is ALREADY redacted by the
#                  caller; this module redacts nothing itself.
# Seam (B4): the summarizer is a provider-agnostic `Summarizer` (a Protocol with
#                  one `summarize(text, share_level) -> str` method). cli.py builds
#                  the configured backend and injects it, so the model — and the
#                  provider, including a LOCAL model — is chosen by config. The
#                  default is Claude Haiku 4.5 (`claude-haiku-4-5`), the lightest
#                  model adequate for summarization. Backends share the one
#                  security-relevant system prompt below (DRY).
# Security note: the model is the WEAKEST redaction layer and is never trusted as
#                  a control — but EVERY backend is instructed to report outcomes,
#                  not code or secrets. The real guarantees are the pre-LLM
#                  redaction and the human preview-before-send, regardless of
#                  which backend (cloud or local) runs.
# =============================================================================

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

import anthropic

# A progress summary is short; bounding max_tokens keeps cost down and stays well
# under the SDK's non-streaming timeout guard (so we don't need streaming). Shared
# by every backend so the bound is provider-independent.
MAX_TOKENS = 1024

# How long to wait on a local endpoint before failing closed. Generous because a
# local model on CPU can be slow to generate, but bounded so a hung server aborts
# the run (no advance, no send) instead of blocking it indefinitely.
LOCAL_TIMEOUT_SECONDS = 120.0


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
    "- Write in plain prose. No preamble like 'Here is the summary'.\n"
    # Mirror the project's Writing & Documentation Style (clean prose) so summaries
    # read like a person wrote them, not an LLM. This is a one-line prompt tune, NOT
    # a doc-inheritance mechanism (see docs/feature-ideas-reconciliation.md #7).
    "- Use clean prose: no em-dashes, no semicolons. Avoid generic LLM filler and "
    "stock phrasings (e.g. 'delve', 'leverage' to mean 'use', 'seamless', "
    "'it's worth noting')."
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
        and lets EVERY backend share the one security-relevant prompt (DRY) instead
        of each re-implementing it. We fall back to the high_level (safer)
        instructions if an unexpected level slips through.
    """
    level_instructions = _SYSTEM_BY_LEVEL.get(share_level, _SYSTEM_BY_LEVEL["high_level"])
    return f"{_SYSTEM_BASE}\n\n{level_instructions}"


def _require_nonempty(summary: str) -> str:
    """Return the stripped summary, or raise if it is empty after stripping.

    Args:
        summary: The raw summary text a backend extracted from its response.

    Returns:
        The stripped summary.

    Why:
        An empty summary is useless and would otherwise become an empty report;
        failing closed here is defense in depth (the CLI also guards). Shared by
        every backend so the "no empty summaries" rule is enforced in one place
        (DRY), regardless of provider.
    """
    cleaned = summary.strip()
    if not cleaned:
        raise SummarizerError("Summarizer returned an empty summary.")
    return cleaned


class Summarizer(Protocol):
    """The provider-agnostic summarizer seam (B4).

    Why:
        This is the one interface the rest of the pipeline depends on: cli.py
        builds the configured backend (Anthropic today, a local model too) and
        calls `summarize`. A Protocol (structural typing) means a backend just
        needs the method — it does not have to import or subclass anything — which
        keeps each backend self-contained and makes a fake trivial to write in
        tests. The seam is deliberately the SMALLEST thing that lets the model and
        provider vary; per-step model choice stays an additive change later.
    """

    def summarize(self, text: str, share_level: str) -> str:
        """Summarize already-redacted activity text into a progress narrative.

        Args:
            text: The ALREADY-REDACTED activity. Backends never redact.
            share_level: "high_level" (outcomes only) or "detailed".

        Returns:
            The summary text. Backends raise SummarizerError on failure/empty.
        """
        ...


class AnthropicSummarizer:
    """Summarizer backend backed by the Anthropic Messages API (the default).

    Args:
        client: An anthropic.Anthropic instance, injected by the caller. Passing
            it in (rather than constructing it here) keeps secret handling in the
            CLI and lets tests substitute a fake client — no network, no API key.
        model: The model id to request (defaults to Haiku via config; overridable
            to e.g. Sonnet if Haiku misses nuance on real diffs).

    Why:
        This is the original single LLM call, now behind the seam. We use a plain
        (non-thinking, non-streaming) Messages request because the task is a short
        summarization and Haiku does not accept the thinking/effort params. Any
        API failure or empty result becomes a SummarizerError so the caller can
        fail closed (abort before sending, without advancing state).
    """

    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model

    def summarize(self, text: str, share_level: str) -> str:
        """Summarize redacted activity via the Anthropic Messages API.

        Args:
            text: The ALREADY-REDACTED activity text.
            share_level: "high_level" or "detailed".

        Returns:
            The summary text produced by Claude.

        Why:
            See the class docstring. We extract the text from the response's
            content blocks (the API returns a list of typed blocks; we want the
            text ones) and treat any APIError or empty result as a SummarizerError.
        """
        system_prompt = _build_system_prompt(share_level)

        try:
            response = self._client.messages.create(
                model=self._model,
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
        )
        return _require_nonempty(summary)


class LocalSummarizer:
    """Summarizer backend for any OpenAI-compatible chat endpoint (B4).

    Args:
        base_url: The endpoint base, e.g. "http://localhost:11434/v1" for Ollama
            (also llama.cpp server, LM Studio, vLLM — all expose this shape). The
            chat-completions path is appended to it.
        model: The local model name to request (e.g. "llama3.1").
        api_key: An optional bearer token, when the endpoint requires one. Most
            local servers need none, so this is usually None.
        timeout: Seconds to wait before failing closed.

    Why:
        A LOCAL model is the headline payoff of the seam: summarization no longer
        needs an outbound call, so only delivery leaves the machine (a local-first
        privacy gain). We target the OpenAI-compatible /chat/completions shape
        because it is the common denominator across local runtimes, and we use
        stdlib urllib (mirroring the delivery modules) so this adds ZERO runtime
        dependencies. The redaction contract is identical to any other backend —
        the text arrives already redacted, and the same "report outcomes, not
        code/secrets" system prompt is sent. Every failure (unreachable endpoint,
        HTTP error, timeout, unparseable or empty response) becomes a
        SummarizerError so the run fails closed.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = LOCAL_TIMEOUT_SECONDS,
    ) -> None:
        # Normalize so callers can give the base with or without a trailing slash.
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def summarize(self, text: str, share_level: str) -> str:
        """Summarize redacted activity via an OpenAI-compatible chat endpoint.

        Args:
            text: The ALREADY-REDACTED activity text.
            share_level: "high_level" or "detailed".

        Returns:
            The summary text the local model produced.

        Why:
            The OpenAI chat schema puts the system prompt as the first message
            (role="system"), unlike Anthropic's top-level `system=` — but the
            prompt STRING is the shared, security-relevant one either way. We read
            the reply from choices[0].message.content and fail closed on any
            transport or shape problem.
        """
        system_prompt = _build_system_prompt(share_level)
        body = json.dumps(
            {
                "model": self._model,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            # Bearer auth is the OpenAI-compatible convention; only sent when the
            # endpoint was configured with a key (local servers usually need none).
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(
            self._url, data=body, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx from the endpoint (e.g. unknown model) arrive as HTTPError.
            raise SummarizerError(
                f"Local summarizer endpoint returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            # Connection refused (server down), DNS failure, or a read timeout.
            # TimeoutError is caught alongside in case a read timeout surfaces
            # unwrapped rather than as a URLError.
            raise SummarizerError(
                f"Could not reach local summarizer endpoint at {self._url}: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            # The body was not JSON, or not the OpenAI chat shape we expect.
            raise SummarizerError(
                f"Local summarizer returned an unexpected response shape: {exc}"
            ) from exc

        if not isinstance(content, str):
            # A null/non-string content would crash the empty-check; treat the
            # malformed reply as a failure rather than let it propagate oddly.
            raise SummarizerError(
                "Local summarizer response had a non-text message content."
            )

        return _require_nonempty(content)
