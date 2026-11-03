"""Business opportunity research and scoring for AgentMailor.

Scores are transparent heuristics, not predictive probabilities. The module
only rewards observed evidence and penalizes unknown/weak evidence.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ResearchReport:
    business_name: str
    category: str
    website_status: str
    website_signals: dict[str, bool]
    observed_facts: list[str]
    unknowns: list[str]
    evidence_confidence: float
    opportunity_score: int
    recommended_angle: str
    rationale: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_research_report(*, business_name: str, category: str, website_status: str,
                          signals: dict[str, bool], hours_fact: str = "",
                          review_count: int = 0, category_fit: int = 0) -> ResearchReport:
    """Create a compact report from fetched/verified signals; avoid claims about inferred pain."""
    facts: list[str] = []
    unknowns: list[str] = []
    rationale: list[str] = []
    score = max(0, min(25, int(category_fit)))
    confidence = 0.25  # category and business listing are preliminary evidence

    if website_status == "ok":
        confidence += 0.35
        if signals.get("tel"):
            facts.append("Website has a telephone link.")
            score += 8
            rationale.append("Phone contact is visibly offered on the website.")
        if signals.get("booking"):
            facts.append("Website appears to include online booking or appointment-related content.")
            score -= 8
            rationale.append("Existing booking functionality may reduce call-agent fit.")
        else:
            unknowns.append("No booking marker detected; this does not prove booking is unavailable.")
        if signals.get("chat"):
            facts.append("Website appears to include a chat/contact widget.")
            score -= 5
        else:
            unknowns.append("No chat marker detected; this does not prove customers cannot contact them online.")
        if signals.get("form"):
            facts.append("Website contains a form.")
            score -= 3
    elif website_status == "none":
        facts.append("No website URL was supplied in the business listing.")
        confidence += 0.15
        score += 5
        rationale.append("Website URL absent from listing; site absence itself is not verified.")
    else:
        unknowns.append("Website could not be fetched; website capabilities are unknown.")
        score -= 8

    if hours_fact:
        facts.append(hours_fact)
        confidence += 0.10
        score += 5
        rationale.append("Opening-hours data contains a potentially relevant timing signal.")

    if review_count >= 30:
        facts.append(f"Business listing displays {review_count} reviews.")
        confidence += 0.05
        # Reviews indicate public activity, not missed calls or phone volume.
        score += 2
        rationale.append("Review count is used only as a weak activity signal.")

    confidence = round(min(0.85, max(0.0, confidence)), 2)
    score = max(0, min(100, score))
    if hours_fact:
        angle = "after_hours"
    elif website_status == "none":
        angle = "no_site"
    elif website_status == "ok" and signals.get("tel") and not signals.get("booking"):
        angle = "phone_contact"
    elif website_status == "ok" and signals.get("booking"):
        angle = "existing_booking"
    else:
        angle = "general"

    return ResearchReport(business_name, category, website_status, dict(signals),
                         facts, unknowns, confidence, score, angle, rationale)


def opportunity_score(*, category_weight: int, research: ResearchReport,
                      phone_present: bool, email_present: bool,
                      email_verified: bool, suppressed: bool = False,
                      already_contacted: bool = False) -> tuple[int, list[str]]:
    """Score fit separately from mandatory eligibility/safety gates."""
    score = max(0, min(100, int(category_weight) * 4))
    score += research.opportunity_score
    reasons = list(research.rationale)
    if phone_present:
        score += 8
        reasons.append("A phone number is available for the product's core workflow.")
    else:
        score -= 25
        reasons.append("No phone number: core product fit is weak.")
    if email_present and email_verified:
        score += 5
    elif not email_present:
        reasons.append("No email address; don't invent or guess one.")
    if research.evidence_confidence < 0.45:
        score -= 15
        reasons.append("Evidence confidence is low.")
    if suppressed:
        return 0, ["Recipient is suppressed; hard stop."]
    if already_contacted:
        return 0, ["Recipient/business already contacted; hard stop."]
    return max(0, min(100, score)), reasons
