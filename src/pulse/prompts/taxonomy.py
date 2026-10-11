"""The classification prompt (P2-T2).

Built from `config/themes.toml` so the taxonomy has one source of truth: the enum
constrains the output, the config describes it, and this renders the description. If they
drifted, the prompt would describe themes the schema rejects — and the symptom would be
confusing classification failures rather than the config error it is. `config.py` guards
that with a startup check.

**This text must be byte-stable for a given config.** It is the system prefix, so any
volatile content in it (a timestamp, a corpus size, a shuffled dict) would defeat prompt
caching on providers that offer it and make runs non-comparable. A test asserts stability.

Examples are invented rather than drawn from `tests/fixtures/`, so the prompt does not
teach the model the fixture it is later evaluated on.
"""

from __future__ import annotations

from pulse.config import Settings
from pulse.models import Review, Theme

#: Per-theme worked examples. Two each: one unambiguous, one near a boundary. Boundaries
#: are where a closed taxonomy actually costs accuracy, so they get the airtime.
_EXAMPLES: dict[Theme, tuple[str, ...]] = {
    Theme.ONBOARDING_KYC: (
        '"KYC stuck in verification for nine days, no update at all." -> onboarding_kyc',
        '"Uploaded my address proof four times, rejected without a reason." -> onboarding_kyc '
        "(a rejected document is onboarding, not support)",
    ),
    Theme.ORDERS_EXECUTION: (
        '"Order placement failed three times during market open." -> orders_execution',
        '"IPO application went through but the mandate never arrived." -> orders_execution '
        "(IPO mechanics, even though a payment is involved)",
    ),
    Theme.PAYMENTS_WITHDRAWALS: (
        '"Withdrawal pending for four days with no update." -> payments_withdrawals',
        '"Money debited from my bank but never showed in the trading account." '
        "-> payments_withdrawals (money movement, not an order problem)",
    ),
    Theme.APP_RELIABILITY: (
        '"App crashes every time I open the chart." -> app_reliability',
        '"Login fails with a network error every morning." -> app_reliability '
        "(cannot get in at all, as opposed to KYC not being approved)",
    ),
    Theme.CHARGES_SUPPORT: (
        '"Charged an AMC I was never told about at signup." -> charges_support',
        '"Raised a ticket eight days ago, no human has replied." -> charges_support '
        "(support responsiveness, whatever the ticket was about)",
    ),
}

#: Disambiguation rules. The highest-value part of the prompt: a closed taxonomy fails at
#: the seams between themes, not in the middle of them.
TAXONOMY_RULES: tuple[str, ...] = (
    "Classify the reviewer's PRIMARY complaint or praise. If a review mentions several "
    "topics, pick the one it spends the most words on.",
    "A failure that happens *because* the app broke is app_reliability only if the "
    "reviewer frames it as the app breaking. 'Order failed, app froze' is "
    "orders_execution; 'app froze constantly, could not even place an order' is "
    "app_reliability.",
    "Money moving in or out of the account is payments_withdrawals. What happens to it "
    "once it is in the account is orders_execution.",
    "Not being able to log in is app_reliability. Not being allowed to trade yet is "
    "onboarding_kyc.",
    "Anything about fees, statements, reports, or the responsiveness of support is "
    "charges_support, regardless of which other theme the underlying issue touches.",
    "Positive reviews get a theme too. 'Charts are excellent' is orders_execution.",
    "Redacted spans appear as bracketed placeholders such as [email] or [phone]. Ignore "
    "them; they are removed personal data, not part of the complaint.",
    "If the review is generic ('good app', 'worst app', 'nice'), carries no topic at all, "
    "or is in a language you cannot read, return null for the theme with a low confidence "
    "rather than guessing.",
)


def taxonomy_prompt(settings: Settings) -> str:
    """The system prompt for classification. Pure and deterministic for a given config."""
    lines: list[str] = [
        "You classify user reviews of Groww, an Indian stock broking and mutual fund app, "
        "into a fixed set of themes.",
        "",
        f"There are exactly {len(Theme)} themes. You may not invent another one. "
        "Every review gets exactly one theme, or null if none genuinely fits.",
        "",
        "THEMES",
        "",
    ]

    # Iterated over the enum, not the config dict, so ordering is fixed by the enum's
    # declaration order rather than by TOML parse order.
    for theme in Theme:
        definition = settings.themes.definition(theme)
        lines.append(f"{theme.value} — {definition.label}")
        lines.append(f"  Covers: {definition.covers}")
        for example in _EXAMPLES[theme]:
            lines.append(f"  Example: {example}")
        lines.append("")

    lines.append("RULES")
    lines.append("")
    lines.extend(f"{index}. {rule}" for index, rule in enumerate(TAXONOMY_RULES, start=1))
    lines.extend(
        [
            "",
            "OUTPUT",
            "",
            "Return one entry for every review you are given, keyed by the integer index "
            "shown beside it. Include every index exactly once, in ascending order.",
            "`confidence` is your own certainty from 0.0 to 1.0. Be honest: a low "
            "confidence is more useful than a confident guess, because low-confidence "
            "labels are discarded rather than trusted.",
            "Return only the JSON object. No commentary, no markdown fences.",
        ]
    )

    return "\n".join(lines)


def render_batch(reviews: list[Review]) -> str:
    """Render one batch of reviews for classification.

    Reviews are addressed by a **1-based index within the batch**, not by `review_id`.
    A small integer is far harder for a model to corrupt than a 16-character hex hash, an
    out-of-range index is detectable where a plausible-looking wrong hash is not, and it
    costs fewer output tokens. `classify.py` maps indices back to ids locally and verifies
    that every index appears exactly once.

    Rating is included because it is real signal for the theme ("charges are low" at 5
    stars versus "charges are hidden" at 1 star), and it costs almost nothing.
    """
    lines: list[str] = []
    for index, review in enumerate(reviews, start=1):
        lines.append(f"[{index}] ({review.rating}/5) {review.text}")
    return "\n".join(lines)
