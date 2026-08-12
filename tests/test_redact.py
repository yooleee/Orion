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

import time

import orion.redact
from orion.redact import _Verdict, _classify_name, redact


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



def test_a_value_shorter_than_four_characters_is_not_redacted():
    """`token = abc` survives; `token = abcd` does not. Pins the 4-character minimum.

    Why this matters: the catch-all's value matcher requires 4+ characters, which is what
    keeps trivially short assignments — a truncated flag, an index, an empty-ish default —
    out of both the output and the "N potential secret(s)" preview count. Nothing pinned
    it: relaxing `{4,}` to `{1,}` leaves the entire suite green.

    The threshold is a fail-safe tradeoff worth stating rather than assuming. It is
    deliberately permissive in the leaking direction — a real 3-character secret would
    escape — on the reasoning that no credential worth protecting is three characters
    long, while short non-secret assignments are common in ordinary diffs. Both sides of
    the boundary are asserted so the constant cannot drift silently in either direction.

    Pre-existing gap in the regression floor, present since the catch-all was written and
    unrelated to any behaviour change. Confirmed real by mutation: relaxing `{4,}` to
    `{1,}` leaves every other test in this file passing.
    """
    for short in ("token = abc", "password: ab", "api_key=x"):
        result = redact(short)
        assert result.text == short, f"a sub-4-character value was redacted: {short!r}"
        assert result.hit_count == 0

    # ...and exactly at the boundary, redaction does fire.
    result = redact("token = abcd")
    assert result.text == "token = [REDACTED_SECRET]"
    assert result.hit_count == 1

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


# --- Pins for the extractor + classifier restructure (RDX-R Unit 1) ---
# Unit 1 changed HOW the catch-all decides, not WHAT it decides. Everything above
# this line is the parity floor and passed unchanged. What follows pins the three
# properties the restructure makes possible to get wrong.


def test_constructed_credential_names_from_real_products_are_redacted():
    """Credential names taken from real products all still redact.

    Why this matters: this is the check that a corpus of real diffs CANNOT do. A
    corpus holds almost no real secrets, so it proves a false positive was removed
    and never that a true positive survived — and two rejected narrowings passed a
    17.6 MB corpus, the whole suite, and mutation-checking while silently going
    blind to exactly these names (KI-41). Every name below is a real field, column
    or environment variable from a shipped product, not an invented example, and
    every one of them must redact through Units 2 and 3 as well.
    """
    names = [
        "secretKey",              # AWS SDK credential field
        "TS_AUTHKEY",             # Tailscale's node-auth env var
        "MINIO_SECRETKEY",        # MinIO server credential
        "authentication_string",  # MySQL's mysql.user password-hash column
        "AuthenticatorKey",       # ASP.NET Identity's TOTP shared secret
        "passwordHash",
        "privateKeyPem",
        "tokenValue",
        "authority_key",
        "tokenization_key",
    ]
    for name in names:
        result = redact(f"{name} = s3cr3tvalue0987")
        assert "s3cr3tvalue0987" not in result.text, f"LEAKED from: {name}"
        assert result.hit_count == 1, f"not caught once: {name}"
        assert name in result.text, f"name not preserved: {name}"


def test_the_extractor_anchors_on_the_credential_name_not_an_earlier_one():
    """`env: DATABASE_PASSWORD=...` matches on the PASSWORD, not on `env`.

    Why this matters: the extractor only yields candidates whose name already
    carries credential evidence, so on a line with two names it lands on the one
    that matters and the credential name survives as signal. An extractor that
    instead matched every name=value would anchor on `env`, and its match would
    swallow the whole `DATABASE_PASSWORD=hunter2supersecret` span as a mere value.
    Today that only costs the supervisor the name (see the leak this becomes in the
    next test); the exact-output assertions below are what distinguishes the two
    shapes, since both remove the password.
    """
    result = redact("env: DATABASE_PASSWORD=hunter2supersecret")
    assert result.text == "env: DATABASE_PASSWORD=[REDACTED_SECRET]"
    assert result.hit_count == 1

    result = redact("run: API_TOKEN=abcdef123456 npm test")
    assert result.text == "run: API_TOKEN=[REDACTED_SECRET] npm test"
    assert result.hit_count == 1


