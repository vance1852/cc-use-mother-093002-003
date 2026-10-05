import unittest
from datetime import datetime, timezone

from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from launch_disclosure.clock import MutableClock
from launch_disclosure.service import DisclosureService
from launch_disclosure.storage import Database


class DisclosureServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = DisclosureService(self.database, self.clock)
        self.service.register_actor(request_id="a0", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization="首发企业")
        for request_id, actor_id, role in (("a1", "rd", "research"), ("a2", "law", "legal"),
                                           ("a3", "pr", "media"), ("a4", "aud", "auditor")):
            self.service.register_actor(request_id=request_id, actor_id="admin", new_actor_id=actor_id,
                                        display_name=actor_id, role=role, organization="首发企业")
        self.service.register_audience(request_id="u1", actor_id="pr", audience_id="inv",
                                       display_name="海外投资人", organization="境外基金",
                                       relationship="investor", commitments=["nda-1"])
        self.service.register_audience(request_id="u2", actor_id="pr", audience_id="rep",
                                       display_name="预览媒体", organization="行业媒体",
                                       relationship="competitor_affiliated", commitments=["nda-1"])
        self.service.register_achievement(request_id="ach", actor_id="rd",
                                          achievement_id="ach", title="新方案")
        self._register_version()

    def tearDown(self):
        self.database.close()

    def _register_version(self, version_id="v1", version_no=1, prerequisites=None, **overrides):
        payload = {
            "contributors": [{"party": "首发企业", "share": "60%"}, {"party": "联合研发方", "share": "40%"}],
            "evidence": [{"evidence_id": "ev-1", "kind": "test_report", "hash": "abc"}],
            "jurisdictions": ["CN", "EU"],
            "publish_at": "2026-10-01T00:00:00Z",
            "prerequisites": prerequisites if prerequisites is not None
            else [{"kind": "patent", "reference": "PCT-1", "status": "cleared"}],
            "confidentiality": {"nda_required": True, "clauses": ["nda-1"]},
            "audience_rules": {"investor": ["summary", "metrics"],
                               "competitor_affiliated": ["summary"]},
            "fields": {"summary": "概要", "metrics": {"qps": 1200}, "internal": "备注"},
        }
        payload.update(overrides)
        return self.service.register_version(request_id=f"ver-{version_id}", actor_id="rd",
                                             version_id=version_id, achievement_id="ach",
                                             version_no=version_no, **payload)

    def _create_disclosure(self, disclosure_id="d1", version_id="v1", **overrides):
        snapshot = self.service.get_version(version_id).snapshot_hash
        payload = {"purpose": "launch-preview", "audience_ids": ["inv", "rep"],
                   "seat_limit": 2, "preview_ends_at": "2026-09-30T08:00:00Z"}
        payload.update(overrides)
        return self.service.create_disclosure(request_id=f"disc-{disclosure_id}", actor_id="rd",
                                              disclosure_id=disclosure_id, version_id=version_id,
                                              snapshot_hash=snapshot, **payload)

    def _to_preview(self, disclosure_id="d1", **create_overrides):
        self._create_disclosure(disclosure_id, **create_overrides)
        self.service.submit_disclosure(request_id=f"{disclosure_id}-sub", actor_id="rd",
                                       disclosure_id=disclosure_id)
        self.service.verify_disclosure(request_id=f"{disclosure_id}-ver", actor_id="law",
                                       disclosure_id=disclosure_id)
        self.service.approve_disclosure(request_id=f"{disclosure_id}-ap1", actor_id="law",
                                        disclosure_id=disclosure_id)
        self.service.approve_disclosure(request_id=f"{disclosure_id}-ap2", actor_id="pr",
                                        disclosure_id=disclosure_id)
        self.service.register_actor(request_id=f"{disclosure_id}-rd2", actor_id="admin",
                                    new_actor_id="rd2", display_name="研发二", role="research",
                                    organization="首发企业")
        return self.service.approve_disclosure(request_id=f"{disclosure_id}-ap3", actor_id="rd2",
                                               disclosure_id=disclosure_id)

    def _issue_and_download(self, disclosure_id="d1", credential_id="c1", request_id="dl1",
                            audience_id="inv", seat="seat-1", purpose="launch-preview",
                            expires_at="2026-09-29T08:00:00Z"):
        self.service.issue_credential(request_id=f"iss-{credential_id}", actor_id="pr",
                                      credential_id=credential_id, disclosure_id=disclosure_id,
                                      audience_id=audience_id, seat=seat, purpose=purpose,
                                      expires_at=expires_at)
        return self.service.download(request_id=request_id, credential_id=credential_id,
                                     purpose=purpose)

    # 版本与快照 ----------------------------------------------------------

    def test_version_snapshot_is_stable_and_required(self):
        version = self.service.get_version("v1")
        self.assertEqual(64, len(version.snapshot_hash))
        with self.assertRaises(ConflictError):
            self.service.create_disclosure(request_id="bad-snap", actor_id="rd", disclosure_id="dX",
                                           version_id="v1", snapshot_hash="0" * 64,
                                           purpose="p", audience_ids=["inv"], seat_limit=1,
                                           preview_ends_at="2026-09-30T08:00:00Z")

    def test_version_validation_rejects_unknown_rule_field(self):
        with self.assertRaises(ValidationError):
            self._register_version(version_id="v9", version_no=9,
                                   audience_rules={"investor": ["missing"]})

    # 流转 ----------------------------------------------------------------

    def test_full_flow_reaches_preview(self):
        receipt = self._to_preview()
        self.assertEqual("preview", self.service.get_disclosure("d1").status)
        self.assertFalse(receipt.replayed)

    def test_creator_cannot_approve_and_roles_cannot_substitute(self):
        self._create_disclosure()
        self.service.submit_disclosure(request_id="sub", actor_id="rd", disclosure_id="d1")
        self.service.verify_disclosure(request_id="ver", actor_id="law", disclosure_id="d1")
        with self.assertRaises(PermissionDenied):
            self.service.approve_disclosure(request_id="ap-self", actor_id="rd", disclosure_id="d1")
        with self.assertRaises(PermissionDenied):
            self.service.approve_disclosure(request_id="ap-aud", actor_id="aud", disclosure_id="d1")
        self.service.approve_disclosure(request_id="ap-law", actor_id="law", disclosure_id="d1")
        with self.assertRaises(ConflictError):
            self.service.approve_disclosure(request_id="ap-law-2", actor_id="law", disclosure_id="d1")

    def test_approve_before_verify_is_rejected(self):
        self._create_disclosure()
        with self.assertRaises(ConflictError):
            self.service.approve_disclosure(request_id="ap-early", actor_id="law", disclosure_id="d1")

    def test_verify_rejects_uncleared_prerequisites(self):
        self._register_version(
            version_id="v-blocked", version_no=7,
            prerequisites=[{"kind": "patent", "reference": "PCT-9", "status": "blocking"}])
        self._create_disclosure(disclosure_id="d-b", version_id="v-blocked")
        self.service.submit_disclosure(request_id="d-b-sub", actor_id="rd", disclosure_id="d-b")
        with self.assertRaises(ConflictError):
            self.service.verify_disclosure(request_id="d-b-ver", actor_id="law", disclosure_id="d-b")

    def test_only_one_active_disclosure_per_version(self):
        self._create_disclosure(disclosure_id="d1")
        with self.assertRaises(ConflictError):
            self._create_disclosure(disclosure_id="d2")

    def test_publish_requires_publish_time(self):
        self._to_preview()
        with self.assertRaises(ConflictError):
            self.service.publish_disclosure(request_id="pub-early", actor_id="pr", disclosure_id="d1")
        self.clock.advance(days=6)
        self.service.publish_disclosure(request_id="pub", actor_id="pr", disclosure_id="d1")
        self.assertEqual("published", self.service.get_disclosure("d1").status)

    def test_withdraw_revokes_credentials(self):
        self._to_preview()
        self.service.issue_credential(request_id="iss-c1", actor_id="pr", credential_id="c1",
                                      disclosure_id="d1", audience_id="inv", seat="seat-1",
                                      purpose="launch-preview", expires_at="2026-09-29T08:00:00Z")
        self.service.withdraw_disclosure(request_id="wd", actor_id="law", disclosure_id="d1",
                                         reason="权利异议")
        with self.assertRaises(PermissionDenied):
            self.service.download(request_id="dl-after-wd", credential_id="c1",
                                  purpose="launch-preview")

    def test_correction_requires_newer_version(self):
        self._to_preview()
        self.clock.advance(days=6)
        self.service.publish_disclosure(request_id="pub", actor_id="pr", disclosure_id="d1")
        with self.assertRaises(NotFoundError):
            self.service.correct_disclosure(request_id="cor-x", actor_id="law", disclosure_id="d1",
                                            new_version_id="v-missing", reason="更正")
        self._register_version(version_id="v2", version_no=2)
        self.service.correct_disclosure(request_id="cor", actor_id="law", disclosure_id="d1",
                                        new_version_id="v2", reason="指标口径更正")
        self.assertEqual("corrected", self.service.get_disclosure("d1").status)
        receipt = self._create_disclosure(disclosure_id="d2", version_id="v2",
                                          preview_ends_at="2026-10-10T00:00:00Z")
        self.assertFalse(receipt.replayed)

    # 凭证与下载 ----------------------------------------------------------

    def test_download_returns_scoped_fields(self):
        self._to_preview()
        view = self._issue_and_download()
        self.assertEqual({"summary", "metrics"}, set(view.fields))
        self.assertFalse(view.replayed)
        self.assertEqual("v1", view.version_id)

    def test_download_replay_does_not_create_second_record(self):
        self._to_preview()
        first = self._issue_and_download()
        second = self.service.download(request_id="dl1", credential_id="c1",
                                       purpose="launch-preview")
        self.assertTrue(second.replayed)
        self.assertEqual(first.access_id, second.access_id)
        report = self.service.access_report(actor_id="aud", audience_id="inv")
        self.assertEqual(1, len(report))
        with self.assertRaises(ConflictError):
            self.service.download(request_id="dl1", credential_id="c1", purpose="other-purpose")

    def test_credential_purpose_must_match(self):
        self._to_preview()
        with self.assertRaises(PermissionDenied):
            self.service.issue_credential(request_id="iss-bad", actor_id="pr", credential_id="c-bad",
                                          disclosure_id="d1", audience_id="inv", seat="seat-1",
                                          purpose="other", expires_at="2026-09-29T08:00:00Z")

    def test_credential_requires_confidentiality_commitment(self):
        self.service.register_audience(request_id="u3", actor_id="pr", audience_id="bare",
                                       display_name="无承诺者", organization="外部",
                                       relationship="investor", commitments=[])
        self._to_preview(audience_ids=["inv", "rep", "bare"])
        with self.assertRaises(PermissionDenied):
            self.service.issue_credential(request_id="iss-bare", actor_id="pr", credential_id="c-bare",
                                          disclosure_id="d1", audience_id="bare", seat="seat-1",
                                          purpose="launch-preview",
                                          expires_at="2026-09-29T08:00:00Z")

    def test_seat_limit_is_enforced(self):
        self._to_preview()
        self.service.issue_credential(request_id="iss-1", actor_id="pr", credential_id="c1",
                                      disclosure_id="d1", audience_id="inv", seat="seat-1",
                                      purpose="launch-preview", expires_at="2026-09-29T08:00:00Z")
        self.service.issue_credential(request_id="iss-2", actor_id="pr", credential_id="c2",
                                      disclosure_id="d1", audience_id="rep", seat="seat-2",
                                      purpose="launch-preview", expires_at="2026-09-29T08:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.issue_credential(request_id="iss-3", actor_id="pr", credential_id="c3",
                                          disclosure_id="d1", audience_id="rep", seat="seat-3",
                                          purpose="launch-preview",
                                          expires_at="2026-09-29T08:00:00Z")

    def test_expired_credential_is_rejected(self):
        self._to_preview()
        self._issue_and_download()
        self.clock.advance(days=5)
        with self.assertRaises(PermissionDenied):
            self.service.download(request_id="dl-late", credential_id="c1",
                                  purpose="launch-preview")

    def test_preview_window_is_enforced(self):
        self._to_preview()
        self.service.issue_credential(request_id="iss-c9", actor_id="pr", credential_id="c9",
                                      disclosure_id="d1", audience_id="inv", seat="seat-9",
                                      purpose="launch-preview", expires_at="2026-10-09T08:00:00Z")
        self.clock.advance(days=6)
        with self.assertRaises(ConflictError):
            self.service.download(request_id="dl-window", credential_id="c9",
                                  purpose="launch-preview")

    # 冻结 ----------------------------------------------------------------

    def test_freeze_filters_only_affected_fields_and_keeps_history(self):
        self._to_preview()
        first = self._issue_and_download()
        self.assertIn("metrics", first.fields)
        self.service.raise_freeze(request_id="fz", actor_id="law", freeze_id="fz1",
                                  version_id="v1", fields=["metrics"], relationships=["investor"],
                                  reason="成果归属异议")
        self.clock.advance(hours=1)
        second = self.service.download(request_id="dl2", credential_id="c1",
                                       purpose="launch-preview")
        self.assertEqual({"summary"}, set(second.fields))
        early = self.service.access_report(actor_id="aud", audience_id="inv",
                                           at="2026-09-25T08:30:00Z")
        self.assertEqual(1, len(early))
        self.assertIn("metrics", early[0]["fields"])
        full = self.service.access_report(actor_id="aud", audience_id="inv")
        self.assertEqual(2, len(full))
        self.service.lift_freeze(request_id="fz-lift", actor_id="law", freeze_id="fz1")
        third = self.service.download(request_id="dl3", credential_id="c1",
                                      purpose="launch-preview")
        self.assertIn("metrics", third.fields)

    def test_freeze_validates_fields(self):
        with self.assertRaises(ValidationError):
            self.service.raise_freeze(request_id="fz-bad", actor_id="law", freeze_id="fz-bad",
                                      version_id="v1", fields=["nope"], relationships=[],
                                      reason="x")

    # 恢复与审计 ----------------------------------------------------------

    def test_recovery_expires_credentials_and_flags_overdue(self):
        self._to_preview()
        self._issue_and_download()
        self._register_version(version_id="v2", version_no=2)
        self._create_disclosure(disclosure_id="d2", version_id="v2")
        self.service.submit_disclosure(request_id="d2-sub", actor_id="rd", disclosure_id="d2",
                                       verify_due_at="2026-09-26T08:00:00Z")
        self.clock.advance(days=5)
        receipt = self.service.recover_expired(request_id="sweep", actor_id="admin")
        self.assertFalse(receipt.replayed)
        self.assertTrue(self.service.get_disclosure("d2").verification_overdue)
        with self.assertRaises(PermissionDenied):
            self.service.download(request_id="dl-expired", credential_id="c1",
                                  purpose="launch-preview")
        replay = self.service.recover_expired(request_id="sweep", actor_id="admin")
        self.assertTrue(replay.replayed)

    def test_access_report_answers_who_saw_what_and_basis(self):
        self._to_preview()
        self._issue_and_download()
        report = self.service.access_report(actor_id="aud", audience_id="inv")
        self.assertEqual(1, len(report))
        entry = report[0]
        self.assertEqual("v1", entry["version_id"])
        self.assertEqual({"summary", "metrics"}, set(entry["fields"]))
        roles = {item["role"] for item in entry["basis"]["approvals"]}
        self.assertEqual({"research", "legal", "media"}, roles)
        self.assertEqual(entry["snapshot_hash"], entry["basis"]["snapshot_hash"])
        with self.assertRaises(PermissionDenied):
            self.service.access_report(actor_id="rd")

    def test_audit_chain_is_valid(self):
        self._to_preview()
        self._issue_and_download()
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
