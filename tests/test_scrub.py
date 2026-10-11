"""P1-T10: the scrubber's positive and negative tables.

This is the highest-stakes test file in the repository. "No PII in any artifact" is a hard
requirement from the brief, and this is the only thing that enforces it.

**Both tables matter equally.** The positive table proves PII is removed. The negative
table proves ordinary review text survives — a scrubber that redacts `₹5000`, `T+1`, or
`24x7` silently degrades every quote the pulse would ever publish, and nothing downstream
would reveal it. Over-redaction is a quieter failure than under-redaction, not a smaller one.

Every PII value below is invented: reserved example domains, the 90000 00000 phone block,
and checksum-valid-but-unowned Aadhaar numbers.
"""

from __future__ import annotations

import pytest

from pulse.analysis.scrub import (
    LABELS,
    RULE_ORDER,
    PiiLeakError,
    ScrubReport,
    assert_clean,
    has_pii,
    is_verhoeff_valid,
    scan_for_pii,
    scrub_reviews,
    scrub_text,
)
from pulse.models import Review, Source

# --------------------------------------------------------------------------------------
# Positive table — every PII class must be redacted
# --------------------------------------------------------------------------------------

POSITIVE_CASES: list[tuple[str, str, str]] = [
    # (rule expected to fire, text, the label it should leave behind)
    ("email", "Contact me at notareal@example.com for details.", "[email]"),
    ("email", "Email me at also.notreal@example.org, ticket attached.", "[email]"),
    ("email", "Write to first.last+tag@sub.example.co.in please.", "[email]"),
    ("url", "Details at https://example.invalid/thread if needed.", "[link]"),
    ("url", "See www.example.invalid/page for proof.", "[link]"),
    ("url", "Posted at http://example.invalid/a/b?c=d here.", "[link]"),
    ("upi", "My UPI is notareal@okaxis and it failed.", "[upi]"),
    ("upi", "Sent to notareal@ybl but nothing arrived.", "[upi]"),
    ("upi", "Tried notareal@paytm as well.", "[upi]"),
    ("pan", "My PAN is ABCDE1234F and it says invalid.", "[id]"),
    ("pan", "my pan abcde1234f is not accepted.", "[id]"),
    ("aadhaar", "Aadhaar 2994 1908 4318 was rejected twice.", "[id]"),
    ("aadhaar", "Aadhaar 299419084318 rejected again.", "[id]"),
    ("aadhaar", "Aadhaar 2994-1908-4318 also failed.", "[id]"),
    ("ifsc", "Transferred via SBIN0001234 and nothing arrived.", "[account]"),
    ("ifsc", "My branch code is hdfc0000123 apparently.", "[account]"),
    ("phone", "My number is +91 90000 00000 if you want to call.", "[phone]"),
    ("phone", "Reach me on 9000000000, I have screenshots.", "[phone]"),
    ("phone", "Call 90000-00000 for the recording.", "[phone]"),
    ("phone", "Alternate is 00 91 9000000000 as well.", "[phone]"),
    ("account", "Credited to 123456789012345 instead of mine.", "[account]"),
    ("handle", "Ping me @notarealhandle on twitter.", "[handle]"),
    ("self_name", "My name is Not A Realperson and nothing works.", "[name]"),
    ("self_name", "Myself Rahul Kumar, account not opened yet.", "[name]"),
    ("self_name_weak", "I am Rahul Kumar and my withdrawal is stuck.", "[name]"),
    ("self_contact", "Please contact me at my personal line urgently.", "[contact]"),
]


@pytest.mark.parametrize(("rule", "text", "label"), POSITIVE_CASES)
def test_pii_is_redacted(rule: str, text: str, label: str) -> None:
    scrubbed, hits = scrub_text(text)

    assert hits, f"no PII detected in {text!r}"
    assert label in scrubbed, f"expected {label} in {scrubbed!r}"
    assert rule in {hit.rule for hit in hits}, (
        f"expected rule {rule!r}, got {sorted({h.rule for h in hits})}"
    )


@pytest.mark.parametrize(("rule", "text", "label"), POSITIVE_CASES)
def test_the_pii_value_itself_never_survives(rule: str, text: str, label: str) -> None:
    """The specific guarantee the brief asks for: the value is gone from the output."""
    scrubbed, hits = scrub_text(text)
    for hit in hits:
        assert hit.matched not in scrubbed


# --------------------------------------------------------------------------------------
# Negative table — ordinary review text must survive untouched
# --------------------------------------------------------------------------------------