def test_a_declining_rule_would_shadow_a_secret_in_the_value_KNOWN_TRAP(monkeypatch):
    """Exempting a prose word like `oauth`/`author` would leak credentials. Pinned.

    Why this matters: this is the trap Unit 2 walks into, asserted here rather than
    described in a doc because a doc is not a test.

    The invariant, exactly: a decline cannot hide a LATER match — `re.sub` scans the
    original string, so `match.end()` and every subsequent span are the same whether
    we replace or hand back `group(0)`. The whole risk is the declined span's OWN
    text. So an exemption is sound only if the entire matched span is safe to emit
    verbatim, which is a claim about the VALUE. **A name-only rule cannot make it**,
    because `group(1)` is often a container key (a YAML key, an HTTP header) rather
    than the variable governing the value.

    The prose words KI-41 wants to exempt are the worst case, because they are also
    real credential names — `oauth`, `author`, `authentication` and `authorization`
    all contain `auth`, so the extractor yields them (asserted below, since that
    structural fact is what the whole trap rests on). Two distinct shapes leak:

        oauth: API_TOKEN=abcdef123456                    an assignment behind the key
        authorization: 8f4e2a91c7b3d5e60192837465afbcde  a raw token AS the value

    The second shape is the sharper one: it contains no `:` or `=` in its value, so
    the obvious remedies — re-scan the declined span, or refuse to decline while the
    span holds an assignment — both fail to fire. Exempting `authorization` also
    re-opens the `Bearer` leak that PR #152 closed, covered by its own test above.

    Nothing declines today, so none of this is live: the second half asserts the
    shipped code redacts every one of these, identically to `main`. When Unit 2
    closes the trap THIS TEST SHOULD FAIL, and its failure is the signal to rewrite
    it as a positive assertion — not to restore the trap.

    Recorded because two earlier versions of this test were wrong, both caught by an
    independent verifier and not by any local instrument: the first stubbed a decline
    on names the extractor never yields, so it exercised no decline path at all; the
    second claimed the trap was bounded to assignments, which the raw-token rows
    below disprove.
    """
    prose = {"oauth", "author", "authentication", "authorization"}
    leaky = [
        # (line, the credential that must not escape)
        #
        # The credential VALUES here are deliberately synthetic rather than realistic.
        # The first version used a real Stripe `sk_live_` shape and GitHub's secret-scanning
        # push protection blocked the push -- correctly, since it cannot know a literal is
        # fabricated. Nothing under test depends on the literal being plausible: what matters
        # is that the value is opaque, contains no ':' or '=', and matches none of the seven
        # specific patterns. The real vendor shapes these stand in for (Stripe `sk_live_`,
        # which our `sk-` rule misses by one character, and SendGrid `SG.`, which has no rule
        # at all) are named in the docstring and in KI-41, where prose cannot trip a scanner.
        ("oauth: API_TOKEN=abcdef123456", "abcdef123456"),
        ("authentication: DB_PASSWORD=hunter2supersecret", "hunter2supersecret"),
        ("authorization: 8f4e2a91c7b3d5e60192837465afbcde", "8f4e2a91c7b3d5e60192837465afbcde"),
        ("oauth: EXAMPLEONLY_live_0000notarealkey0000", "EXAMPLEONLY_live_0000notarealkey0000"),
        ("author: EX.EXAMPLEONLYnotareal.0000key0000", "EX.EXAMPLEONLYnotareal.0000key0000"),
    ]

    # The structural fact the trap rests on: the extractor really does hand the
    # classifier the PROSE WORD on these lines, not the credential name behind it.
    # Without this, the test would pass against a stub that declines everything and
    # would pin nothing about prose exemptions at all.
    for line, _ in leaky:
        candidate = orion.redact._SECRET_ASSIGNMENT.search(line)
        assert candidate.group(1).lower() in prose, (
            f"extractor yielded {candidate.group(1)!r}, not a prose word, for {line!r}"
        )

    monkeypatch.setattr(
        orion.redact, "_classify_name",
        lambda name: _Verdict(redact=False, reason="test-prose-exemption")
        if name.lower() in prose
        else _Verdict(redact=True, reason="keyword-in-name"),
    )
    for line, _ in leaky:
        result = redact(line)
        # THE TRAP, asserted so a supervisor does not discover it instead.
        assert result.text == line, f"trap closed for {line!r} -- rewrite this test"
        assert result.hit_count == 0

    # Control: the SAME stub, on a line whose name is the credential itself rather
    # than a prose container key, still redacts. This is what makes the test about
    # prose exemptions specifically, rather than about declining in general -- a
    # stub that refused every name would pass everything above and pin nothing.
    result = redact("API_TOKEN=abcdef123456")
    assert result.text == "API_TOKEN=[REDACTED_SECRET]"
    assert result.hit_count == 1

    # ...and with no exemption in play -- the shipped behaviour -- every one is caught.
    monkeypatch.undo()
    for line, secret in leaky:
        result = redact(line)
        assert secret not in result.text, f"LIVE LEAK from: {line}"
        assert result.hit_count == 1


