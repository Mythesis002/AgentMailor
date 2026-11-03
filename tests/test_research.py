import unittest

from agentmailor.research import build_research_report, opportunity_score


class ResearchScoringTests(unittest.TestCase):
    def test_existing_booking_reduces_fit(self):
        report = build_research_report(
            business_name="Example Clinic", category="clinic", website_status="ok",
            signals={"tel": True, "booking": True, "chat": True, "form": True},
            category_fit=3,
        )
        self.assertEqual(report.recommended_angle, "existing_booking")
        self.assertLess(report.opportunity_score, 20)

    def test_unknown_website_is_not_claimed_as_missing(self):
        report = build_research_report(
            business_name="Example", category="salon", website_status="unknown",
            signals={}, category_fit=3,
        )
        self.assertIn("Website could not be fetched; website capabilities are unknown.", report.unknowns)
        self.assertNotIn("No website URL was supplied in the business listing.", report.observed_facts)

    def test_no_website_listing_is_explicitly_qualified(self):
        report = build_research_report(
            business_name="Example", category="salon", website_status="none",
            signals={}, category_fit=3,
        )
        self.assertIn("No website URL was supplied in the business listing.", report.observed_facts)
        self.assertIn("site absence itself is not verified", " ".join(report.rationale))

    def test_suppressed_lead_is_hard_stopped(self):
        report = build_research_report(
            business_name="Example", category="dentist", website_status="ok",
            signals={"tel": True}, category_fit=3,
        )
        score, reasons = opportunity_score(
            category_weight=3, research=report, phone_present=True,
            email_present=True, email_verified=True, suppressed=True,
        )
        self.assertEqual(score, 0)
        self.assertIn("Recipient is suppressed; hard stop.", reasons)


if __name__ == "__main__":
    unittest.main()
