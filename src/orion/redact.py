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
    # trivial/empty values.
    #
    # KI-41 (2026-08-06) narrowed this. It used to accept its keyword ANYWHERE
    # inside the name, so English words matched -- `auth` inside `authenticated`
    # and `author` both fired on real documentation prose, which changed the
    # meaning of text a supervisor reads and padded the preview's "N potential
    # secret(s)" count with noise. The two narrowings are marked N1 and N2 below;
    # the scheme skip beside them fixes a leak found while pinning the old
    # behavior, not a false positive.
    #
    # N1 TOOK THREE ATTEMPTS AND TWO OF THEM LEAKED. Read this before editing it.
    #   1. A word BOUNDARY (keyword must end at a non-letter) -- REJECTED. It
    #      cannot tell `tokenizer` from `tokenValue`, and silently stopped
    #      redacting `secretKey` (the AWS SDK field), `TS_AUTHKEY` (Tailscale's
    #      env var), `MINIO_SECRETKEY`, `passwordHash`, `privateKeyPem`.
    #   2. A STEM list (`authentic`, `tokeniz`, ...) -- REJECTED. A stem is
    #      unconditional on what follows it, so it swallowed
    #      `authentication_string` (MySQL's password-hash column) and
    #      `AuthenticatorKey` (ASP.NET Identity's TOTP secret).
    #   3. What is here: a list of word FORMS, plus a rule that any credential
    #      noun in the name voids the exemption outright.
    # The through-line is that each rejected version was SIMPLER and read better,
    # and each one leaked. Simplicity is the right instinct almost everywhere in
    # this codebase; here it kept collapsing a distinction that is real.
    #
    # Calibrated against a corpus of real `share_level = "detailed"` collector
    # output (27 windows, 14 repos, 17.9 MB, measured 2026-08-06): 1212 hits ->
    # 990. All 87 distinct lost hits were read individually and every one is a
    # false positive; zero true positives were lost. The counts drift a little
    # between runs because the corpus is built from live local repos.
    #
    # HOW TO CHECK THE NEXT NARROWING, because the corpus alone will not do it.
    # A corpus of real diffs contains almost no real secrets, so it can show that
    # a false positive was removed and can NEVER show that a true positive
    # survived. Both rejected versions passed the corpus with zero losses. What
    # caught them was CONSTRUCTING credential names by hand -- `secretKey`,
    # `authentication_string` -- and diffing old against new. Do that. ---
    (
        re.compile(
            r"""
            ( [\w.\-]*                              # optional name prefix
              (?!                                   # N1: the keyword does not count when
                (?: author(?!i)                     # it starts one of these PROSE WORD
                  | authenticat(?:ed|es|ing|e)(?![a-z])   # FORMS. Each was observed matching
                  | authentic(?![a-z])              # real documentation prose:
                  | authoritative                   #   author/authors/Co-Authored-By,
                  | tokeniz(?:ers|er|ed|es|e|ing)(?![a-z])  # authenticated, isAuthenticated,
                )                                   #   WWW-Authenticate, NonAuthoritative,
                                                    #   tokenizer/tokenizers.
                                                    #
                                                    # ONLY verb/participle/adjective forms are
                                                    # listed. The MECHANISM NOUNS built on the
                                                    # same stems are deliberately absent --
                                                    # `authentication` (MySQL's
                                                    # authentication_string holds a password
                                                    # hash), `authenticator` (ASP.NET's
                                                    # AuthenticatorKey is a TOTP secret),
                                                    # `authorization`, `authority`,
                                                    # `tokenization` are all real credential
                                                    # field names. `authenticated` is a state;
                                                    # `authentication` is a thing that has a
                                                    # value. One letter apart, opposite
                                                    # answers -- which is why this is a list
                                                    # of word forms and not a stem match.
                (?! [\w.\-]* (?: key|secret|token|password|passwd|credential
                               | hash|salt|signature|sig|cert|pem|jwt|otp|pin ) )
                                                    # ...and even a listed prose form loses
                                                    # its exemption if the name carries a
                                                    # credential noun anywhere: `author_key`
                                                    # and `authorKey` are credentials, whatever
                                                    # `author` alone means.
              )
              (?:api[_\-]?key|secret|token|         # the secret-ish keywords
                 password|passwd|access[_\-]?key|
                 private[_\-]?key|auth|credential)
              [\w.\-]* )                            # optional name suffix -- left wide open
                                                    # ON PURPOSE. `secretKey`, `TS_AUTHKEY`,
                                                    # `passwordHash` and `tokenValue` are
                                                    # credential names that continue past
                                                    # the keyword, and a boundary here would
                                                    # leak every one of them.
            ( \s*[:=]\s* )                          # the assignment operator (kept)
            (?:(?:Bearer|Basic)[ \t]+)?              # skip an auth scheme word, so
                                                    # `Authorization: Bearer <opaque>`
                                                    # redacts the TOKEN and not the word
                                                    # "Bearer" (which is what the value
                                                    # matcher below reached first, leaving
                                                    # the credential in the clear). Two
                                                    # ways a looser version misfires, both
                                                    # observed: `\s+` instead of `[ \t]+`
                                                    # spans a newline and eats the NEXT
                                                    # line's value, and adding `Token` to
                                                    # the alternation makes prose like
                                                    # "OAuth: token exchange" match.
            ['"]?                                   # optional opening quote (dropped)
            (?!\[REDACTED_)                         # don't re-redact a token an earlier
                                                    # pattern already inserted (e.g. a
                                                    # 'token = sk-...' caught by the sk-
                                                    # rule first): prevents double-counting
                                                    # and the [REDACTED_API_KEY] ->
                                                    # [REDACTED_SECRET] mangling.
            (?!(?:true|false|null|none)(?![^\s'"])) # N2: a value that is EXACTLY one of
                                                    # these literals is not a secret --
                                                    # `token: null` is a fact about the
                                                    # config, and redacting it destroyed
                                                    # that fact for no gain. Exact-match
                                                    # only (the trailing lookahead pins the
                                                    # value's end), so `token: nullish4char`
                                                    # still redacts. Anything added to this
                                                    # list needs the same argument these
                                                    # four have: it CANNOT plausibly be a
                                                    # secret.
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
