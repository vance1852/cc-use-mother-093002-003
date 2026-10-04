"""运行技术首发与受限披露服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import PermissionDenied

from .service import LaunchService
from .storage import LaunchDatabase

T0 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
PURPOSE = "数贸会首发限时预览"


def _at(hours: int) -> str:
    return (T0 + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version_payload(publish_at: str) -> dict:
    return {
        "contributors": [
            {"party": "首发企业", "role": "lead", "share": 60},
            {"party": "联合实验室", "role": "joint", "share": 40},
        ],
        "evidence": [
            {"evidence_id": "ev-test", "kind": "test_report", "reference": "sha256:aaa",
             "description": "第三方性能测试报告"},
            {"evidence_id": "ev-code", "kind": "code_attestation", "reference": "sha256:bbb",
             "description": "源代码公证"},
        ],
        "jurisdictions": ["CN", "EU", "SG"],
        "publish_at": publish_at,
        "prerequisites": [
            {"item": "PCT专利申请", "kind": "patent", "reference": "", "status": "pending"},
            {"item": "跨境数据监管备案", "kind": "regulatory", "reference": "", "status": "pending"},
        ],
        "confidentiality": [
            {"commitment_id": "nda-investors", "party": "投资人", "scope": "财务与核心指标",
             "terms": "仅限内部评估使用"},
            {"commitment_id": "nda-media", "party": "媒体", "scope": "预览资料",
             "terms": " embargo 期内不得刊发"},
        ],
        "fields": {
            "overview": {"value": "方案概览", "sensitivity": "public"},
            "architecture": {"value": "架构说明", "sensitivity": "public"},
            "roadmap": {"value": "路线图", "sensitivity": "restricted"},
            "benchmark_summary": {"value": "基准测试摘要", "sensitivity": "restricted"},
            "core_metric": {"value": "核心指标", "sensitivity": "confidential"},
            "financial_projection": {"value": "财务预测", "sensitivity": "confidential"},
        },
    }


def run() -> dict[str, object]:
    """执行一条完整的首发披露链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = LaunchDatabase(Path(directory) / "acceptance.sqlite3")
        service = LaunchService(database, FixedClock(T0))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="首发企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        for request_id, actor_id, name, role in [
            ("req-research", "rd-001", "研发负责人", "researcher"),
            ("req-legal", "legal-001", "法务负责人", "legal"),
            ("req-media", "media-001", "媒体联络人", "media"),
            ("req-reviewer", "reviewer-001", "合规核验员", "reviewer"),
            ("req-auditor", "auditor-001", "审计员", "auditor"),
        ]:
            service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                                   display_name=name, role=role, organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="admin-001", site_id="site-001",
                              organization_id="org-001", name="数贸会展位", timezone_name="Asia/Shanghai")

        version_id = service.register_version(
            request_id="req-version", actor_id="rd-001", site_id="site-001",
            external_key="flagship-analytics", **_version_payload(_at(30 * 24))).resource_id
        investor_id = service.add_audience(
            request_id="req-aud-inv", actor_id="rd-001", version_id=version_id, label="海外投资人A",
            organization="投资方", relationship="investor", confidentiality_ref="nda-investors",
            allowed_fields=["overview", "benchmark_summary", "core_metric", "financial_projection"],
        ).resource_id
        media_id = service.add_audience(
            request_id="req-aud-media", actor_id="rd-001", version_id=version_id, label="行业记者B",
            organization="行业媒体", relationship="media", confidentiality_ref="nda-media",
            allowed_fields=["overview", "architecture", "roadmap", "benchmark_summary"],
            risk_note="与竞争对手存在任职关系",
        ).resource_id

        application_id = service.create_application(
            request_id="req-app", actor_id="rd-001", version_id=version_id, purpose=PURPOSE,
            verify_due_at=_at(48), preview_hours=72).resource_id
        service.submit_application(request_id="req-submit", actor_id="rd-001", application_id=application_id)
        service.verify_application(request_id="req-verify", actor_id="reviewer-001",
                                   application_id=application_id, approve=True, note="证据齐全")
        service.countersign_application(request_id="req-sign-rd", actor_id="rd-001",
                                        application_id=application_id, duty="research", note="研发确认")
        service.countersign_application(request_id="req-sign-legal", actor_id="legal-001",
                                        application_id=application_id, duty="legal", note="法务确认")
        service.countersign_application(request_id="req-sign-media", actor_id="media-001",
                                        application_id=application_id, duty="media", note="媒体确认")

        media_credential = service.issue_credential(
            request_id="req-cred-media", actor_id="media-001", application_id=application_id,
            audience_id=media_id, seats=3, expires_at=_at(40 * 24), purpose=PURPOSE).resource_id
        first = service.record_access(request_id="req-access-1", credential_id=media_credential,
                                      audience_id=media_id, accessor_label="行业记者B", purpose=PURPOSE)
        replay = service.record_access(request_id="req-access-1", credential_id=media_credential,
                                       audience_id=media_id, accessor_label="行业记者B", purpose=PURPOSE)
        investor_credential = service.issue_credential(
            request_id="req-cred-inv", actor_id="rd-001", application_id=application_id,
            audience_id=investor_id, seats=2, expires_at=_at(40 * 24), purpose=PURPOSE).resource_id
        service.clear_prerequisite(request_id="req-patent", actor_id="legal-001", version_id=version_id,
                                   item="PCT专利申请", reference="PCT/CN2026/000001")
        service.clear_prerequisite(request_id="req-reg", actor_id="legal-001", version_id=version_id,
                                   item="跨境数据监管备案", reference="备字2026-0042")

        service.clock = FixedClock(T0 + timedelta(hours=30 * 24 + 1))
        service.publish_application(request_id="req-publish", actor_id="rd-001",
                                    application_id=application_id)
        investor_access = service.record_access(
            request_id="req-access-2", credential_id=investor_credential,
            audience_id=investor_id, accessor_label="海外投资人A", purpose=PURPOSE)

        # 权利异议：只冻结真正受影响的字段与受众，历史访问保留。
        service.freeze_scope(request_id="req-freeze", actor_id="legal-001", version_id=version_id,
                             reason="rights_objection", detail="联合实验室对基准摘要归属提出异议",
                             fields=["benchmark_summary"], audience_ids=[investor_id])
        investor_blocked = False
        try:
            service.record_access(request_id="req-access-3", credential_id=investor_credential,
                                  audience_id=investor_id, accessor_label="海外投资人A", purpose=PURPOSE)
        except PermissionDenied:
            investor_blocked = True
        media_access = service.record_access(request_id="req-access-4", credential_id=media_credential,
                                             audience_id=media_id, accessor_label="行业记者B", purpose=PURPOSE)

        # 到期回收与待核验事项，随后模拟系统恢复。
        second_version = service.register_version(
            request_id="req-version-2", actor_id="rd-001", site_id="site-001",
            external_key="companion-sdk", **_version_payload(_at(60 * 24))).resource_id
        second_application = service.create_application(
            request_id="req-app-2", actor_id="rd-001", version_id=second_version, purpose=PURPOSE,
            verify_due_at=_at(41 * 24 + 1), preview_hours=24).resource_id
        service.submit_application(request_id="req-submit-2", actor_id="rd-001",
                                   application_id=second_application)
        service.clock = FixedClock(T0 + timedelta(hours=41 * 24 + 2))
        maintenance = service.run_maintenance()
        recovered = LaunchService(database, FixedClock(T0 + timedelta(hours=41 * 24 + 2)))
        recovery = recovered.run_maintenance()

        history = service.list_access_events("auditor-001", version_id=version_id)
        record = service.explain_access("auditor-001", investor_access.resource_id)
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "accesses": len(history),
            "first_replayed": first.replayed,
            "replay_replayed": replay.replayed,
            "investor_blocked_after_freeze": investor_blocked,
            "media_fields_after_freeze": sorted(
                service.explain_access("auditor-001", media_access.resource_id)["fields"]),
            "preserved_fields": sorted(record["fields"]),
            "approvals_on_record": len(record["approvals"]),
            "expired_credentials": maintenance["expired_credentials"],
            "escalated_verifications": maintenance["escalated_verifications"],
            "recovery_expired": recovery["expired_credentials"],
            "recovery_escalated": recovery["escalated_verifications"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["accesses"] == 3
          and result["replay_replayed"] and result["investor_blocked_after_freeze"]
          and result["recovery_expired"] == 0 and result["recovery_escalated"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
