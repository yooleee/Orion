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


# --- KI-41: calibrated narrowing of the generic catch-all ---------------------
# The catch-all used to accept its secret-ish keyword ANYWHERE inside the name,
# so ordinary words matched (`auth` inside `authenticated`, `author`). These pins
# draw the line between "a name that means a secret" and "an English word that
# happens to contain one", in BOTH directions — the false-positive tests below
# are only safe to keep because the true-positive tests beside them still pass.
#
# Read those two groups as a pair. The first attempt at this narrowing passed
# every false-positive test here and was still wrong: it required the keyword to
# end at a non-letter, which quietly stopped redacting `secretKey`, `TS_AUTHKEY`
# and `passwordHash`. No test failed, because no test asked. That is why
# test_keyword_prefix_credential_names_still_redact exists and why it is written
# from real-world credential names rather than invented ones.


def test_prose_words_containing_a_keyword_are_not_redacted():
    """Words that merely BEGIN with a secret keyword survive untouched.

    Why this matters: the first two lines are verbatim from the DF1 sweep
    (2026-07-21), where both were false positives on documentation prose in a
    real report a supervisor read. `authenticated: [REDACTED_SECRET]` gives no
    hint the value was `false` — the redactor silently changed the meaning of
    the text. Worse, each one inflated the "N potential secret(s) were redacted"
    preview count, which is the human control KI-3 calls the guaranteeing layer;
    routine noise there trains the operator to scroll past it.
    """
    text = (
        "authenticated: false\n"
        "author: yoolee committed the change\n"
        "unauthorized: true\n"
        "tokenizer: gpt2neox\n"
        "co-authored-by: Teammate B\n"
        "author_name: Supervisor A"
    )
    result = redact(text)
    assert result.text == text
    assert result.hit_count == 0


def test_keyword_delimited_within_a_name_still_redacts():
    """A keyword delimited by `_`, `-`, `.` or the name's edge is still a secret.

    Why this matters: this is the other half of the line drawn above, and the
    half that carries the security risk. Narrowing a redaction pattern trades
    false positives for false NEGATIVES, so every shape the narrowing was NOT
    meant to touch needs its own pin. `authorization` earns its place here: the
    exemption list carves it out explicitly (`author(?!i)`, so `author` is prose
    but `authoriz...` is not), and getting that carve-out wrong would leak a real
    HTTP credential name while looking like a tidy simplification.
    """
    secret = "aB3xQ9zLmNp0RtVwYs2K"
    for line in (
        f"AUTH_TOKEN={secret}",
        f"x-api-key: {secret}",
        f"oauth: {secret}",
        f"oauth_token: {secret}",
        f"authorization = {secret}",
        f"my.private-key={secret}",
    ):
        result = redact(line)
        assert secret not in result.text, f"leaked from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"


def test_plural_keyword_names_still_redact():
    """A pluralized keyword (`secrets`, `TOKENS`, `CREDENTIALS`) is still caught.

    Why this matters: an early version of this narrowing required the keyword to
    end at a non-letter, which would have killed every plural since `s` is a
    letter. That version carried an explicit `s?` to compensate, so plurals never
    actually broke — but the compensation was load-bearing and easy to drop. The
    shipped exemption-list design has no such edge (a plural is simply not one of
    the listed word forms), and the pin stays because plurals are common in real
    config and this is the cheapest possible guard on a whole class.
    """
    secret = "aB3xQ9zLmNp0RtVwYs2K"
    for line in (
        f"secrets: {secret}",
        f"API_TOKENS={secret}",
        f"AWS_CREDENTIALS={secret}",
        f"passwords: {secret}",
    ):
        result = redact(line)
        assert secret not in result.text, f"leaked from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"


