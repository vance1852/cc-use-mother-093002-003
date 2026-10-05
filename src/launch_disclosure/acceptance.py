"""运行技术首发与受限披露管理服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.errors import PermissionDenied

from .clock import MutableClock
from .service import DisclosureService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的首发披露链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DisclosureService(database, clock)
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="系统管理员", role="admin", organization="首发企业")
        service.register_actor(request_id="req-research", actor_id="admin-1", new_actor_id="research-1",
                               display_name="研发负责人", role="research", organization="首发企业")
        service.register_actor(request_id="req-research2", actor_id="admin-1", new_actor_id="research-2",
                               display_name="研发会签人", role="research", organization="联合研发方")
        service.register_actor(request_id="req-legal", actor_id="admin-1", new_actor_id="legal-1",
                               display_name="法务负责人", role="legal", organization="首发企业")
        service.register_actor(request_id="req-media", actor_id="admin-1", new_actor_id="media-1",
                               display_name="媒体联络人", role="media", organization="首发企业")
        service.register_actor(request_id="req-auditor", actor_id="admin-1", new_actor_id="auditor-1",
                               display_name="审计员", role="auditor", organization="首发企业")
        service.register_audience(request_id="req-aud-inv", actor_id="media-1", audience_id="aud-inv",
                                  display_name="海外投资人", organization="境外基金",
                                  relationship="investor", commitments=["nda-2026"])
        service.register_audience(request_id="req-aud-med", actor_id="media-1", audience_id="aud-med",
                                  display_name="预览媒体", organization="行业媒体",
                                  relationship="competitor_affiliated", commitments=["nda-2026"])
        service.register_achievement(request_id="req-ach", actor_id="research-1",
                                     achievement_id="ach-1", title="跨境数据协作新方案")
        version_payload = {
            "contributors": [{"party": "首发企业", "share": "60%"}, {"party": "联合研发方", "share": "40%"}],
            "evidence": [{"evidence_id": "ev-1", "kind": "test_report", "hash": "abc123"}],
            "jurisdictions": ["CN", "EU", "SG"],
            "publish_at": "2026-10-01T00:00:00Z",
            "prerequisites": [{"kind": "patent", "reference": "PCT-2026-001", "status": "cleared"}],
            "confidentiality": {"nda_required": True, "clauses": ["nda-2026"]},
            "audience_rules": {"investor": ["summary", "metrics"],
                               "competitor_affiliated": ["summary"]},
            "fields": {"summary": "方案概要", "metrics": {"qps": 1200},
                       "internal_notes": "内部备注"},
        }
        service.register_version(request_id="req-v1", actor_id="research-1", version_id="ach-1-v1",
                                 achievement_id="ach-1", version_no=1, **version_payload)
        service.register_version(request_id="req-v2", actor_id="research-1", version_id="ach-1-v2",
                                 achievement_id="ach-1", version_no=2, **version_payload)
        snapshot_v1 = service.get_version("ach-1-v1").snapshot_hash
        snapshot_v2 = service.get_version("ach-1-v2").snapshot_hash
        service.create_disclosure(request_id="req-d1", actor_id="research-1", disclosure_id="d-1",
                                  version_id="ach-1-v1", snapshot_hash=snapshot_v1,
                                  purpose="launch-preview", audience_ids=["aud-inv", "aud-med"],
                                  seat_limit=4, preview_ends_at="2026-09-30T08:00:00Z")
        service.create_disclosure(request_id="req-d2", actor_id="research-1", disclosure_id="d-2",
                                  version_id="ach-1-v2", snapshot_hash=snapshot_v2,
                                  purpose="launch-preview", audience_ids=["aud-inv"],
                                  seat_limit=2, preview_ends_at="2026-10-05T08:00:00Z")
        service.submit_disclosure(request_id="req-d1-submit", actor_id="research-1", disclosure_id="d-1")
        service.submit_disclosure(request_id="req-d2-submit", actor_id="research-1", disclosure_id="d-2",
                                  verify_due_at="2026-09-26T08:00:00Z")
        service.verify_disclosure(request_id="req-d1-verify", actor_id="legal-1", disclosure_id="d-1")
        service.approve_disclosure(request_id="req-ap-r", actor_id="research-2", disclosure_id="d-1")
        service.approve_disclosure(request_id="req-ap-l", actor_id="legal-1", disclosure_id="d-1")
        service.approve_disclosure(request_id="req-ap-m", actor_id="media-1", disclosure_id="d-1")
        service.issue_credential(request_id="req-c1", actor_id="media-1", credential_id="cred-1",
                                 disclosure_id="d-1", audience_id="aud-inv", seat="seat-1",
                                 purpose="launch-preview", expires_at="2026-09-29T08:00:00Z")
        first = service.download(request_id="req-dl-1", credential_id="cred-1", purpose="launch-preview")
        replay = service.download(request_id="req-dl-1", credential_id="cred-1", purpose="launch-preview")
        service.raise_freeze(request_id="req-fz", actor_id="legal-1", freeze_id="fz-1",
                             version_id="ach-1-v1", fields=["metrics"], relationships=["investor"],
                             reason="联合研发方对指标归属提出异议")
        clock.advance(hours=1)
        second = service.download(request_id="req-dl-2", credential_id="cred-1", purpose="launch-preview")
        early_report = service.access_report(actor_id="auditor-1", audience_id="aud-inv",
                                             at="2026-09-25T08:30:00Z")
        full_report = service.access_report(actor_id="auditor-1", audience_id="aud-inv")
        clock.advance(days=5)
        service.recover_expired(request_id="req-sweep", actor_id="admin-1")
        try:
            service.download(request_id="req-dl-3", credential_id="cred-1", purpose="launch-preview")
            download_blocked = False
        except PermissionDenied:
            download_blocked = True
        clock.advance(days=2)
        service.publish_disclosure(request_id="req-d1-publish", actor_id="media-1", disclosure_id="d-1")
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "first_fields": sorted(first.fields),
            "second_fields": sorted(second.fields),
            "replay_same_access": replay.replayed and replay.access_id == first.access_id,
            "early_report_records": len(early_report),
            "full_report_records": len(full_report),
            "expired_download_blocked": download_blocked,
            "disclosure_status": service.get_disclosure("d-1").status,
            "d2_overdue": service.get_disclosure("d-2").verification_overdue,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["replay_same_access"]
          and result["first_fields"] == ["metrics", "summary"]
          and result["second_fields"] == ["summary"]
          and result["early_report_records"] == 1 and result["full_report_records"] == 2
          and result["expired_download_blocked"]
          and result["disclosure_status"] == "published" and result["d2_overdue"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
