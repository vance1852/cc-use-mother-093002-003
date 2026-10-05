import unittest

from launch_disclosure.acceptance import run


class LaunchAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["replay_same_access"])
        self.assertEqual(["metrics", "summary"], result["first_fields"])
        self.assertEqual(["summary"], result["second_fields"])
        self.assertEqual(1, result["early_report_records"])
        self.assertEqual(2, result["full_report_records"])
        self.assertTrue(result["expired_download_blocked"])
        self.assertEqual("published", result["disclosure_status"])
        self.assertTrue(result["d2_overdue"])


if __name__ == "__main__":
    unittest.main()