NEGATIVE_CASES: list[str] = [
    # Money and numbers that look nothing like identifiers.
    "Failed deposit of ₹5000 took eleven days to be refunded.",
    "Lost 1500 rupees due to slippage on a small cap.",
    "Charges were 23.60 including GST which is fair.",
    "Brokerage is 0.03% which is fine by me.",
    "My order for 100 shares at 2450 did not execute.",
    "Took 20 minutes to open the account end to end.",
    # Domain shorthand that resembles codes.
    "Settlement takes longer than the T+1 the app promises.",
    "Support is supposed to be 24x7 but nobody replies.",
    "SBI netbanking works but HDFC does not.",
    "App version 5.2.1 broke the portfolio screen.",
    "Waiting since 15/03/2026 for a refund.",
    "IPO allotment for 2 lots never came through.",
    # "I am ..." followed by a complaint, not a name. These are extremely common in
    # review text, and redacting them would gut the quote candidate pool.
    "I am giving 2 stars because withdrawal is slow.",
    "I am unable to withdraw my money at all.",
    "I am not getting the OTP on time.",
    "I am still waiting for a reply after 8 days.",
    "I am facing issues since the last update.",
    "I am Unable to login since the update.",
    "I'm done with this app honestly.",
    "This is the worst app for withdrawals.",
    "This is genuinely the best broker app I have used.",
    # Ordinary complaint prose with no identifiers at all.
    "KYC has been stuck in verification for nine days now.",
    "App crashes every time I open the chart on this phone.",
    "Statements are hard to find and the PDF has the wrong range.",
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_ordinary_review_text_is_left_alone(text: str) -> None:
    scrubbed, hits = scrub_text(text)

    assert not hits, f"false positive: {[h.rule for h in hits]} fired on {text!r}"
    assert scrubbed == text


# --------------------------------------------------------------------------------------
# Rule ordering — load-bearing, per architecture.md §10
# --------------------------------------------------------------------------------------


def test_specific_rules_run_before_the_digit_backstop() -> None:
    """If the catch-all ran first, every phone and Aadhaar would become `[number]`."""
    order = list(RULE_ORDER)
    backstop = order.index("digits")

    for specific in ("email", "upi", "pan", "aadhaar", "ifsc", "phone", "account"):
        assert order.index(specific) < backstop, f"{specific} must precede the digit backstop"


def test_email_runs_before_handle() -> None:
    """Otherwise `@example.com` in an address would be clipped as a social handle."""
    order = list(RULE_ORDER)
    assert order.index("email") < order.index("handle")
    assert order.index("upi") < order.index("handle")


def test_a_phone_number_is_labelled_phone_not_number() -> None:
    scrubbed, _ = scrub_text("Reach me on 9000000000 please.")
    assert "[phone]" in scrubbed
    assert "[number]" not in scrubbed


# --------------------------------------------------------------------------------------
# Idempotence — required by the stage 7 rescan
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("rule", "text", "label"), POSITIVE_CASES)
def test_scrubbing_is_idempotent(rule: str, text: str, label: str) -> None:
    """Re-scrubbing must be a no-op, and scrubbed text must scan clean.

    Stage 7 runs this same detector over the rendered note (architecture.md §9). If a rule
    could match its own placeholder, that rescan would abort a perfectly good run — and the
    abort would look like a PII leak rather than the false positive it is.
    """
    once, _ = scrub_text(text)
    twice, second_hits = scrub_text(once)

    assert not second_hits, f"{[h.rule for h in second_hits]} re-fired on scrubbed text"
    assert once == twice
    assert not has_pii(once)


def test_text_containing_a_placeholder_is_considered_clean() -> None:
    assert not has_pii("Please contact me at [contact] for the rest.")
    assert not has_pii("My name is [name] and nothing works.")


# --------------------------------------------------------------------------------------
# Multiple hits and positions
# --------------------------------------------------------------------------------------


def test_multiple_pii_classes_in_one_review() -> None:
    text = (
        "My name is Not A Realperson, email notareal@example.com, phone 9000000000, PAN ABCDE1234F."
    )
    scrubbed, hits = scrub_text(text)

    assert {"self_name", "email", "phone", "pan"} <= {hit.rule for hit in hits}
    assert not has_pii(scrubbed)


def test_hits_are_returned_in_document_order() -> None:
    text = "Call 9000000000 or write to notareal@example.com today."
    hits = scan_for_pii(text)
    assert [hit.start for hit in hits] == sorted(hit.start for hit in hits)


def test_hit_spans_point_at_the_matched_text() -> None:
    text = "Contact notareal@example.com now."
    (hit,) = scan_for_pii(text)
    assert text[hit.start : hit.end] == hit.matched == "notareal@example.com"


def test_empty_text_has_no_hits() -> None:
    assert scan_for_pii("") == []
    assert scrub_text("") == ("", [])


# --------------------------------------------------------------------------------------
# PiiHit must not leak through its own reporting
# --------------------------------------------------------------------------------------


