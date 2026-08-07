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
# catch-all for anything the specific patterns miss. This list plus the catch-all
# below it is the documented single source of truth for "what a secret looks like".
# Extending it: a new fixed key SHAPE goes in this list; a new credential-ish NAME
# goes in _CREDENTIAL_KEYWORDS. NARROWING it is a different act and does not belong
# in either — an exemption is a claim about the whole matched span, not about a name,
# and _classify_name cannot see the span. See the note above _SECRET_ASSIGNMENT.
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
]


# =============================================================================
# The generic catch-all: candidate extractor + classifier.
# -----------------------------------------------------------------------------
# Everything above is a FIXED key format — 'AKIA' followed by 16 characters is a
# secret, full stop, and a regex expresses that perfectly. What follows is the
# opposite kind of decision: does this variable NAME denote a credential? That is
# a judgement, and three attempts to express it as a single cleverer regex failed
# (docs/known-issues.md, KI-41) — two of them by silently ceasing to redact real
# credential names, which is the only failure mode here that actually matters.
#
# So the decision is split in two:
#   1. an EXTRACTOR regex, which finds assignment-shaped candidates, and
#   2. a CLASSIFIER (_classify_name), which decides per candidate, with a named
#      reason for every decision.
# A named Python rule can be read, tested, and argued about one at a time; a
# nested lookahead cannot. Rules are read top to bottom, and every path defaults
# to redacting — see _classify_name.
# =============================================================================

# The credential-ish word fragments, in the order the extractor tries them. ONE
# vocabulary with TWO consumers: the extractor interpolates it into its
# alternation, and the classifier searches it to name which keyword it saw.
# Sharing the list is deliberate — an extractor and a classifier holding separate
# copies of the same vocabulary is exactly the drift class KI-43 was closed for.
#
# These are regex fragments, not plain words: the [_\-]? forms accept api_key /
# api-key / apikey alike. They are matched case-insensitively and ANYWHERE inside
# a name, which is why AUTH_TOKEN, secretKey and TS_AUTHKEY are all caught.
_CREDENTIAL_KEYWORDS: tuple[str, ...] = (
    r"api[_\-]?key",
    r"secret",
    r"token",
    r"password",
    r"passwd",
    r"access[_\-]?key",
    r"private[_\-]?key",
    r"auth",
    r"credential",
)

_KEYWORD_ALTERNATION = "|".join(_CREDENTIAL_KEYWORDS)

# The same vocabulary, compiled, for the classifier's use.
_KEYWORD_RE: re.Pattern[str] = re.compile(_KEYWORD_ALTERNATION, re.IGNORECASE)

