"""Evidence-first outreach intelligence for AgentMailor.

This module intentionally favors skipping a lead over inventing a reason to contact it.
It is deterministic, dependency-light, and safe to use before an email is sent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable


# These phrases are commonly used in manipulative or fabricated cold outreach.
# This is a quality guard, not a claim that any one word automatically causes spam.
HYPE_PATTERNS = [
    (r"\b(?:guaranteed?|revolutionary|game[- ]?changing|skyrocket|explode your|transform your business overnight)\b", "hype or guarantee"),
    (r"\b(?:you are losing|you’re losing|you've lost|you have lost)\s+(?:\$|\d|thousands?|customers?)", "unsupported loss claim"),
    (r"\b(?:urgent|act now|limited time|last chance|don't miss out|exclusive deal)\b", "false urgency"),
    (r"\b(?:we noticed you are missing|we found that you are losing|your business is failing)\b", "unsupported business diagnosis"),
    (r"\b(?:dear sir|dear madam|to whom it may concern)\b", "generic/formal salutation"),
    (r"\b(?:just checking in|bumping this|circling back|touching base)\b", "low-value follow-up cliché"),
]
URL_RE = re.compile(r"https?://|www\.", re.I)
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]|\{[^}]+\}|<[^>]+>")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


@dataclass
class EmailReview:
    passed: bool
    score: int
    reasons: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def review_email(subject: str, body: str, *, evidence: Iterable[str] = (),
                verified_claims: Iterable[str] = (), min_words: int = 25,
                max_words: int = 150) -> EmailReview:
    """Review generated copy for unsupported claims, spammy tone, and mechanical issues.

    Evidence strings are facts collected from the business's public site/listing.
    The function does not assume that a claim is true merely because it appears
    in generated copy. If evidence is absent, specific observation language is
    treated as a risk and requires human review.
    """
    subject, body = normalize_text(subject), normalize_text(body)
    text = f"{subject}\n{body}"
    evidence_l = [normalize_text(x).lower() for x in evidence if normalize_text(x)]
    verified_l = [normalize_text(x).lower() for x in verified_claims if normalize_text(x)]
    reasons: list[str] = []
    warnings: list[str] = []

    if not subject:
        reasons.append("missing subject")
    if len(subject) > 75:
        reasons.append("subject too long")
    if subject.endswith(("!", "!!")) or re.search(r"[!?]{2,}", subject):
        reasons.append("sensational subject punctuation")
    if re.search(r"\b(?:re:|fwd:|fw:)\b", subject, re.I):
        reasons.append("fake reply/forward subject")

    words = re.findall(r"\b[\w’'-]+\b", body)
    if len(words) < min_words:
        warnings.append(f"short email ({len(words)} words)")
    if len(words) > max_words:
        reasons.append(f"email too long ({len(words)} words)")
    if PLACEHOLDER_RE.search(text):
        reasons.append("unfilled placeholder")
    if URL_RE.search(text):
        warnings.append("contains a link; ensure it is expected and safe")
    if body.count("!") > 1:
        reasons.append("excessive exclamation marks")
    if body.count("?") > 3:
        warnings.append("too many questions")
    if re.search(r"\b(?:free|guarantee|act now|limited time|click here|buy now|risk[- ]free)\b", text, re.I):
        warnings.append("promotional/spam-associated wording")

    for pattern, label in HYPE_PATTERNS:
        if re.search(pattern, text, re.I):
            reasons.append(label)

    # Observation phrases need support. A generic conditional hypothesis is fine;
    # a definitive "I noticed..." claim must match provided evidence or be verified.
    observation_claim = re.search(
        r"\b(?:I noticed|I saw|I found|your website (?:doesn't|does not|has no|is missing)|"
        r"your listing shows|you are (?:closed|missing|not offering))\b",
        body, re.I,
    )
    if observation_claim and not evidence_l and not verified_l:
        reasons.append("specific observation has no recorded evidence")
    elif observation_claim:
        claim_text = body.lower()
        if not any(ev in claim_text for ev in evidence_l + verified_l):
            warnings.append("observation wording may not match stored evidence")

    # Do not let the generated email invent measurable outcomes.
    numeric_claims = re.findall(
        r"(?:\b\d+(?:\.\d+)?\s?%|\b\d{2,}\s?(?:customers?|calls?|appointments?|leads?|dollars?|rupees?))",
        body, re.I,
    )
    if numeric_claims:
        # Numbers grounded in stored business evidence are allowed; proof/pricing
        # claims still require explicit owner-provided verification.
        if not any(claim.lower() in text.lower() for claim in verified_l + evidence_l):
            reasons.append("unverified numeric claim")

    # A natural cold email should have one clear ask, not a sequence of asks.
    cta_count = len(re.findall(r"\b(?:reply|book|schedule|call|meet|demo)\b", body, re.I))
    if cta_count > 5:
        warnings.append("multiple calls to action")

    score = max(0, 100 - 25 * len(reasons) - 5 * len(warnings))
    return EmailReview(not reasons, score, reasons, warnings)


def safe_personalization(evidence: Iterable[str], max_items: int = 3) -> list[str]:
    """Return short, non-empty evidence snippets without fabricating or embellishing."""
    cleaned = []
    for item in evidence:
        item = normalize_text(item)
        if not item or len(item) > 300:
            continue
        if item.lower() not in {x.lower() for x in cleaned}:
            cleaned.append(item)
        if len(cleaned) >= max_items:
            break
    return cleaned


def qualification_decision(*, score: float, threshold: float, evidence_confidence: float,
                          evidence_min: float = 0.55, email_valid: bool = True,
                          suppressed: bool = False, already_contacted: bool = False) -> tuple[bool, list[str]]:
    """Hard safety gates first; score alone cannot override them."""
    reasons = []
    if suppressed:
        reasons.append("recipient is suppressed")
    if already_contacted:
        reasons.append("recipient/business already contacted")
    if not email_valid:
        reasons.append("email address is not valid/verified")
    if evidence_confidence < evidence_min:
        reasons.append("insufficient evidence confidence")
    if score < threshold:
        reasons.append("opportunity score below threshold")
    return not reasons, reasons


# Reply intent classification. Opt-out signals take precedence over sales intent.
REPLY_PATTERNS = {
    "optout": r"\b(?:unsubscribe|remove me|stop emailing|stop contacting|do not email|don't email|opt out)\b",
    "not_interested": r"\b(?:not interested|no thanks|not for us|we're not looking|we are not looking|no need)\b",
    "wrong_person": r"\b(?:wrong person|not the right person|contact .* instead|no longer responsible)\b",
    "out_of_office": r"\b(?:out of office|away from the office|on leave|returning on)\b",
    "pricing": r"\b(?:price|pricing|cost|how much|quote|rates|budget)\b",
    "demo": r"\b(?:demo|demonstration|try it|hear it|see it in action)\b",
    "interested": r"\b(?:interested|tell me more|sounds good|let's discuss|lets discuss|book a call|schedule a call)\b",
}


def classify_reply(subject: str, body: str) -> dict[str, Any]:
    """Classify a reply conservatively; opt-out wins over all positive intent."""
    text = normalize_text(f"{subject} {body}").lower()
    for label in ("optout", "not_interested", "wrong_person", "out_of_office",
                  "pricing", "demo", "interested"):
        if re.search(REPLY_PATTERNS[label], text, re.I):
            return {"intent": label, "confidence": 0.85, "needs_human": label in {"pricing", "demo", "interested", "wrong_person"}}
    return {"intent": "uncertain", "confidence": 0.35, "needs_human": True}


def evidence_confidence(*, website_checked: bool, source_verified: bool,
                        direct_observation_count: int, data_age_days: int | None = None) -> float:
    """Transparent confidence heuristic. It is not an ML probability."""
    score = 0.0
    score += 0.30 if source_verified else 0.0
    score += 0.30 if website_checked else 0.0
    score += min(max(direct_observation_count, 0), 3) * 0.10
    if data_age_days is not None:
        if data_age_days > 180:
            score -= 0.20
        elif data_age_days > 90:
            score -= 0.10
    return round(min(1.0, max(0.0, score)), 2)
