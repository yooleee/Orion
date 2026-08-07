# =============================================================================
# redact.py
# -----------------------------------------------------------------------------
# Responsible for: Scrubbing obvious secrets (API keys, tokens, private keys,
#                  secret-ish assignments) out of free text.
# Role in project: The core SAFETY CONTROL. It runs twice in the pipeline — once
#                  on collected raw text BEFORE it reaches the LLM, and again as a
#                  net on the composed body BEFORE it is sent. It is also applied
#                  to anything that gets stored (history holds only redacted text).
# Honest limits: No pattern set is 100%. This is ONE layer of defense in depth;
#                the guaranteeing layer is the human preview-before-send. The
#                hit_count this returns surfaces in that preview so the human
#                knows redaction fired and scrutinizes harder.
# Assumptions: Input is text (git output, summaries). Pure function, no I/O.
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionResult:
    """The outcome of a redaction pass.

    Args:
        text: The input with every matched secret replaced by a token.
        hit_count: How many replacements were made across all patterns.

    Why:
        Returning the count alongside the text lets the CLI show a
        "N secrets redacted" notice in the preview. That visibility is part of
        the safety story — the user is told redaction fired, rather than trusting
        it silently.
    """

    text: str
    hit_count: int


# Ordered list of (compiled pattern, replacement). ORDER MATTERS: specific,
# high-confidence formats run first so a known key shape gets a precise token
# (e.g. [REDACTED_AWS_KEY]); the generic NAME=value assignment runs last as a
# catch-all for anything the specific patterns miss. This list is the documented
# single source of truth for "what a secret looks like" — extend it here.
#
# Why regex and not an entropy/ML detector: regex is explicit, auditable, fast,
# and dependency-free. A reader can see exactly what is and isn't caught, which
# matters for a safety control. Entropy-based detection is a possible future
# layer, not a Phase 1 need.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Multi-line PEM private key block. DOTALL so '.' spans newlines; this
    # must run before line-oriented patterns so the whole block is removed, not
    # just its header line. Non-greedy to stop at the first END marker. ---
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # --- AWS Access Key ID: literal 'AKIA' (or 'ASIA' for temp creds) + 16 A-Z0-9. ---
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # --- Google API key: literal 'AIza' + 35 url-safe chars (39 total). ---
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED_GOOGLE_API_KEY]"),
    # --- GitHub tokens: ghp_/gho_/ghu_/ghs_/ghr_ + 36+ alphanumerics. ---
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    # --- Slack tokens: xoxb-/xoxp-/xoxa-/xoxr-/xoxs- + token body. ---
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    # --- sk- style keys (Anthropic 'sk-ant-...', OpenAI 'sk-...'): 'sk-' + 20+
    # url-safe chars. Covers the project's own API key shape. ---
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{20,}\b"), "[REDACTED_API_KEY]"),
    # --- JWT: three base64url segments separated by dots, first segment starts
    # 'eyJ' (the base64 of '{"'). ---
    (
        re.compile(r"\beyJ[0-9A-Za-z_\-]+\.eyJ[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+\b"),
        "[REDACTED_JWT]",
    ),
    # --- Generic catch-all: a variable whose NAME contains a secret-ish word,
    # assigned a value via '=' or ':'. Keeps the name (signal) but redacts the
    # value (the secret). The mandatory '=' / ':' is what prevents matching plain
    # prose that merely contains the word "secret" or "token".
    #   group 1: the name (e.g. DATABASE_PASSWORD)
    #   group 2: the separator and surrounding space (e.g. " = ")  -- preserved
    # The value is everything up to whitespace or a quote, min 4 chars to skip
    # trivial/empty values. ---
    (
        re.compile(
            r"""
            ( [\w.\-]*                              # optional name prefix
              (?:api[_\-]?key|secret|token|         # the secret-ish keywords
                 password|passwd|access[_\-]?key|
                 private[_\-]?key|auth|credential)
              [\w.\-]* )                            # optional name suffix
            ( \s*[:=]\s* )                          # the assignment operator (kept)
            ['"]?                                   # optional opening quote (dropped) -- BEFORE
                                                    # the scheme word, so `Authorization="Bearer
                                                    # <token>"` is covered as well as the
                                                    # unquoted form.
            (?:(?:Bearer|Basic)[ \t]+)?              # An HTTP auth scheme word, skipped so the
                                                    # CREDENTIAL after it is what gets redacted.
                                                    # Without this, the value matcher below stops
                                                    # at the first space, eats the word "Bearer",
                                                    # and leaves the token itself in the clear --
                                                    # while reporting a hit, so the line looks
                                                    # covered.
                                                    #
                                                    # Two looser forms were built and rejected.
                                                    # `\s+` instead of `[ \t]+` spans a newline
                                                    # and redacts the NEXT line's value. Adding
                                                    # `Token` to the alternation makes an
                                                    # already-matching prose line eat one more
                                                    # word ("OAuth: token exchange" loses
                                                    # "exchange" too) -- note it does NOT create
                                                    # a new match; nothing here can, since the
                                                    # scheme word is optional and both spellings
                                                    # already satisfy the value matcher.
            ['"]?                                   # ...and a quote after it, for the rarer
                                                    # `Authorization: Bearer "<token>"`.
            (?!\[REDACTED_)                         # don't re-redact a token an earlier
                                                    # pattern already inserted (e.g. a
                                                    # 'token = sk-...' caught by the sk-
                                                    # rule first): prevents double-counting
                                                    # and the [REDACTED_API_KEY] ->
                                                    # [REDACTED_SECRET] mangling.
            [^\s'"]{4,}                             # the secret value (redacted)
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
        r"\1\2[REDACTED_SECRET]",
    ),
]


def redact(text: str) -> RedactionResult:
    """Replace recognized secrets in text with redaction tokens.

    Args:
        text: Arbitrary text (git output, an LLM summary, a composed message).

    Returns:
        A RedactionResult with the scrubbed text and the number of replacements.

    Why:
        Applying every pattern in order and summing the replacement counts gives
        both the cleaned text and the visibility (hit_count) the preview needs.
        We use re.subn (not re.sub) precisely because it returns the count for
        free, so we never have to guess whether redaction fired.
    """
    hit_count = 0
    result = text
    for pattern, replacement in _PATTERNS:
        # subn returns (new_string, number_of_subs_made) — exactly what we need.
        result, n = pattern.subn(replacement, result)
        hit_count += n
    return RedactionResult(text=result, hit_count=hit_count)