def test_camel_case_secret_names_still_redact():
    """`clientSecret` / `accessToken` — a keyword PRECEDED by letters — still go.

    Why this matters: camelCase is how this repo's own TypeScript names things,
    and the keyword sits at the end of the name with letters in front of it. Any
    narrowing that reasons about where a keyword starts has to leave this family
    alone. Its mirror image lives in the next test.
    """
    for line in (
        'clientSecret: "GOCSPX-aB3xQ9zLmNp0RtVwYs2K"',
        "accessToken = aB3xQ9zLmNp0RtVwYs2K",
        "dbpassword: hunter2supersecret",
    ):
        result = redact(line)
        assert "aB3xQ9zLmNp0RtVwYs2K" not in result.text
        assert "hunter2supersecret" not in result.text
        assert result.hit_count == 1, f"not caught once: {line}"


def test_keyword_prefix_credential_names_still_redact():
    """`secretKey`, `TS_AUTHKEY`, `passwordHash` — keyword FIRST, letters after.

    Why this matters: this is the mirror of the camelCase test above, and it is
    the one that caught a real regression. The first version of KI-41's narrowing
    required the keyword to end at a non-letter, which reads as a clean rule and
    passes every false-positive test in this file — while silently emitting all
    of these in the clear. A blanket boundary cannot tell `tokenizer` from
    `tokenValue`; only naming the English words can.

    Every name below is one somebody actually ships, not one invented to make the
    test pass: `secretKey` is the AWS SDK field, `TS_AUTHKEY` is Tailscale's env
    var, `MINIO_SECRETKEY` is a MinIO deployment variable. If a future narrowing
    breaks this test, the right response is to narrow differently — these are
    credentials, and nothing about them is arguable.
    """
    secret = "aB3xQ9zLmNp0RtVwYs2K"
    for line in (
        f"TS_AUTHKEY=tskey-{secret}",
        f"secretKey: {secret}",
        f"SECRETKEY={secret}",
        f"MINIO_SECRETKEY={secret}",
        f"authkey = {secret}",
        f"authKey: {secret}",
        f"privateKeyPem={secret}",
        f'passwordValue = "{secret}"',
        f"passwordHash: {secret}",
        f'String tokenValue = "{secret}";',
        f"secretString={secret}",
        f"credentialValue={secret}",
    ):
        result = redact(line)
        assert secret not in result.text, f"LEAKED from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"

    # A name carrying BOTH a real keyword and an exempted word is still a secret:
    # the exemption is anchored at the keyword, not searched across the whole name.
    result = redact(f"admin_token_author_x: {secret}")
    assert secret not in result.text
    assert result.hit_count == 1


def test_mechanism_nouns_are_not_exempted_only_prose_forms_are():
    """`authentication_string` and `AuthenticatorKey` redact; `authenticated` does not.

    Why this matters: this is the sharpest edge in the whole pattern, and it was
    found by review rather than by reasoning. `authenticated` and `authentication`
    are one letter apart and want opposite answers — the first is a state a
    document reports, the second is a thing that HAS a value. MySQL's
    `mysql.user.authentication_string` holds a password hash; ASP.NET Identity's
    `AuthenticatorKey` is a TOTP shared secret. An exemption written as a stem
    match (`authentic…`) silently swallows both.

    So the exemption lists word FORMS, not stems, and the mechanism nouns built
    on the same stems are deliberately absent. If someone later "simplifies" this
    to a stem, that is what this test is here to stop.
    """
    secret = "aB3xQ9zLmNp0RtVwYs2K"
    for line in (
        f"authentication_string: {secret}",
        f"AuthenticatorKey = {secret}",
        f"AUTHENTICATION_KEY={secret}",
        f"authenticationSalt: {secret}",
        f"authentication: {secret}",
        f"tokenization_key = {secret}",
        f"authority_key: {secret}",
        f"authenticity_token: {secret}",
    ):
        result = redact(line)
        assert secret not in result.text, f"LEAKED from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"


