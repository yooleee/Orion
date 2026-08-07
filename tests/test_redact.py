# =============================================================================
# tests/test_redact.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the behavior of the redactor — the single most
#                  important safety control in Orion. Each test names a real
#                  secret format that must never leak, plus the false-positive
#                  guard (ordinary prose must survive untouched).
# Role in project: This is the corpus from the plan's "security release gate."
#                  If any of these fail, Phase 1 is not done.
# =============================================================================

from orion.redact import redact


def test_empty_string_is_unchanged_with_zero_hits():
    """Empty input returns empty output and a zero hit count.

    Why this matters: the no-activity / empty-body paths call redact; it must not
    choke on empty input or report phantom hits.
    """
    result = redact("")
    assert result.text == ""
    assert result.hit_count == 0


def test_benign_prose_is_not_redacted():
    """Ordinary text with words like 'secret' (but no assignment) is untouched.

    Why this matters: a redactor that mangles normal commit messages would make
    summaries useless. The '=' / ':' requirement is what separates a real secret
    assignment from prose like 'the secret to good code is tests'.
    """
    text = "Refactored the auth module. The secret to good code is tests."
    result = redact(text)
    assert result.text == text
    assert result.hit_count == 0


def test_aws_access_key_id_is_redacted():
    """An AWS Access Key ID (AKIA...) is removed.

    Why this matters: AWS keys have a fixed, recognizable shape and are a classic
    accidental commit.
    """
    result = redact("aws_key = AKIAIOSFODNN7EXAMPLE done")
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert result.hit_count >= 1


def test_github_token_is_redacted():
    """A GitHub personal access token (ghp_...) is removed.

    Why this matters: another fixed-prefix token format that grants repo access.
    """
    token = "ghp_" + "a" * 36
    result = redact(f"token: {token}")
    assert token not in result.text
    assert result.hit_count >= 1


def test_google_api_key_is_redacted():
    """A Google API key (AIza...) is removed.

    Why this matters: fixed 'AIza' prefix, 39 chars total; common in configs.
    """
    key = "AIza" + "B" * 35
    result = redact(f"maps key {key}")
    assert key not in result.text
    assert result.hit_count >= 1


def test_sk_style_key_is_redacted():
    """An sk-/sk-ant- style key (Anthropic/OpenAI) is removed.

    Why this matters: this is the exact shape of the project's own API key; it
    must never appear in a report.
    """
    key = "sk-ant-api03-" + "x" * 40
    result = redact(f"ANTHROPIC_API_KEY={key}")
    assert key not in result.text
    assert result.hit_count >= 1


def test_slack_token_is_redacted():
    """A Slack token (xoxb-/xoxp-/xoxa-/xoxr-/xoxs-) is removed.

    Why this matters: Slack bot/user tokens grant workspace access and have a
    fixed 'xox?-' prefix. Phase 3 added Slack delivery, so a Slack token is now a
    plausible secret to encounter — and every other secret shape here has a test,
    so leaving this redaction pattern uncovered risks a silent regression in a
    SECURITY control. Uses a non-secret-ish context so it isolates the Slack
    pattern itself rather than the generic NAME=value catch-all.
    """
    token = "xoxb-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUv"
    result = redact(f"posted to Slack with {token} just now")
    assert token not in result.text
    assert "[REDACTED_SLACK_TOKEN]" in result.text  # the precise token was used
    assert result.hit_count >= 1


def test_jwt_is_redacted():
    """A JSON Web Token (three base64url segments) is removed.

    Why this matters: JWTs carry auth claims and appear in logs/headers.
    """
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N"
    result = redact(f"Authorization: Bearer {jwt}")
    assert jwt not in result.text
    assert result.hit_count >= 1


def test_pem_private_key_block_is_redacted():
    """A multi-line PEM PRIVATE KEY block is removed in full.

    Why this matters: private keys span many lines; a per-line redactor would
    leak the middle. This verifies the whole block (BEGIN..END) goes.
    """
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAabc123def456\n"
        "ghijklmnopqrstuvwxyz0987654321\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact(f"Here is the key:\n{pem}\nend")
    assert "MIIEowIBAAKCAQEA" not in result.text
    assert "BEGIN RSA PRIVATE KEY" not in result.text
    assert result.hit_count >= 1


def test_generic_assignment_redacts_value_keeps_name():
    """A NAME=value assignment for a secret-ish name redacts only the value.

    Why this matters: keeping the variable NAME (but not its value) preserves
    useful signal ("a password was set here") without leaking the secret. This is
    the catch-all that covers formats the specific patterns miss.
    """
    result = redact("DATABASE_PASSWORD=hunter2supersecret")
    assert "hunter2supersecret" not in result.text
    assert "DATABASE_PASSWORD" in result.text  # name preserved
    assert result.hit_count >= 1


def test_dotenv_contents_are_redacted():
    """A realistic .env snippet has every value scrubbed.

    Why this matters: .env contents are the canonical thing we must never leak;
    this mirrors the plan's seeded-fake-key end-to-end check at the unit level.
    """
    env = (
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        "API_TOKEN=abcd1234efgh5678\n"
        "NORMAL_SETTING=hello"
    )
    result = redact(env)
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result.text
    assert "abcd1234efgh5678" not in result.text
    # A non-secret setting name is fine to keep; only secret-ish values go.
    assert "NORMAL_SETTING" in result.text


