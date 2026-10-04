import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.clock import FixedClock
from launch_disclosure import LaunchDatabase, LaunchService
from launch_disclosure.api import route

T0 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


VERSION_BODY = {
    "site_id": "s1",
    "external_key": "flagship",
    "contributors": [{"party": "首发企业", "role": "lead", "share": 100}],
    "evidence": [{"evidence_id": "ev-1", "kind": "test_report", "reference": "sha256:aaa",
                  "description": "测试报告"}],
    "jurisdictions": ["CN"],
    "publish_at": at(30 * 24),
    "prerequisites": [],
    "confidentiality": [{"commitment_id": "nda-media", "party": "媒体", "scope": "预览资料",
                         "terms": "embargo"}],
    "fields": {"overview": {"value": "概览", "sensitivity": "public"}},
}


class LaunchApiTest(unittest.TestCase):
    def setUp(self):
        self.database = LaunchDatabase()
        self.service = LaunchService(self.database, FixedClock(T0))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="首发企业")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="rd", actor_id="a1", new_actor_id="rd1",
                                    display_name="研发", role="researcher", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="展位", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def test_health_delegates_to_foundation(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_launch_route_returns_404(self):
        status, payload = route(self.service, "GET", "/launch/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_register_version_and_replay(self):
        body = {"request_id": "v1", **VERSION_BODY}
        status, payload = route(self.service, "POST", "/launch/versions", body,
                                {"X-Actor-Id": "rd1"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/launch/versions", body,
                                {"X-Actor-Id": "rd1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_role_denied_maps_to_403(self):
        status, payload = route(self.service, "POST", "/launch/versions",
                                {"request_id": "v1", **VERSION_BODY}, {"X-Actor-Id": "au1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_invalid_body_maps_to_400(self):
        status, payload = route(self.service, "POST", "/launch/versions", {"request_id": "v1"},
                                {"X-Actor-Id": "rd1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_access_events_require_auditor(self):
        status, payload = route(self.service, "GET", "/launch/access-events", None,
                                {"X-Actor-Id": "rd1"})
        self.assertEqual(403, status)
        status, payload = route(self.service, "GET", "/launch/access-events", None,
                                {"X-Actor-Id": "au1"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_maintenance_endpoint(self):
        status, payload = route(self.service, "POST", "/launch/maintenance", {},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertEqual({"expired_credentials": 0, "escalated_verifications": 0}, payload)


if __name__ == "__main__":
    unittest.main()