def test_a_declined_candidate_is_emitted_verbatim_quotes_and_scheme_word_included(monkeypatch):
    """A declined span comes back byte-identical, including text redaction would drop.

    Why this matters: "the declined span is emitted verbatim" is the foundation of the
    whole exemption invariant — a decline cannot hide a later match, so an exemption is
    sound if and only if its entire matched span is safe to emit. Every other test that
    exercises a decline uses quote-free, scheme-free input, which leaves the one property
    Unit 2 will reason from untested on exactly the inputs where it is least obvious.

    The asymmetry is worth seeing, because it runs the safe way. On

        auth = "Bearer abcdef1234"

    the matched span is `auth = "Bearer abcdef1234` — the opening quote and the scheme
    word are inside it but outside groups 1 and 2, so the REDACT path discards them and
    emits `auth = [REDACTED_SECRET]`. The DECLINE path emits all of it. So "safe to emit
    verbatim" is a claim about strictly more text than redaction ever produces, which is
    the conservative direction — but it also means an exemption author cannot reason
    about the value alone and ignore the scheme word sitting in front of it.
    """
    monkeypatch.setattr(
        orion.redact, "_classify_name",
        lambda name: _Verdict(redact=False, reason="test-decline"),
    )
    for text in (
        'auth = "Bearer abcdef1234"',
        "auth = 'Basic dXNlcjpodW50ZXIy'",
        'Authorization: Bearer "abcdef1234567890"',
        "oauth:\n    API_TOKEN=abcdef123456",   # the separator crosses a newline
    ):
        result = redact(text)
        assert result.text == text, f"declined span was not emitted verbatim: {text!r}"
        assert result.hit_count == 0


def test_a_declined_candidate_is_not_counted_as_a_hit(monkeypatch):
    """A classifier that declines leaves the text alone AND adds nothing to hit_count.

    Why this matters: `re.subn` counts every MATCH, not every replacement, so a
    callable that hands back the original string still increments its count:

        p.subn(lambda m: m.group(0), "aaa bbb ccc")  ->  ('aaa bbb ccc', 3)

    Taking the count from `subn` would therefore inflate the preview's "N potential
    secret(s)" warning by one for every candidate the classifier turns down — noise
    in the exact control that redaction's incompleteness is backstopped by. No rule
    declines yet, so this cannot regress today; it is pinned NOW so that the first
    exemption rule (Unit 2) lands against a test that already holds the property,
    rather than one written afterwards to describe whatever it did.
    """
    monkeypatch.setattr(
        orion.redact, "_classify_name",
        lambda name: _Verdict(redact=False, reason="test-decline"),
    )
    text = "DATABASE_PASSWORD=hunter2supersecret and API_TOKEN=abcd1234"
    result = redact(text)
    assert result.text == text      # nothing replaced
    assert result.hit_count == 0    # and nothing counted


def test_classifier_reports_which_keyword_decided():
    """The classifier returns a decision AND the name of the rule that made it.

    Why this matters: the named reason is the whole argument for moving this
    judgement out of a regex. A 451-character pattern with nine lookarounds can be
    verified only as a whole; an ordered list of rules that each say why they fired
    can be reviewed one rule at a time. Nothing in the pipeline consumes the reason
    today — this test is its consumer, and it is what makes a future exemption rule
    reviewable rather than merely plausible.
    """
    assert _classify_name("DATABASE_PASSWORD") == _Verdict(True, "keyword-in-name:password")
    assert _classify_name("TS_AUTHKEY") == _Verdict(True, "keyword-in-name:auth")
    assert _classify_name("secretKey") == _Verdict(True, "keyword-in-name:secret")


def test_an_unrecognised_name_defaults_to_redacting():
    """A name the classifier has no rule for is redacted, not passed through.

    Why this matters: this is the fail-safe floor, and the direction of the
    asymmetry is the entire safety argument. Over-redacting costs a supervisor some
    clarity in text they can still ask about; under-redacting puts a credential in
    front of them. So "I do not recognise this" must never resolve to "let it
    through". The extractor cannot currently produce a name that reaches this
    branch, which is exactly why it needs a test of its own — an unreachable
    default is the kind of thing a later refactor deletes as dead.
    """
    verdict = _classify_name("wholly_unremarkable_name")
    assert verdict.redact is True
    assert verdict.reason == "unrecognised-name"


def test_pathological_name_run_does_not_stall_the_redactor():
    """Input that repeatedly almost-matches finishes fast instead of taking seconds.

    Why this matters: KI-48. The name group is greedy on both sides of the keyword,
    so before this unit the engine retried the whole prefix/keyword/suffix split at
    every offset inside a long name-like run. Measured on the old pattern, this
    exact 3.5 KB input took **14.6 seconds**, and `"authorization" * 400` took 20.3.
    Anchoring the match to the start of a name run makes it 0.012 s. redact() runs
    on collector output, which is text someone else may have written (a commit
    message, a diff hunk), so an input that costs seconds of CPU per KB is a real
    if low-severity exposure — previously bounded only by the git collector's
    400-line cap, a containment argument resting on a constant in another module.

    The 2-second bound is deliberately loose: it sits ~160x above the measured time
    so a slow or loaded CI box cannot make it flap, while still being ~7x BELOW the
    old behaviour, so the property it pins genuinely fails on the old pattern —
    confirmed by mutation, not assumed: commenting out the anchor makes this test
    fail with a 14 s run.
    """
    payload = ("secret_token_auth_" * 200) + "Z"
    start = time.perf_counter()
    redact(payload)
    assert time.perf_counter() - start < 2.0
