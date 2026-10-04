import threading
import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.audit import digest
from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from launch_disclosure import LaunchDatabase, LaunchService

T0 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
PURPOSE = "数贸会首发限时预览"


def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def version_payload(**overrides):
    payload = {
        "contributors": [
            {"party": "首发企业", "role": "lead", "share": 60},
            {"party": "联合实验室", "role": "joint", "share": 40},
        ],
        "evidence": [
            {"evidence_id": "ev-test", "kind": "test_report", "reference": "sha256:aaa",
             "description": "第三方性能测试报告"},
        ],
        "jurisdictions": ["CN", "EU"],
        "publish_at": at(30 * 24),
        "prerequisites": [
            {"item": "PCT专利申请", "kind": "patent", "reference": "", "status": "pending"},
        ],
        "confidentiality": [
            {"commitment_id": "nda-media", "party": "媒体", "scope": "预览资料",
             "terms": "embargo 期内不得刊发"},
        ],
        "fields": {
            "overview": {"value": "方案概览", "sensitivity": "public"},
            "roadmap": {"value": "路线图", "sensitivity": "restricted"},
            "core_metric": {"value": "核心指标", "sensitivity": "confidential"},
        },
    }
    payload.update(overrides)
    return payload


class LaunchServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = LaunchDatabase()
        self.service = LaunchService(self.database, FixedClock(T0))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="首发企业")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, role in [
            ("rd", "rd1", "researcher"), ("legal", "lg1", "legal"), ("media", "md1", "media"),
            ("reviewer", "rv1", "reviewer"), ("auditor", "au1", "auditor"),
        ]:
            self.service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                        display_name=actor_id, role=role, organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="数贸会展位", timezone_name="Asia/Shanghai")
        self.version_id = self._register_version()
        self.audience_id = self._add_audience()

    def tearDown(self):
        self.database.close()

    def _register_version(self, request_id="version", **overrides):
        return self.service.register_version(request_id=request_id, actor_id="rd1", site_id="s1",
                                             external_key=overrides.pop("external_key", "flagship"),
                                             **version_payload(**overrides)).resource_id

    def _add_audience(self, request_id="audience", **overrides):
        params = {"version_id": self.version_id, "label": "行业记者", "organization": "行业媒体",
                  "relationship": "media", "confidentiality_ref": "nda-media",
                  "allowed_fields": ["overview", "roadmap", "core_metric"]}
        params.update(overrides)
        return self.service.add_audience(request_id=request_id, actor_id="rd1", **params).resource_id

    def _create_application(self, request_id="app"):
        return self.service.create_application(request_id=request_id, actor_id="rd1",
                                               version_id=self.version_id, purpose=PURPOSE,
                                               verify_due_at=at(48), preview_hours=72).resource_id

    def _to_preview(self, request_id="app"):
        application_id = self._create_application(request_id)
        self.service.submit_application(request_id=f"{request_id}-submit", actor_id="rd1",
                                        application_id=application_id)
        self.service.verify_application(request_id=f"{request_id}-verify", actor_id="rv1",
                                        application_id=application_id, approve=True, note="ok")
        self.service.countersign_application(request_id=f"{request_id}-sign-rd", actor_id="rd1",
                                             application_id=application_id, duty="research", note="")
        self.service.countersign_application(request_id=f"{request_id}-sign-lg", actor_id="lg1",
                                             application_id=application_id, duty="legal", note="")
        self.service.countersign_application(request_id=f"{request_id}-sign-md", actor_id="md1",
                                             application_id=application_id, duty="media", note="")
        return application_id

    def _to_published(self, request_id="app"):
        application_id = self._to_preview(request_id)
        self.service.clear_prerequisite(request_id=f"{request_id}-clear", actor_id="lg1",
                                        version_id=self.version_id, item="PCT专利申请",
                                        reference="PCT/CN2026/000001")
        self.service.clock = FixedClock(T0 + timedelta(hours=30 * 24 + 1))
        self.service.publish_application(request_id=f"{request_id}-publish", actor_id="rd1",
                                         application_id=application_id)
        return application_id

    def _issue(self, application_id, request_id="cred", **overrides):
        params = {"actor_id": "md1", "application_id": application_id, "audience_id": self.audience_id,
                  "seats": 2, "expires_at": at(40 * 24), "purpose": PURPOSE}
        params.update(overrides)
        return self.service.issue_credential(request_id=request_id, **params).resource_id

    # ------------------------------------------------------------------
    # 版本登记与受众
    # ------------------------------------------------------------------

    def test_version_registration_puts_scope_on_single_audit_chain(self):
        events = [e for e in self.service.audit_events() if e["action"] == "launch.version_registered"]
        self.assertEqual(1, len(events))
        detail = events[0]["detail"]
        for key in ("contributors", "evidence_snapshot_hash", "jurisdictions", "publish_at",
                    "prerequisites", "confidentiality", "field_scope", "payload_hash"):
            self.assertIn(key, detail)
        self.assertEqual(digest(version_payload()["evidence"]), detail["evidence_snapshot_hash"])
        self.assertEqual({"overview": "public", "roadmap": "restricted", "core_metric": "confidential"},
                         detail["field_scope"])
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_register_version_requires_researcher(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_version(request_id="bad", actor_id="lg1", site_id="s1",
                                          external_key="other", **version_payload())

    def test_audience_validation(self):
        with self.assertRaises(ValidationError):
            self._add_audience(request_id="bad-field", allowed_fields=["overview", "unknown"])
        with self.assertRaises(ValidationError):
            self._add_audience(request_id="bad-ref", confidentiality_ref="nda-missing")
        with self.assertRaises(ValidationError):
            self._add_audience(request_id="bad-rel", relationship="competitor")
        with self.assertRaises(ValidationError):
            self._add_audience(request_id="bad-empty", allowed_fields=[])

    # ------------------------------------------------------------------
    # 流转顺序与职责分离
    # ------------------------------------------------------------------

    def test_lifecycle_enforces_order(self):
        application_id = self._create_application()
        with self.assertRaises(ConflictError):
            self.service.countersign_application(request_id="early-sign", actor_id="rd1",
                                                 application_id=application_id, duty="research", note="")
        with self.assertRaises(ConflictError):
            self.service.publish_application(request_id="early-publish", actor_id="rd1",
                                             application_id=application_id)
        with self.assertRaises(ConflictError):
            self.service.verify_application(request_id="early-verify", actor_id="rv1",
                                            application_id=application_id, approve=True)

    def test_verify_requires_reviewer(self):
        application_id = self._create_application()
        self.service.submit_application(request_id="submit", actor_id="rd1", application_id=application_id)
        with self.assertRaises(PermissionDenied):
            self.service.verify_application(request_id="verify-rd", actor_id="rd1",
                                            application_id=application_id, approve=True)
        with self.assertRaises(PermissionDenied):
            self.service.verify_application(request_id="verify-lg", actor_id="lg1",
                                            application_id=application_id, approve=True)

    def test_verify_rejection_returns_to_draft(self):
        application_id = self._create_application()
        self.service.submit_application(request_id="submit", actor_id="rd1", application_id=application_id)
        self.service.verify_application(request_id="verify", actor_id="rv1",
                                        application_id=application_id, approve=False, note="证据不足")
        self.assertEqual("draft", self.service.get_application("au1", application_id)["status"])
        self.service.submit_application(request_id="resubmit", actor_id="rd1", application_id=application_id)
        self.assertEqual("verifying", self.service.get_application("au1", application_id)["status"])

    def test_countersign_roles_cannot_substitute_each_other(self):
        application_id = self._create_application()
        self.service.submit_application(request_id="submit", actor_id="rd1", application_id=application_id)
        self.service.verify_application(request_id="verify", actor_id="rv1",
                                        application_id=application_id, approve=True, note="")
        with self.assertRaises(PermissionDenied):
            self.service.countersign_application(request_id="sign-lg-as-rd", actor_id="lg1",
                                                 application_id=application_id, duty="research", note="")
        with self.assertRaises(PermissionDenied):
            self.service.countersign_application(request_id="sign-md-as-lg", actor_id="md1",
                                                 application_id=application_id, duty="legal", note="")
        with self.assertRaises(PermissionDenied):
            self.service.countersign_application(request_id="sign-admin", actor_id="a1",
                                                 application_id=application_id, duty="media", note="")
        self.service.countersign_application(request_id="sign-rd", actor_id="rd1",
                                             application_id=application_id, duty="research", note="")
        with self.assertRaises(ConflictError):
            self.service.countersign_application(request_id="sign-rd-2", actor_id="rd1",
                                                 application_id=application_id, duty="research", note="")
        self.assertEqual("countersign", self.service.get_application("au1", application_id)["status"])

    def test_three_distinct_duties_open_preview(self):
        application_id = self._to_preview()
        application = self.service.get_application("au1", application_id)
        self.assertEqual("preview", application["status"])
        self.assertEqual(3, len(application["countersigns"]))
        self.assertEqual(at(72), application["preview_ends_at"])

    def test_publish_requires_cleared_prerequisites_and_due_time(self):
        application_id = self._to_preview()
        with self.assertRaises(ConflictError):
            self.service.publish_application(request_id="publish-pending", actor_id="rd1",
                                             application_id=application_id)
        self.service.clear_prerequisite(request_id="clear", actor_id="lg1",
                                        version_id=self.version_id, item="PCT专利申请",
                                        reference="PCT/CN2026/000001")
        with self.assertRaises(ConflictError):
            self.service.publish_application(request_id="publish-early", actor_id="rd1",
                                             application_id=application_id)
        self.service.clock = FixedClock(T0 + timedelta(hours=30 * 24 + 1))
        self.service.publish_application(request_id="publish", actor_id="rd1",
                                         application_id=application_id)
        self.assertEqual("published", self.service.get_application("au1", application_id)["status"])

    def test_clear_prerequisite_requires_legal(self):
        with self.assertRaises(PermissionDenied):
            self.service.clear_prerequisite(request_id="clear-rd", actor_id="rd1",
                                            version_id=self.version_id, item="PCT专利申请")
        with self.assertRaises(NotFoundError):
            self.service.clear_prerequisite(request_id="clear-missing", actor_id="lg1",
                                            version_id=self.version_id, item="不存在的事项")
        self.service.clear_prerequisite(request_id="clear", actor_id="lg1",
                                        version_id=self.version_id, item="PCT专利申请")
        with self.assertRaises(ConflictError):
            self.service.clear_prerequisite(request_id="clear-again", actor_id="lg1",
                                            version_id=self.version_id, item="PCT专利申请")

    # ------------------------------------------------------------------
    # 凭证约束与访问
    # ------------------------------------------------------------------

    def test_credential_requires_open_application(self):
        application_id = self._create_application()
        with self.assertRaises(ConflictError):
            self._issue(application_id, request_id="cred-draft")

    def test_credential_requires_matching_purpose(self):
        application_id = self._to_preview()
        with self.assertRaises(ValidationError):
            self._issue(application_id, request_id="cred-purpose", purpose="其他用途")

    def test_access_replay_does_not_create_second_record(self):
        application_id = self._to_preview()
        credential = self._issue(application_id)
        first = self.service.record_access(request_id="access", credential_id=credential,
                                           audience_id=self.audience_id, accessor_label="记者",
                                           purpose=PURPOSE)
        replay = self.service.record_access(request_id="access", credential_id=credential,
                                            audience_id=self.audience_id, accessor_label="记者",
                                            purpose=PURPOSE)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        events = self.service.list_access_events("au1", version_id=self.version_id)
        self.assertEqual(1, len(events))
        self.assertEqual(1, self.service.get_credential("au1", credential)["seats_used"])

    def test_access_constraints(self):
        application_id = self._to_preview()
        credential = self._issue(application_id, seats=1)
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="wrong-purpose", credential_id=credential,
                                       audience_id=self.audience_id, accessor_label="记者",
                                       purpose="别的用途")
        other_audience = self._add_audience(request_id="audience-2", label="另一位记者")
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="wrong-audience", credential_id=credential,
                                       audience_id=other_audience, accessor_label="记者",
                                       purpose=PURPOSE)
        self.service.record_access(request_id="access", credential_id=credential,
                                   audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        with self.assertRaises(ConflictError):
            self.service.record_access(request_id="access-extra", credential_id=credential,
                                       audience_id=self.audience_id, accessor_label="记者",
                                       purpose=PURPOSE)

    def test_expired_credential_is_rejected(self):
        application_id = self._to_preview()
        credential = self._issue(application_id, expires_at=at(10))
        self.service.clock = FixedClock(T0 + timedelta(hours=11))
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="access", credential_id=credential,
                                       audience_id=self.audience_id, accessor_label="记者",
                                       purpose=PURPOSE)
        self.assertEqual("expired", self.service.get_credential("au1", credential)["status"])

    def test_concurrent_access_allows_single_success(self):
        application_id = self._to_preview()
        credential = self._issue(application_id, seats=1)
        outcomes = []
        lock = threading.Lock()

        def attempt(index):
            try:
                self.service.record_access(request_id=f"race-{index}", credential_id=credential,
                                           audience_id=self.audience_id, accessor_label="记者",
                                           purpose=PURPOSE)
                with lock:
                    outcomes.append("ok")
            except ConflictError:
                with lock:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict"] * 3 + ["ok"], sorted(outcomes))
        self.assertEqual(1, len(self.service.list_access_events("au1", version_id=self.version_id)))

    def test_concurrent_applications_allow_single_success(self):
        outcomes = []
        lock = threading.Lock()

        def attempt(index):
            try:
                self.service.create_application(request_id=f"app-{index}", actor_id="rd1",
                                                version_id=self.version_id, purpose=PURPOSE,
                                                verify_due_at=at(48), preview_hours=72)
                with lock:
                    outcomes.append("ok")
            except ConflictError:
                with lock:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict"] * 2 + ["ok"], sorted(outcomes))

    # ------------------------------------------------------------------
    # 冻结
    # ------------------------------------------------------------------

    def test_freeze_scopes_only_listed_fields_and_audiences(self):
        application_id = self._to_preview()
        other_audience = self._add_audience(request_id="audience-2", label="投资人", relationship="investor")
        frozen_credential = self._issue(application_id, request_id="cred-1")
        other_credential = self._issue(application_id, request_id="cred-2", audience_id=other_audience)
        self.service.record_access(request_id="access-1", credential_id=other_credential,
                                   audience_id=other_audience, accessor_label="投资人", purpose=PURPOSE)
        self.service.freeze_scope(request_id="freeze", actor_id="lg1", version_id=self.version_id,
                                  reason="rights_objection", detail="联合实验室对核心指标归属有异议",
                                  fields=["core_metric"], audience_ids=[self.audience_id])
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="access-frozen", credential_id=frozen_credential,
                                       audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        with self.assertRaises(ConflictError):
            self._issue(application_id, request_id="cred-3")
        self.service.record_access(request_id="access-2", credential_id=other_credential,
                                   audience_id=other_audience, accessor_label="投资人", purpose=PURPOSE)
        events = self.service.list_access_events("au1", version_id=self.version_id)
        self.assertEqual(2, len(events))
        self.assertEqual(["core_metric", "overview", "roadmap"], events[0]["fields"])
        self.assertEqual(["overview", "roadmap"], events[1]["fields"])

    def test_freeze_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.freeze_scope(request_id="freeze-rd", actor_id="rd1", version_id=self.version_id,
                                      reason="rights_objection", detail="x", fields=["core_metric"])
        with self.assertRaises(ValidationError):
            self.service.freeze_scope(request_id="freeze-empty", actor_id="lg1",
                                      version_id=self.version_id, reason="rights_objection", detail="x")
        with self.assertRaises(ValidationError):
            self.service.freeze_scope(request_id="freeze-unknown", actor_id="lg1",
                                      version_id=self.version_id, reason="regulatory_restriction",
                                      detail="x", fields=["unknown"])
        with self.assertRaises(ValidationError):
            self.service.freeze_scope(request_id="freeze-aud", actor_id="lg1",
                                      version_id=self.version_id, reason="rights_objection",
                                      detail="x", audience_ids=["missing"])

    def test_lift_freeze_restores_audience(self):
        application_id = self._to_preview()
        credential = self._issue(application_id)
        freeze_id = self.service.freeze_scope(
            request_id="freeze", actor_id="lg1", version_id=self.version_id,
            reason="regulatory_restriction", detail="辖区新限制",
            audience_ids=[self.audience_id]).resource_id
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="access", credential_id=credential,
                                       audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        self.service.lift_freeze(request_id="lift", actor_id="lg1", freeze_id=freeze_id)
        self.service.record_access(request_id="access-2", credential_id=credential,
                                   audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        self.assertEqual(1, len(self.service.list_access_events("au1", version_id=self.version_id)))

    # ------------------------------------------------------------------
    # 撤回与更正
    # ------------------------------------------------------------------

    def test_withdraw_revokes_credentials_and_keeps_history(self):
        application_id = self._to_preview()
        credential = self._issue(application_id)
        self.service.record_access(request_id="access", credential_id=credential,
                                   audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        self.service.withdraw_application(request_id="withdraw", actor_id="lg1",
                                          application_id=application_id, reason="首发计划调整")
        self.assertEqual("withdrawn", self.service.get_application("au1", application_id)["status"])
        self.assertEqual("revoked", self.service.get_credential("au1", credential)["status"])
        with self.assertRaises(PermissionDenied):
            self.service.record_access(request_id="access-2", credential_id=credential,
                                       audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        self.assertEqual(1, len(self.service.list_access_events("au1", version_id=self.version_id)))
        new_application = self._create_application(request_id="app-2")
        self.assertEqual("draft", self.service.get_application("au1", new_application)["status"])

    def test_correction_supersedes_version_and_revokes_credentials(self):
        application_id = self._to_published()
        credential = self._issue(application_id)
        self.service.record_access(request_id="access", credential_id=credential,
                                   audience_id=self.audience_id, accessor_label="记者", purpose=PURPOSE)
        self.service.clock = FixedClock(T0 + timedelta(hours=31 * 24))
        new_application = self.service.correct_application(
            request_id="correct", actor_id="rd1", application_id=application_id,
            verify_due_at=at(32 * 24), preview_hours=24,
            **version_payload(publish_at=at(33 * 24))).resource_id
        old = self.service.get_application("au1", application_id)
        self.assertEqual("corrected", old["status"])
        self.assertEqual("superseded", self.service.get_version("au1", self.version_id)["status"])
        self.assertEqual("revoked", self.service.get_credential("au1", credential)["status"])
        new = self.service.get_application("au1", new_application)
        self.assertEqual("draft", new["status"])
        new_version = self.service.get_version("au1", new["version_id"])
        self.assertEqual(2, new_version["revision"])
        self.assertEqual(self.version_id, new_version["supersedes_version_id"])
        with self.assertRaises(ConflictError):
            self.service.countersign_application(request_id="sign-new", actor_id="rd1",
                                                 application_id=new_application, duty="research", note="")
        self.assertEqual(1, len(self.service.list_access_events("au1", version_id=self.version_id)))

    def test_correct_requires_published(self):
        application_id = self._to_preview()
        with self.assertRaises(ConflictError):
            self.service.correct_application(request_id="correct", actor_id="rd1",
                                             application_id=application_id, verify_due_at=at(60),
                                             preview_hours=24, **version_payload(publish_at=at(70)))

    # ------------------------------------------------------------------
    # 到期回收、待核验与恢复
    # ------------------------------------------------------------------

    def test_maintenance_recycles_and_escalates_then_recovers(self):
        application_id = self._to_preview()
        credential = self._issue(application_id, expires_at=at(10))
        pending_application = self._create_pending_verification()
        self.service.clock = FixedClock(T0 + timedelta(hours=49))
        first = self.service.run_maintenance()
        self.assertEqual(1, first["expired_credentials"])
        self.assertEqual(1, first["escalated_verifications"])
        self.assertEqual("expired", self.service.get_credential("au1", credential)["status"])
        application = self.service.get_application("au1", pending_application)
        self.assertTrue(application["verification_escalated"])
        second = self.service.run_maintenance()
        self.assertEqual({"expired_credentials": 0, "escalated_verifications": 0}, second)
        recovered = LaunchService(self.database, FixedClock(T0 + timedelta(hours=49)))
        self.assertEqual({"expired_credentials": 0, "escalated_verifications": 0},
                         recovered.run_maintenance())
        actions = [e["action"] for e in self.service.audit_events()]
        self.assertEqual(1, actions.count("launch.credential_expired"))
        self.assertEqual(1, actions.count("launch.verification_overdue"))

    def _create_pending_verification(self):
        version_id = self._register_version(request_id="version-2", external_key="companion",
                                            publish_at=at(60 * 24))
        application_id = self.service.create_application(
            request_id="app-pending", actor_id="rd1", version_id=version_id, purpose=PURPOSE,
            verify_due_at=at(24), preview_hours=24).resource_id
        self.service.submit_application(request_id="submit-pending", actor_id="rd1",
                                        application_id=application_id)
        return application_id

    # ------------------------------------------------------------------
    # 审计查询
    # ------------------------------------------------------------------

    def test_auditor_can_explain_who_saw_what_and_on_which_approvals(self):
        application_id = self._to_preview()
        credential = self._issue(application_id)
        access_id = self.service.record_access(request_id="access", credential_id=credential,
                                               audience_id=self.audience_id, accessor_label="记者",
                                               purpose=PURPOSE).resource_id
        with self.assertRaises(PermissionDenied):
            self.service.list_access_events("rd1", version_id=self.version_id)
        record = self.service.explain_access("au1", access_id)
        self.assertEqual(self.version_id, record["version_id"])
        self.assertEqual(1, record["revision"])
        self.assertEqual(["core_metric", "overview", "roadmap"], record["fields"])
        duties = {item["duty"] for item in record["approvals"]}
        self.assertEqual({"verification", "research", "legal", "media"}, duties)
        self.assertEqual(self.service.get_version("au1", self.version_id)["evidence_snapshot_hash"],
                         record["evidence_snapshot_hash"])
        events = self.service.list_access_events("au1", audience_id=self.audience_id,
                                                 since=at(0), until=at(1))
        self.assertEqual(1, len(events))

    def test_credential_and_access_share_application_evidence_snapshot(self):
        application_id = self._to_preview()
        credential = self._issue(application_id)
        record = self.service.get_credential("au1", credential)
        application = self.service.get_application("au1", application_id)
        self.assertEqual(application["evidence_snapshot_hash"], record["evidence_snapshot_hash"])
        self.assertEqual(self.service.get_version("au1", self.version_id)["evidence_snapshot_hash"],
                         record["evidence_snapshot_hash"])

    def test_request_id_rejects_changed_payload(self):
        with self.assertRaises(ConflictError):
            self.service.register_version(request_id="version", actor_id="rd1", site_id="s1",
                                          external_key="changed", **version_payload())


if __name__ == "__main__":
    unittest.main()
