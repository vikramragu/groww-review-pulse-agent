#!/usr/bin/env python3
"""Generate the synthetic review fixture (P1-T1).

Run from the repository root:

    python scripts/make_fixture.py

**Every review here is invented.** Committing real Play Store reviews would put
third-party text — and plausibly real PII — into the repository, which is precisely what
this project promises not to do. Synthetic text also makes the scrubber tests honest: we
know the ground truth for every row, so "the scrubber found 14 emails" is checkable rather
than assumed.

All PII in the fixture is deliberately unusable: reserved example domains, the 90000 00000
phone block, and Aadhaar numbers constructed to pass the Verhoeff checksum without
belonging to anyone.

Deterministic: a fixed seed means re-running produces a byte-identical file, so the
committed fixture can be regenerated and diffed.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20261011
TARGET_WEEK = "2026-W41"
WEEKS = 12
OUTPUT = Path("tests/fixtures/reviews_sample.json")

# Review bodies by theme. Deliberately uneven in length and register, because the quote
# candidate filter in P3 selects on length and readability.
TEMPLATES: dict[str, list[str]] = {
    "onboarding_kyc": [
        "KYC has been stuck in verification for nine days now. No update, no email, nothing.",
        "Signup was smooth but my PAN verification keeps failing even though the details match.",
        "Account opening took twenty minutes, genuinely impressed with how simple it was.",
        "Uploaded my address proof four times and it keeps getting rejected without a reason.",
        "eKYC worked on the second attempt. The first one timed out at the selfie step.",
        "Why does KYC need a video call at 11pm? No slots available for a week.",
        "Been waiting two weeks for account activation. Support says 24 hours every time.",
        "Onboarding is clean and fast. Had my account running the same evening.",
    ],
    "orders_execution": [
        "Order placement failed three times during market open and the app showed no error.",
        "Charts are smooth and the watchlist is exactly what I need. Best in class.",
        "IPO application went through but the mandate never arrived. Missed the allotment.",
        "Price feed froze for ten minutes during a volatile session. Lost a good entry.",
        "Limit orders work fine, but market orders slip badly on small caps.",
        "The new order window is a big improvement, far fewer taps than before.",
        "Placed a sell order and it executed at a price I never saw on screen.",
        "Options chain loads instantly now. Whatever you changed last month, keep it.",
    ],
    "payments_withdrawals": [
        "Withdrawal has been pending for four days with no update from support.",
        "Money was debited from my bank but never showed up in the trading account.",
        "UPI deposit is instant every single time. No complaints at all here.",
        "Requested a withdrawal on Monday, still processing on Friday. This is my own money.",
        "Settlement takes longer than the T+1 the app promises. Consistently.",
        "Added funds via netbanking and it reflected in under a minute. Smooth.",
        "Failed deposit of five thousand rupees took eleven days to be refunded.",
        "Withdrawal worked fine but the timeline shown in the app was wrong by two days.",
    ],
    "app_reliability": [
        "App crashes every time I open the chart on this phone. Reinstalled twice already.",
        "Login fails with a network error even on strong wifi. Happens most mornings.",
        "OTP never arrives on the first try. Always have to request it twice.",
        "Has been rock solid for me over six months of daily use.",
        "The app went down completely during market hours on Tuesday. Unacceptable.",
        "Latest update broke the portfolio screen, it just spins forever now.",
        "Very slow to load holdings after the last upgrade. Was instant before.",
        "Biometric login stopped working after the update and there is no way to reset it.",
    ],
    "charges_support": [
        "Brokerage is transparent and genuinely low. The calculator helps a lot.",
        "Got charged an AMC I was never told about at signup. Still waiting on an explanation.",
        "Support replies with the same copy paste message no matter what you ask.",
        "Statements are hard to find and the downloaded PDF has the wrong date range.",
        "Tried to close my account and the option is hidden three menus deep.",
        "Charges are clear but the P&L report does not match my own calculations.",
        "Raised a ticket eight days ago about a wrong debit. No human has replied yet.",
        "Fee structure is the most honest I have used. No hidden surprises so far.",
    ],
}

# PII-bearing suffixes. Each exercises a specific scrubber rule; all values are fake.
PII_SUFFIXES: list[str] = [
    " Please contact me at notareal@example.com for the details.",
    " My number is +91 90000 00000 if anyone from the team wants to call.",
    " Reach me on 9000000000, I have the screenshots.",
    " My PAN is ABCDE1234F and it still says invalid.",
    # Verhoeff-valid so the aadhaar rule fires rather than the digit-run backstop.
    " Aadhaar 2994 1908 4318 was rejected twice.",
    " Transferred to account 123456789012 via SBIN0001234 and nothing arrived.",
    " My UPI is notareal@okaxis and the payment still failed.",
    " Details are at https://example.invalid/my-complaint-thread if you want them.",
    " Ping me @notarealhandle on twitter, I have posted the screenshots.",
    " My name is Not A Realperson and I have been a customer for three years.",
    " Email me at also.notreal@example.org, ticket number is attached.",
    " Call me on +91 90000 00001 any time before 6pm.",
]

# Ratings skew negative, as store reviews for financial apps generally do.
RATING_WEIGHTS = [(1, 34), (2, 18), (3, 12), (4, 14), (5, 22)]


def iso_week_monday(iso_week: str) -> date:
    year, week = iso_week.split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def build_rows() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    rows: list[dict[str, object]] = []

    last_monday = iso_week_monday(TARGET_WEEK)
    week_mondays = [last_monday - timedelta(days=7 * offset) for offset in range(WEEKS)]
    week_mondays.reverse()

    ratings = [rating for rating, weight in RATING_WEIGHTS for _ in range(weight)]
    themes = list(TEMPLATES)

    for monday in week_mondays:
        # More reviews in recent weeks, so the trailing baseline is not flat.
        recency = week_mondays.index(monday) / max(1, WEEKS - 1)
        count = rng.randint(16, 20) + int(recency * 6)

        for _ in range(count):
            theme = rng.choice(themes)
            body = rng.choice(TEMPLATES[theme])
            rating = rng.choice(ratings)

            # ~9% of rows carry PII, which is roughly what real review corpora show.
            if rng.random() < 0.09:
                body += rng.choice(PII_SUFFIXES)

            rows.append(
                {
                    "rating": rating,
                    "text": body,
                    "date": (monday + timedelta(days=rng.randint(0, 6))).isoformat(),
                }
            )

    _add_edge_cases(rows, rng, last_monday)
    rng.shuffle(rows)
    return rows


def _add_edge_cases(rows: list[dict[str, object]], rng: random.Random, last_monday: date) -> None:
    """Rows that exercise the adapter and normalizer rather than the analysis."""
    day = (last_monday + timedelta(days=2)).isoformat()

    # Exact duplicates — must collapse to one review via the content hash.
    duplicate = {
        "rating": 1,
        "text": "Withdrawal has been pending for four days with no update from support.",
        "date": day,
    }
    rows.extend([dict(duplicate), dict(duplicate), dict(duplicate)])

    # Same text and date but differing rating: still one review, because review_id
    # deliberately excludes rating.
    rows.append({**duplicate, "rating": 2})

    # Whitespace-only difference — collapse_whitespace must treat these as identical.
    rows.append(
        {
            "rating": 1,
            "text": "Withdrawal  has been pending for four days   with no update from support.",
            "date": day,
        }
    )

    # Empty and whitespace-only text: dropped at normalization.
    rows.append({"rating": 3, "text": "", "date": day})
    rows.append({"rating": 3, "text": "   ", "date": day})

    # Title-only row: the title becomes the text.
    rows.append({"rating": 2, "title": "Deposit never credited to my account", "date": day})

    # Alias columns and odd types the adapter must cope with.
    rows.append({"score": "4", "content": "Alias columns should map cleanly.", "at": day})
    rows.append(
        {
            "rating": 5.0,
            "text": "Float rating should coerce to an int.",
            "submitted_at": f"{day}T09:15:00Z",
        }
    )

    # Out-of-range ratings: clamped to 1-5.
    rows.append({"rating": 0, "text": "Zero rating should clamp up to one.", "date": day})
    rows.append({"rating": 7, "text": "Seven rating should clamp down to five.", "date": day})

    # Missing rating and unparseable date: dropped, with the reason counted.
    rows.append({"text": "No rating at all on this row.", "date": day})
    rows.append({"rating": 3, "text": "Date is nonsense.", "date": "not-a-date"})

    # Identity columns present in the export. These must never reach stage 2's output.
    rows.append(
        {
            "rating": 1,
            "text": "Identity columns in the export must not survive normalization.",
            "date": day,
            "userName": "Not A Realperson",
            "author": "notareal@example.com",
            "device_id": "fake-device-0001",
        }
    )

    # A row outside the window: filtered out by stage 2.
    rows.append(
        {
            "rating": 2,
            "text": "This review is far older than the configured window.",
            "date": (last_monday - timedelta(days=7 * 30)).isoformat(),
        }
    )

    del rng  # kept in the signature for symmetry with build_rows


def main() -> None:
    rows = build_rows()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()
