import unittest

from agentmailor.intelligence import qualification_decision, review_email, safe_personalization


class OutreachIntelligenceTests(unittest.TestCase):
    def test_rejects_fake_urgency(self):
        result = review_email(
            "URGENT: act now",
            "This revolutionary system will transform your business overnight!",
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.reasons)

    def test_rejects_observation_without_evidence(self):
        result = review_email(
            "Question about your website",
            "I noticed your website is missing online booking. Could I help?",
        )
        self.assertFalse(result.passed)
        self.assertIn("specific observation has no recorded evidence", result.reasons)

    def test_allows_short_plain_message_with_no_unverified_claim(self):
        result = review_email(
            "Question about appointment calls",
            "Hi there. I'm an independent developer building AI phone assistants. "
            "Would handling routine calls during busy periods be useful to discuss?",
        )
        self.assertTrue(result.passed)

    def test_suppression_is_hard_gate(self):
        allowed, reasons = qualification_decision(
            score=99, threshold=5, evidence_confidence=0.99, suppressed=True
        )
        self.assertFalse(allowed)
        self.assertIn("recipient is suppressed", reasons)

    def test_evidence_snippets_are_cleaned_and_deduplicated(self):
        result = safe_personalization(["  Booking page  ", "Booking page", "", "Phone contact"])
        self.assertEqual(result, ["Booking page", "Phone contact"])


if __name__ == "__main__":
    unittest.main()
