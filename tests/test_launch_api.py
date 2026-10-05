import unittest

from launch_disclosure.api import route
from launch_disclosure.service import DisclosureService
from launch_disclosure.storage import Database


class LaunchApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DisclosureService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["audit_valid"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/actors", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_register_actor_and_replay(self):
        route(self.service, "POST", "/actors",
              {"request_id": "r0", "new_actor_id": "admin", "display_name": "管理员",
               "role": "admin", "organization": "首发企业"},
              {"X-Actor-Id": "bootstrap"})
        body = {"request_id": "r1", "new_actor_id": "rd", "display_name": "研发",
                "role": "research", "organization": "首发企业"}
        status, payload = route(self.service, "POST", "/actors", body,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/actors", body,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_access_report_requires_actor_role(self):
        route(self.service, "POST", "/actors",
              {"request_id": "r1", "new_actor_id": "admin", "display_name": "管理员",
               "role": "admin", "organization": "首发企业"},
              {"X-Actor-Id": "bootstrap"})
        status, payload = route(self.service, "GET", "/access-report", None,
                                {"X-Actor-Id": "nobody"})
        self.assertEqual(404, status)
        status, payload = route(self.service, "GET", "/access-report", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])


if __name__ == "__main__":
    unittest.main()