# --- The candidate extractor: a variable whose NAME contains a secret-ish word,
# assigned a value via '=' or ':'. Keeps the name (signal) but redacts the value
# (the secret). The mandatory '=' / ':' is what prevents matching plain prose that
# merely contains the word "secret" or "token".
#   group 1: the name (e.g. DATABASE_PASSWORD)  -- also what the classifier judges
#   group 2: the separator and surrounding space (e.g. " = ")  -- preserved
# The value is everything up to whitespace or a quote, min 4 chars to skip
# trivial/empty values.
#
# WHY THE KEYWORD STAYS IN THE EXTRACTOR, rather than moving wholly into the
# classifier where it would read more naturally: a declined candidate still
# CONSUMES its span, and re.sub resumes after it. An extractor that matched every
# NAME=value and let Python judge would swallow a real secret sitting inside a
# benign name's value -- measured against the old behaviour:
#     env: DATABASE_PASSWORD=hunter2supersecret
# would match on the name "env", be declined, and the password would walk out.
# So the extractor yields only names that already carry credential evidence, and
# the classifier decides among those.
#
# NECESSARY BUT NOT SUFFICIENT -- read this before writing an exemption rule.
# The above stops a BENIGN first name shadowing a secret. It does NOT make
# exempting a name safe, and the difference is where Unit 2 will get hurt.
#
# The invariant, stated exactly: a decline cannot hide a LATER match, because
# re.sub scans the original string, so match.end() -- and therefore every
# subsequent match span -- is identical whether we replace or hand back
# group(0). The entire risk is the declined span's OWN text. Which means:
#
#     AN EXEMPTION IS SOUND ONLY IF THE WHOLE MATCHED SPAN IS SAFE TO EMIT
#     VERBATIM. That is a claim about the VALUE, not about the name.
#
# A name-only rule cannot make it. group(1) is frequently a container key -- a
# YAML key, an HTTP header -- not the variable that governs the value, so the
# text behind it is arbitrary. The prose words an exemption would target are the
# worst case, because they are also real credential names:
#     oauth: API_TOKEN=abcdef123456                    -> hides an assignment
#     authorization: 8f4e2a91c7b3d5e60192837465afbcde  -> hides a raw token
#     author: SG.ngeVfQFYQlKU0ufo8x5d1A.TEcCM5Vlso     -> hides a SendGrid key
# None of the last two contain ':' or '=' in the value, so "re-scan the span" and
# "refuse while the span holds an assignment" BOTH fail to fire. And exempting
# `authorization` re-opens the Bearer leak PR #152 closed. Nothing declines
# today, so none of this is live; it is pinned by
# test_a_declining_rule_would_shadow_a_secret_in_the_value_KNOWN_TRAP.
#
# Consequence for the seam: this judgement needs the span, which only _replace
# has. _classify_name(name) is NOT where an exemption can live. Widening it is
# Unit 2's first design decision, deliberately left open here rather than guessed
# at without a consumer. ---
_SECRET_ASSIGNMENT: re.Pattern[str] = re.compile(
    rf"""
    (?<![\w.\-])                            # Anchor the match at the START of a run of
                                            # name characters. The name group below is
                                            # greedy on both sides of the keyword, so
                                            # without this anchor the engine retries the
                                            # whole prefix/keyword/suffix split at every
                                            # offset inside a long name-like run, which
                                            # is what made pathological input take tens
                                            # of seconds (KI-48). The anchor cannot
                                            # remove a match: starting at the run's first
                                            # character, the prefix can still reach any
                                            # keyword the run contains.
    ( [\w.\-]*                              # optional name prefix
      (?:{_KEYWORD_ALTERNATION})            # the secret-ish keywords
      [\w.\-]* )                            # optional name suffix
    ( \s*[:=]\s* )                          # the assignment operator (kept)
    ['"]?                                   # optional opening quote (dropped) -- BEFORE
                                            # the scheme word, so `Authorization="Bearer
                                            # <token>"` is covered as well as the
                                            # unquoted form.
    (?:(?:Bearer|Basic)[ \t]+)?             # An HTTP auth scheme word, skipped so the
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
    [^\s'"]{{4,}}                           # the secret value (redacted)
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class _Verdict:
    """One classifier decision about one candidate name.

    Args:
        redact: True to replace the value with [REDACTED_SECRET].
        reason: The name of the rule that decided, e.g. "keyword-in-name:token".

    Why:
        The reason is the point of the whole restructure. An ordered list of named
        rules, each able to say why it fired, is auditable one rule at a time —
        which nested lookarounds in a 451-character regex are not. Nothing in the
        pipeline consumes the reason today; the tests assert on it, and it is what
        makes a future exemption rule reviewable rather than merely plausible.
    """

    redact: bool
    reason: str


def _classify_name(name: str) -> _Verdict:
    """Decide whether an assignment to this variable name should be redacted.

    Args:
        name: The matched variable name, e.g. "DATABASE_PASSWORD" or "hash_author".

    Returns:
        A _Verdict carrying the decision and the name of the rule that made it.

    Why:
        This is the seam the redaction restructure exists to create. Rules are read
        top to bottom and every path — including the fallthrough — REDACTS. That
        asymmetry is deliberate: over-redacting costs a supervisor some clarity,
        under-redacting leaks a credential, so "I do not recognise this" must never
        mean "let it through".

        Where exemptions go is deliberately NOT settled here. KI-41's prose words and
        KI-47's code-shaped values cannot be decided from the name alone: a decline
        emits the whole matched span verbatim, and this function never sees it. See
        the "NECESSARY BUT NOT SUFFICIENT" note above _SECRET_ASSIGNMENT for why a
        name-only rule is unsound, and treat widening this signature as Unit 2's
        first design decision rather than as a foregone one.
    """
    # Rule: the name carries credential evidence. Today the extractor guarantees
    # this, so the rule always fires — it is stated here rather than left implicit
    # because it is the fact every future exemption has to argue against, and
    # because naming the matched keyword makes the decision legible.
    keyword = _KEYWORD_RE.search(name)
    if keyword is not None:
        return _Verdict(redact=True, reason=f"keyword-in-name:{keyword.group(0).lower()}")

    # Fallthrough: unreachable from the extractor as it stands, and kept anyway.
    # It is the fail-safe floor, and it is what a caller passing a name directly
    # (a test, or a future extractor that yields more) hits.
    return _Verdict(redact=True, reason="unrecognised-name")


def _redact_secret_assignments(text: str) -> tuple[str, int]:
    """Apply the catch-all: extract assignment candidates, classify, replace.

    Args:
        text: Text that the specific high-confidence patterns have already run over.

    Returns:
        A (scrubbed text, number of accepted replacements) pair.

    Why:
        The count is accumulated INSIDE the closure rather than taken from re.subn,
        and that is not a stylistic choice. re.subn counts every match, including
        ones where the callable hands back the original string unchanged:

            p.subn(lambda m: m.group(0), "aaa bbb ccc")  ->  ('aaa bbb ccc', 3)

        A declined candidate is not a redaction, and counting it as one would
        inflate the preview's "N potential secret(s)" warning with non-events —
        eroding the very human control this module's incompleteness is backstopped
        by. Nothing declines yet, so the property is free today; it is pinned by a
        test precisely so it stays true when the first exemption rule lands.
    """
    hits = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal hits
        verdict = _classify_name(match.group(1))
        if not verdict.redact:
            return match.group(0)
        hits += 1
        # Name and separator are preserved; only the value goes.
        return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]"

    return _SECRET_ASSIGNMENT.sub(_replace, text), hits


def redact(text: str) -> RedactionResult:
    """Replace recognized secrets in text with redaction tokens.

    Args:
        text: Arbitrary text (git output, an LLM summary, a composed message).

    Returns:
        A RedactionResult with the scrubbed text and the number of replacements.

    Why:
        Applying every pattern in order and summing the replacement counts gives
        both the cleaned text and the visibility (hit_count) the preview needs.
        The specific fixed-format patterns run first so a known key shape gets its
        precise token, then the generic catch-all sweeps up whatever they missed —
        the same top-to-bottom order this module has always had.
    """
    hit_count = 0
    result = text
    for pattern, replacement in _PATTERNS:
        # subn returns (new_string, number_of_subs_made) — exactly what we need
        # for a plain string replacement, where every match IS a replacement.
        result, n = pattern.subn(replacement, result)
        hit_count += n

    # The generic catch-all runs LAST, as it always has. It is a function rather
    # than another _PATTERNS entry because its count comes from accepted
    # replacements, not from subn — see _redact_secret_assignments.
    result, n = _redact_secret_assignments(result)
    hit_count += n

    return RedactionResult(text=result, hit_count=hit_count)
