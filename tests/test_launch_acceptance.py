import unittest

from launch_disclosure.acceptance import run


class LaunchAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(3, result["accesses"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["replay_replayed"])
        self.assertTrue(result["investor_blocked_after_freeze"])
        self.assertEqual(["architecture", "overview", "roadmap"], result["media_fields_after_freeze"])
        self.assertEqual(["benchmark_summary", "core_metric", "financial_projection", "overview"],
                         result["preserved_fields"])
        self.assertEqual(4, result["approvals_on_record"])
        self.assertEqual(1, result["expired_credentials"])
        self.assertEqual(1, result["escalated_verifications"])
        self.assertEqual(0, result["recovery_expired"])
        self.assertEqual(0, result["recovery_escalated"])


if __name__ == "__main__":
    unittest.main()