def test_hit_count_reflects_multiple_secrets():
    """Two distinct secrets in one text produce a hit count of at least two.

    Why this matters: the preview shows this number so the user knows redaction
    fired; an undercount would understate the risk that was caught.
    """
    token = "ghp_" + "z" * 36
    text = f"key AKIAIOSFODNN7EXAMPLE and token {token}"
    result = redact(text)
    assert result.hit_count >= 2


def test_single_secret_is_counted_once_not_double():
    """A specific-pattern secret inside a secret-ish-named assignment counts ONCE.

    Why this matters: `token = "sk-..."` is matched by the sk- rule (which inserts
    [REDACTED_API_KEY]) AND would then be re-matched by the generic NAME=value
    catch-all, which used to eat the just-inserted token, double-count it, and
    mangle it into [REDACTED_SECRET]" with a dangling quote. The preview's
    "N secrets redacted" notice must reflect ONE real secret here, not two, and
    the precise token must survive. This pins the fix (a negative lookahead that
    stops the catch-all from re-redacting an existing placeholder).
    """
    result = redact('token = "sk-abcdef0123456789abcdef0123456789abcdef0123456789"')
    assert "sk-abcdef0123456789" not in result.text  # the secret is gone
    assert result.hit_count == 1                       # counted exactly once
    assert "[REDACTED_API_KEY]" in result.text         # precise token preserved
    assert "[REDACTED_SECRET]" not in result.text      # NOT re-redacted/mangled


# --- A false negative found while pinning the catch-all's behavior for KI-41 ---
# Not a KI-41 fix: KI-41 is about over-matching. This is the opposite failure, and
# the one that matters -- a credential that reached a supervisor unredacted.


def test_bearer_scheme_word_does_not_shield_the_token_behind_it():
    """`Authorization: Bearer <opaque>` redacts the TOKEN, not just the word `Bearer`.

    Why this matters: this was a live leak. The catch-all's value matcher stops at
    whitespace, so on an `Authorization:` header it consumed the word `Bearer` and
    left the credential after it untouched:

        Authorization: Bearer aB3x...  ->  Authorization: [REDACTED_SECRET] aB3x...

    An opaque bearer token matches none of the specific patterns (it is not a JWT,
    not `sk-`, not `ghp_`), so the catch-all was its only cover — and it reported a
    hit_count of 1, which made the line look handled in the preview. That is the
    worst shape a redaction bug can take: a false negative wearing the costume of a
    catch.
    """
    token = "aB3xQ9zLmNp0RtVwYs2K"
    result = redact(f"Authorization: Bearer {token}")
    assert token not in result.text
    assert result.text == "Authorization: [REDACTED_SECRET]"
    assert result.hit_count == 1

    # Basic auth carries a base64 user:pass -- same shape, same cover.
    result = redact("Authorization: Basic dXNlcjpodW50ZXIy")
    assert "dXNlcjpodW50ZXIy" not in result.text
    assert result.hit_count == 1

    # The QUOTED header form. This is the same bug one character to the left: the
    # optional quote used to sit after the scheme word, so a quote before `Bearer`
    # ended the name-and-separator match and the token walked out. Found by review
    # after the first version of this fix, which is why it has its own assertions
    # rather than riding along in the loop above.
    for line in (f'Authorization="Bearer {token}"', f"Authorization='Bearer {token}'",
                 f'Authorization: Bearer "{token}"'):
        result = redact(line)
        assert token not in result.text, f"LEAKED from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"


def test_a_quoted_header_NAME_is_a_known_gap_not_a_silent_pass():
    """`headers={"Authorization": "Bearer <token>"}` is NOT redacted. Pinned as known.

    Why this matters: a quote before the NAME breaks the name-then-separator match
    entirely (`[\\w.\\-]*` cannot cross `"`), so this shape has never been covered --
    not before the scheme fix and not after it. It is a real shape (any Python or
    JS source that builds a headers dict) and it is a genuine hole in the catch-all.

    This test asserts the CURRENT behavior deliberately, so the gap is recorded in
    the suite rather than living only in a doc. It is out of scope for a fix here:
    quoted names are a property of the name matcher, not of the scheme skip, and
    widening the name matcher is its own calibrated change. If someone closes it,
    this test SHOULD fail -- and its failure is the signal to delete it, not to
    restore the gap. Tracked under KI-3.
    """
    token = "aB3xQ9zLmNp0RtVwYs2K"
    result = redact(f'headers={{"Authorization": "Bearer {token}"}}')
    assert token in result.text  # known gap, asserted so it cannot regress silently
    assert result.hit_count == 0


def test_the_scheme_skip_never_reaches_across_a_line():
    """A scheme word at end-of-line does not pull the NEXT line's value into the match.

    Why this matters: the first version of the scheme skip used `\\s+`, which matches
    newlines, so a line ending in `Bearer` swallowed the line break and redacted
    whatever followed on the next line — silently deleting content that was never a
    secret. `[ \\t]+` confines it. The property worth holding is broader than this
    one bug: the catch-all is line-oriented by construction (its value matcher stops
    at whitespace), and nothing added to it should quietly make it span lines.
    """
    text = "auth = Bearer\nnext_line_value_here"
    result = redact(text)
    assert "next_line_value_here" in result.text, "redaction crossed a line boundary"