def test_a_prose_word_plus_a_credential_noun_is_a_credential():
    """`author_key` redacts even though `author` alone is exempt prose.

    Why this matters: the exemption list is about what a word means on its own.
    `author` is prose; `author_key` is a credential, and no reading of `author`
    changes that. So a listed prose form loses its exemption whenever the name
    carries a credential noun anywhere — which also means the list can grow later
    without each new entry re-opening this hole.
    """
    secret = "aB3xQ9zLmNp0RtVwYs2K"
    for line in (
        f"author_key: {secret}",
        f"authorKey = {secret}",
        f"authenticated_secret: {secret}",
        f"tokenizer_password={secret}",
        f"authoritative_cert: {secret}",
    ):
        result = redact(line)
        assert secret not in result.text, f"LEAKED from: {line}"
        assert result.hit_count == 1, f"not caught once: {line}"

    # ...but a prose word plus an ORDINARY noun stays prose.
    for line in ("author_name: Supervisor A", "authenticated_at: yesterdayish"):
        result = redact(line)
        assert result.text == line, f"over-redacted: {line}"


def test_literal_non_secret_values_are_not_redacted():
    """A value that is exactly true/false/null/none passes through.

    Why this matters: `token: null` in a config dump is a fact about the config,
    not a secret, and redacting it both destroys that fact and inflates the
    preview count. The exemption is EXACT-match only — `nullish4char` is not the
    literal `null`, so it still redacts. That narrowness is the point: the list
    is safe precisely because no real secret is literally the word `false`, and
    a prefix match would have given away far more than that argument covers.
    """
    for line in ("token: null", "secret = None", "api_key: TRUE", "password: false"):
        result = redact(line)
        assert result.text == line
        assert result.hit_count == 0

    # ...but a value that merely STARTS with an exempt literal is still a secret.
    result = redact("token: nullish4char")
    assert "nullish4char" not in result.text
    assert result.hit_count == 1


def test_bearer_scheme_word_does_not_shield_the_token_behind_it():
    """`Authorization: Bearer <opaque>` redacts the TOKEN, not just 'Bearer'.

    Why this matters: this was a live leak, found while pinning current behavior
    for KI-41. The value matcher stops at whitespace, so the catch-all consumed
    the word `Bearer` and left the credential after it in the clear:

        Authorization: Bearer aB3x...  ->  Authorization: [REDACTED_SECRET] aB3x...

    An opaque bearer token matches none of the specific patterns (it is not a
    JWT, not `sk-`, not `ghp_`), so the catch-all was its only cover, and the
    hit_count of 1 made it look covered. Skipping an optional scheme word closes
    it. Only `Bearer` and `Basic` are skipped, and only over spaces/tabs — see
    the pattern comment for the two ways a looser version misfires.
    """
    token = "aB3xQ9zLmNp0RtVwYs2K"
    result = redact(f"Authorization: Bearer {token}")
    assert token not in result.text
    assert result.text == "Authorization: [REDACTED_SECRET]"
    assert result.hit_count == 1

    # Basic auth carries a base64 user:pass — same shape, same cover.
    result = redact("Authorization: Basic dXNlcjpodW50ZXIy")
    assert "dXNlcjpodW50ZXIy" not in result.text
    assert result.hit_count == 1


def test_the_scheme_skip_never_reaches_across_a_line():
    """A scheme word at end-of-line does not pull the NEXT line's value into the match.

    Why this matters: the first version of the scheme skip used `\\s+`, which
    matches newlines, so a line ending in `Bearer` swallowed the line break and
    redacted whatever followed on the next line — silently deleting content that
    was never a secret. `[ \\t]+` confines it. The property worth holding is
    broader than this one bug: the catch-all is line-oriented by construction
    (its value matcher stops at whitespace), and nothing added to it should
    quietly make it span lines.
    """
    text = "auth = Bearer\nnext_line_value_here"
    result = redact(text)
    assert "next_line_value_here" in result.text, "redaction crossed a line boundary"