def test_masked_hides_everything_but_the_first_character() -> None:
    (hit,) = scan_for_pii("Contact notareal@example.com now.")
    masked = hit.masked()

    assert masked.startswith("n")
    assert set(masked[1:]) == {"*"}
    assert len(masked) == len(hit.matched)
    assert hit.matched not in masked


def test_describe_does_not_contain_the_pii() -> None:
    """Reporting a leak must not itself be a leak."""
    (hit,) = scan_for_pii("Contact notareal@example.com now.")
    assert hit.matched not in hit.describe()
    assert "email" in hit.describe()


def test_assert_clean_passes_on_scrubbed_text() -> None:
    scrubbed, _ = scrub_text("Contact notareal@example.com now.")
    assert_clean(scrubbed, "test note")


def test_assert_clean_raises_without_echoing_the_pii() -> None:
    with pytest.raises(PiiLeakError) as exc_info:
        assert_clean("Contact notareal@example.com now.", "rendered note")

    message = str(exc_info.value)
    assert "notareal@example.com" not in message
    assert "rendered note" in message
    assert "stage 3" in message


# --------------------------------------------------------------------------------------
# Verhoeff checksum
# --------------------------------------------------------------------------------------


def test_verhoeff_accepts_a_valid_number() -> None:
    assert is_verhoeff_valid("299419084318")


def test_verhoeff_rejects_an_invalid_number() -> None:
    assert not is_verhoeff_valid("299419084316")
    assert not is_verhoeff_valid("123456789012")


def test_verhoeff_rejects_non_digits() -> None:
    assert not is_verhoeff_valid("29941908431X")


def test_a_checksum_failing_twelve_digit_run_is_still_redacted() -> None:
    """Declining the aadhaar label must never mean declining to redact.

    `123456789012` fails Verhoeff, so it is not an Aadhaar number — but it is still a long
    digit run, and the account rule claims it. The checksum buys label accuracy, not safety.
    """
    scrubbed, hits = scrub_text("Credited to 123456789012 apparently.")

    assert "123456789012" not in scrubbed
    assert {hit.rule for hit in hits} == {"account"}


# --------------------------------------------------------------------------------------
# Corpus-level scrubbing
# --------------------------------------------------------------------------------------


def a_review(text: str, review_id: str = "a" * 16) -> Review:
    return Review(
        review_id=review_id,
        source=Source.PLAY_STORE,
        rating=2,
        text=text,
        submitted_on="2026-10-07",  # type: ignore[arg-type]
        iso_week="2026-W41",
        scrubbed=False,
    )


def test_scrub_reviews_marks_every_review_scrubbed() -> None:
    reviews = [
        a_review("Withdrawal is slow.", "1" * 16),
        a_review("Mail me at notareal@example.com.", "2" * 16),
    ]
    scrubbed, report = scrub_reviews(reviews)

    assert len(scrubbed) == 2
    assert all(review.scrubbed for review in scrubbed)
    assert report.reviews_scrubbed == 1
    assert report.hits_by_rule == {"email": 1}


def test_scrub_reviews_drops_a_review_that_is_entirely_pii() -> None:
    """Nothing quotable remains, so it is dropped rather than kept as bare placeholders."""
    reviews = [a_review("notareal@example.com", "1" * 16)]
    scrubbed, report = scrub_reviews(reviews)

    assert scrubbed == []
    assert report.reviews_dropped == 1


def test_scrub_reviews_keeps_reviews_with_partial_redaction() -> None:
    reviews = [a_review("Withdrawal stuck, mail notareal@example.com now.", "1" * 16)]
    scrubbed, _ = scrub_reviews(reviews)

    assert len(scrubbed) == 1
    assert "[email]" in scrubbed[0].text


def test_scrub_report_summary_contains_no_review_text() -> None:
    """The ledger must never become a PII sink (architecture.md §16)."""
    reviews = [a_review("Mail me at notareal@example.com.", "1" * 16)]
    _, report = scrub_reviews(reviews)

    rendered = str(report.summary())
    assert "notareal@example.com" in reviews[0].text  # the text really did contain it
    assert "notareal" not in rendered
    assert report.summary()["pii_hits"] == 1


def test_empty_corpus_scrubs_to_empty() -> None:
    scrubbed, report = scrub_reviews([])
    assert scrubbed == []
    assert report == ScrubReport(reviews_in=0)


# --------------------------------------------------------------------------------------
# Label contract — the quote candidate filter in P3 depends on it
# --------------------------------------------------------------------------------------


def test_every_label_is_bracketed() -> None:
    """P3-T4 drops quote candidates containing a placeholder, matched by this shape."""
    for label in LABELS:
        assert label.startswith("[") and label.endswith("]")
