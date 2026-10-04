"""技术首发与受限披露管理服务。

在基础服务的主体、场所、幂等与哈希链审计能力之上，管理成果版本、受众关系、
披露申请流转（草拟、核验、会签、限时预览、正式公开、撤回、更正）、受限开放
凭证、访问记录与局部冻结，并保证研发方、法务、媒体联络人三方会签不能相互
代替。所有事实写入同一条审计链，系统恢复运行后会继续处理到期回收与待核验
事项。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from digital_trade_foundation.audit import append_event, digest
from digital_trade_foundation.clock import Clock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.models import WriteReceipt
from digital_trade_foundation.service import DomainService

from .domain import (DUTIES, DUTY_ROLE, FREEZE_REASONS, PREREQUISITE_KINDS,
                     PREREQUISITE_STATUSES, RELATIONSHIPS, SENSITIVITIES)
from .storage import LaunchDatabase


class _AccessExpired(Exception):
    """凭证在访问瞬间到期，需要在事务外完成回收落库。"""


class LaunchService(DomainService):
    """协调首发披露的版本、流转、开放与冻结规则。"""

    def __init__(self, database: LaunchDatabase, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        self._write_lock = threading.RLock()
        # 恢复运行后立即继续处理到期回收与待核验事项。
        self.run_maintenance()

    def _now(self) -> str:
        # 统一秒级精度，保证时间字符串的字典序与时间序一致。
        return self.clock.now().isoformat(timespec="seconds").replace("+00:00", "Z")

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    def _timestamp(self, value: str, field: str) -> str:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _positive_int(self, value: Any, field: str, upper: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValidationError(f"{field} 必须在 1 到 {upper} 之间")
        return value

    def _contributors(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("contributors 必须是非空列表")
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("贡献主体必须是对象")
            party = self._text(item.get("party", ""), "contributors.party")
            role = self._text(item.get("role", ""), "contributors.role", 80)
            share = item.get("share")
            if share is not None and (not isinstance(share, (int, float)) or not 0 <= share <= 100):
                raise ValidationError("contributors.share 必须在 0 到 100 之间")
            result.append({"party": party, "role": role, "share": share})
        return result

    def _evidence(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("evidence 必须是非空列表")
        seen: set[str] = set()
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("证明材料必须是对象")
            evidence_id = self._identifier(str(item.get("evidence_id", "")), "evidence_id")
            if evidence_id in seen:
                raise ValidationError("evidence_id 不能重复")
            seen.add(evidence_id)
            result.append({
                "evidence_id": evidence_id,
                "kind": self._text(item.get("kind", ""), "evidence.kind", 60),
                "reference": self._text(item.get("reference", ""), "evidence.reference", 200),
                "description": self._text(item.get("description", ""), "evidence.description", 200),
            })
        return result

    def _jurisdictions(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("jurisdictions 必须是非空列表")
        result = []
        for item in value:
            code = self._text(item, "jurisdictions", 16)
            if code in result:
                raise ValidationError("适用辖区不能重复")
            result.append(code)
        return result

    def _prerequisites(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise ValidationError("prerequisites 必须是列表")
        seen: set[str] = set()
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("前置事项必须是对象")
            name = self._text(item.get("item", ""), "prerequisites.item")
            if name in seen:
                raise ValidationError("前置事项不能重复")
            seen.add(name)
            kind = str(item.get("kind", "")).strip()
            if kind not in PREREQUISITE_KINDS:
                raise ValidationError("前置事项类别必须是 patent 或 regulatory")
            status = str(item.get("status", "pending")).strip()
            if status not in PREREQUISITE_STATUSES:
                raise ValidationError("前置事项状态无效")
            reference = str(item.get("reference", "")).strip()
            if len(reference) > 200:
                raise ValidationError("prerequisites.reference 过长")
            result.append({"item": name, "kind": kind, "reference": reference, "status": status})
        return result

    def _confidentiality(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("confidentiality 必须是非空列表")
        seen: set[str] = set()
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("保密承诺必须是对象")
            commitment_id = self._identifier(str(item.get("commitment_id", "")), "commitment_id")
            if commitment_id in seen:
                raise ValidationError("commitment_id 不能重复")
            seen.add(commitment_id)
            result.append({
                "commitment_id": commitment_id,
                "party": self._text(item.get("party", ""), "confidentiality.party"),
                "scope": self._text(item.get("scope", ""), "confidentiality.scope"),
                "terms": self._text(item.get("terms", ""), "confidentiality.terms", 400),
            })
        return result

    def _fields(self, value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("fields 必须是非空对象")
        result = {}
        for name, spec in value.items():
            field_name = self._identifier(str(name), "fields 名称")
            if not isinstance(spec, dict) or "value" not in spec:
                raise ValidationError("每个字段必须包含 value")
            sensitivity = str(spec.get("sensitivity", "")).strip()
            if sensitivity not in SENSITIVITIES:
                raise ValidationError("字段敏感级别无效")
            result[field_name] = {"value": spec["value"], "sensitivity": sensitivity}
        return result

    def _field_names(self, value: Any, known: set[str], field: str) -> list[str]:
        if not isinstance(value, list):
            raise ValidationError(f"{field} 必须是列表")
        result = []
        for item in value:
            name = str(item).strip()
            if name not in known:
                raise ValidationError(f"{field} 包含成果版本之外的字段")
            if name not in result:
                result.append(name)
        return sorted(result)

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------

    def _version_row(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM launch_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("成果版本不存在")
        return row

    def _application_row(self, connection, application_id: str):
        row = connection.execute(
            "SELECT * FROM launch_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("披露申请不存在")
        return row

    def _audience_row(self, connection, audience_id: str):
        row = connection.execute("SELECT * FROM launch_audiences WHERE audience_id=?", (audience_id,)).fetchone()
        if row is None:
            raise NotFoundError("受众不存在")
        return row

    def _frozen_fields(self, connection, version_id: str) -> set[str]:
        frozen: set[str] = set()
        for row in connection.execute(
            "SELECT fields_json FROM launch_freezes WHERE version_id=? AND status='active'", (version_id,)
        ):
            frozen.update(json.loads(row["fields_json"]))
        return frozen

    def _check_site_org(self, connection, actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能在其他组织的场所操作")
        return site

    def _approvals_snapshot(self, connection, application) -> list[dict[str, Any]]:
        approvals: list[dict[str, Any]] = []
        if application["verified_by"]:
            approvals.append({"duty": "verification", "actor_id": application["verified_by"],
                              "signed_at": application["verified_at"]})
        for row in connection.execute(
            "SELECT duty, actor_id, created_at FROM launch_countersigns WHERE application_id=? ORDER BY duty",
            (application["application_id"],),
        ):
            approvals.append({"duty": row["duty"], "actor_id": row["actor_id"], "signed_at": row["created_at"]})
        return approvals

    # ------------------------------------------------------------------
    # 成果版本与受众
    # ------------------------------------------------------------------

    def register_version(self, *, request_id: str, actor_id: str, site_id: str, external_key: str,
                         contributors: Any, evidence: Any, jurisdictions: Any, publish_at: str,
                         prerequisites: Any, confidentiality: Any, fields: Any) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            self._check_site_org(connection, actor, site_id)
            external_key = self._identifier(external_key, "external_key")
            contributors = self._contributors(contributors)
            evidence = self._evidence(evidence)
            snapshot_hash = digest(evidence)
            jurisdictions = self._jurisdictions(jurisdictions)
            publish_at = self._timestamp(publish_at, "publish_at")
            if publish_at <= self._now():
                raise ValidationError("publish_at 必须晚于当前时间")
            prerequisites = self._prerequisites(prerequisites)
            confidentiality = self._confidentiality(confidentiality)
            fields = self._fields(fields)
            payload = {"actor_id": actor_id, "site_id": site_id, "external_key": external_key,
                       "contributors": contributors, "evidence": evidence, "jurisdictions": jurisdictions,
                       "publish_at": publish_at, "prerequisites": prerequisites,
                       "confidentiality": confidentiality, "fields": fields}

            def create() -> tuple[str, str, dict[str, Any]]:
                version_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO launch_versions(version_id,site_id,external_key,revision,contributors_json,"
                        "evidence_json,evidence_snapshot_hash,jurisdictions_json,publish_at,prerequisites_json,"
                        "confidentiality_json,fields_json,payload_hash,supersedes_version_id,status,created_by,created_at) "
                        "VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,NULL,'active',?,?)",
                        (version_id, site_id, external_key, json_canonical(contributors),
                         json_canonical(evidence), snapshot_hash, json_canonical(jurisdictions), publish_at,
                         json_canonical(prerequisites), json_canonical(confidentiality), json_canonical(fields),
                         digest(payload), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("成果版本已经存在") from exc
                append_event(connection, actor_id=actor_id, action="launch.version_registered",
                             resource_type="launch_version", resource_id=version_id,
                             detail={"site_id": site_id, "external_key": external_key, "revision": 1,
                                     "contributors": contributors, "evidence_snapshot_hash": snapshot_hash,
                                     "jurisdictions": jurisdictions, "publish_at": publish_at,
                                     "prerequisites": prerequisites, "confidentiality": confidentiality,
                                     "field_scope": {name: spec["sensitivity"] for name, spec in fields.items()},
                                     "payload_hash": digest(payload)},
                             occurred_at=self._now())
                return "launch_version", version_id, {"version_id": version_id, "revision": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.register_version", payload=payload, create=create)

    def add_audience(self, *, request_id: str, actor_id: str, version_id: str, label: str,
                     organization: str, relationship: str, confidentiality_ref: str,
                     allowed_fields: Any, risk_note: str = "") -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            version = self._version_row(connection, version_id)
            self._check_site_org(connection, actor, version["site_id"])
            if version["status"] != "active":
                raise ConflictError("成果版本已不可用")
            label = self._text(label, "label")
            organization = self._text(organization, "organization")
            if relationship not in RELATIONSHIPS:
                raise ValidationError("受众关系类别无效")
            risk_note = str(risk_note).strip()
            if len(risk_note) > 200:
                raise ValidationError("risk_note 过长")
            known_fields = set(json.loads(version["fields_json"]))
            allowed = self._field_names(allowed_fields, known_fields, "allowed_fields")
            if not allowed:
                raise ValidationError("allowed_fields 不能为空")
            commitments = {item["commitment_id"] for item in json.loads(version["confidentiality_json"])}
            if confidentiality_ref not in commitments:
                raise ValidationError("confidentiality_ref 必须引用版本内的保密承诺")
            payload = {"actor_id": actor_id, "version_id": version_id, "label": label,
                       "organization": organization, "relationship": relationship,
                       "confidentiality_ref": confidentiality_ref, "allowed_fields": allowed,
                       "risk_note": risk_note}

            def create() -> tuple[str, str, dict[str, Any]]:
                audience_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO launch_audiences(audience_id,version_id,label,organization,relationship,"
                    "risk_note,confidentiality_ref,allowed_fields_json,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'active',?)",
                    (audience_id, version_id, label, organization, relationship, risk_note,
                     confidentiality_ref, json_canonical(allowed), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="launch.audience_added",
                             resource_type="launch_audience", resource_id=audience_id,
                             detail={"version_id": version_id, "label": label, "organization": organization,
                                     "relationship": relationship, "risk_note": risk_note,
                                     "confidentiality_ref": confidentiality_ref, "allowed_fields": allowed},
                             occurred_at=self._now())
                return "launch_audience", audience_id, {"audience_id": audience_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.add_audience", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 披露申请流转
    # ------------------------------------------------------------------

    def create_application(self, *, request_id: str, actor_id: str, version_id: str, purpose: str,
                           verify_due_at: str, preview_hours: int) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            version = self._version_row(connection, version_id)
            self._check_site_org(connection, actor, version["site_id"])
            if version["status"] != "active":
                raise ConflictError("成果版本已不可用")
            purpose = self._text(purpose, "purpose")
            verify_due_at = self._timestamp(verify_due_at, "verify_due_at")
            if verify_due_at <= self._now():
                raise ValidationError("verify_due_at 必须晚于当前时间")
            preview_hours = self._positive_int(preview_hours, "preview_hours", 720)
            payload = {"actor_id": actor_id, "version_id": version_id, "purpose": purpose,
                       "verify_due_at": verify_due_at, "preview_hours": preview_hours}

            def create() -> tuple[str, str, dict[str, Any]]:
                application_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO launch_applications(application_id,version_id,site_id,purpose,"
                        "evidence_snapshot_hash,verify_due_at,preview_hours,status,applicant_id,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,'draft',?,?,?)",
                        (application_id, version_id, version["site_id"], purpose,
                         version["evidence_snapshot_hash"], verify_due_at, preview_hours,
                         actor_id, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该成果版本已存在进行中的披露申请") from exc
                append_event(connection, actor_id=actor_id, action="launch.application_created",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"version_id": version_id, "revision": version["revision"],
                                     "purpose": purpose, "verify_due_at": verify_due_at,
                                     "preview_hours": preview_hours,
                                     "evidence_snapshot_hash": version["evidence_snapshot_hash"]},
                             occurred_at=self._now())
                return "launch_application", application_id, {"application_id": application_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.create_application", payload=payload, create=create)

    def _transition(self, connection, *, request_id: str, action: str, application_id: str,
                    payload: dict[str, Any], apply: Any) -> WriteReceipt:
        application = self._application_row(connection, application_id)

        def create() -> tuple[str, str, dict[str, Any]]:
            apply(application)
            connection.execute("UPDATE launch_applications SET updated_at=? WHERE application_id=?",
                               (self._now(), application_id))
            return "launch_application", application_id, {"application_id": application_id}

        return self._idempotent(connection, request_id=request_id, action=action, payload=payload, create=create)

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            payload = {"actor_id": actor_id, "application_id": application_id}

            def apply(application) -> None:
                if application["applicant_id"] != actor_id:
                    raise PermissionDenied("只有申请人本人可以提交核验")
                if application["status"] != "draft":
                    raise ConflictError("只有草拟状态的申请可以提交核验")
                connection.execute("UPDATE launch_applications SET status='verifying' WHERE application_id=?",
                                   (application_id,))
                append_event(connection, actor_id=actor_id, action="launch.application_submitted",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"version_id": application["version_id"]}, occurred_at=self._now())

            return self._transition(connection, request_id=request_id, action="launch.submit_application",
                                    application_id=application_id, payload=payload, apply=apply)

    def verify_application(self, *, request_id: str, actor_id: str, application_id: str,
                           approve: bool, note: str = "") -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if not isinstance(approve, bool):
                raise ValidationError("approve 必须是布尔值")
            note = str(note).strip()
            if len(note) > 400:
                raise ValidationError("note 过长")
            payload = {"actor_id": actor_id, "application_id": application_id,
                       "approve": bool(approve), "note": note}

            def apply(application) -> None:
                if application["applicant_id"] == actor_id:
                    raise PermissionDenied("申请人不能核验自己的申请")
                if application["status"] != "verifying":
                    raise ConflictError("只有核验中的申请可以核验")
                if approve:
                    connection.execute(
                        "UPDATE launch_applications SET status='countersign', verified_by=?, verified_at=? "
                        "WHERE application_id=?",
                        (actor_id, self._now(), application_id),
                    )
                    append_event(connection, actor_id=actor_id, action="launch.application_verified",
                                 resource_type="launch_application", resource_id=application_id,
                                 detail={"note": note}, occurred_at=self._now())
                else:
                    connection.execute("UPDATE launch_applications SET status='draft' WHERE application_id=?",
                                       (application_id,))
                    append_event(connection, actor_id=actor_id, action="launch.application_verification_rejected",
                                 resource_type="launch_application", resource_id=application_id,
                                 detail={"note": note}, occurred_at=self._now())

            return self._transition(connection, request_id=request_id, action="launch.verify_application",
                                    application_id=application_id, payload=payload, apply=apply)

    def countersign_application(self, *, request_id: str, actor_id: str, application_id: str,
                                duty: str, note: str = "") -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if duty not in DUTIES:
                raise ValidationError("会签职责无效")
            if actor.role != DUTY_ROLE[duty]:
                raise PermissionDenied("研发方、法务和媒体联络人不能相互代替批准")
            note = str(note).strip()
            if len(note) > 400:
                raise ValidationError("note 过长")
            payload = {"actor_id": actor_id, "application_id": application_id, "duty": duty, "note": note}

            def apply(application) -> None:
                if application["status"] != "countersign":
                    raise ConflictError("只有待会签状态的申请可以会签")
                existing = connection.execute(
                    "SELECT duty FROM launch_countersigns WHERE application_id=? AND actor_id=?",
                    (application_id, actor_id),
                ).fetchone()
                if existing is not None and existing["duty"] != duty:
                    raise PermissionDenied("研发方、法务和媒体联络人不能相互代替批准")
                try:
                    connection.execute(
                        "INSERT INTO launch_countersigns(application_id,duty,actor_id,note,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (application_id, duty, actor_id, note, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该会签职责已经完成") from exc
                append_event(connection, actor_id=actor_id, action="launch.application_countersigned",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"duty": duty, "note": note}, occurred_at=self._now())
                signed = connection.execute(
                    "SELECT COUNT(DISTINCT duty) AS count FROM launch_countersigns WHERE application_id=?",
                    (application_id,),
                ).fetchone()["count"]
                if signed == len(DUTIES):
                    starts = self.clock.now()
                    ends = (starts + timedelta(hours=application["preview_hours"])).isoformat(timespec="seconds").replace("+00:00", "Z")
                    connection.execute(
                        "UPDATE launch_applications SET status='preview', preview_starts_at=?, preview_ends_at=? "
                        "WHERE application_id=?",
                        (self._now(), ends, application_id),
                    )
                    append_event(connection, actor_id=actor_id, action="launch.application_preview_opened",
                                 resource_type="launch_application", resource_id=application_id,
                                 detail={"preview_starts_at": self._now(), "preview_ends_at": ends},
                                 occurred_at=self._now())

            return self._transition(connection, request_id=request_id, action="launch.countersign_application",
                                    application_id=application_id, payload=payload, apply=apply)

    def publish_application(self, *, request_id: str, actor_id: str, application_id: str) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            payload = {"actor_id": actor_id, "application_id": application_id}

            def apply(application) -> None:
                if application["status"] != "preview":
                    raise ConflictError("只有限时预览中的申请可以正式公开")
                version = self._version_row(connection, application["version_id"])
                if self._now() < version["publish_at"]:
                    raise ConflictError("尚未到达成果公开时间")
                pending = [item["item"] for item in json.loads(version["prerequisites_json"])
                           if item["status"] != "cleared"]
                if pending:
                    raise ConflictError("专利或监管前置事项未清结")
                connection.execute("UPDATE launch_applications SET status='published' WHERE application_id=?",
                                   (application_id,))
                append_event(connection, actor_id=actor_id, action="launch.application_published",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"version_id": application["version_id"],
                                     "revision": version["revision"]},
                             occurred_at=self._now())

            return self._transition(connection, request_id=request_id, action="launch.publish_application",
                                    application_id=application_id, payload=payload, apply=apply)

    def withdraw_application(self, *, request_id: str, actor_id: str, application_id: str,
                             reason: str) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher", "legal")
            reason = self._text(reason, "reason", 400)
            payload = {"actor_id": actor_id, "application_id": application_id, "reason": reason}

            def apply(application) -> None:
                if application["status"] in ("withdrawn", "corrected"):
                    raise ConflictError("申请已经关闭")
                connection.execute("UPDATE launch_applications SET status='withdrawn' WHERE application_id=?",
                                   (application_id,))
                revoked = [row["credential_id"] for row in connection.execute(
                    "SELECT credential_id FROM launch_credentials WHERE application_id=? AND status='active'",
                    (application_id,),
                )]
                connection.execute(
                    "UPDATE launch_credentials SET status='revoked' WHERE application_id=? AND status='active'",
                    (application_id,),
                )
                append_event(connection, actor_id=actor_id, action="launch.application_withdrawn",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"reason": reason, "revoked_credentials": revoked},
                             occurred_at=self._now())

            return self._transition(connection, request_id=request_id, action="launch.withdraw_application",
                                    application_id=application_id, payload=payload, apply=apply)

    def correct_application(self, *, request_id: str, actor_id: str, application_id: str,
                            contributors: Any, evidence: Any, jurisdictions: Any, publish_at: str,
                            prerequisites: Any, confidentiality: Any, fields: Any,
                            verify_due_at: str, preview_hours: int) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher")
            contributors = self._contributors(contributors)
            evidence = self._evidence(evidence)
            snapshot_hash = digest(evidence)
            jurisdictions = self._jurisdictions(jurisdictions)
            publish_at = self._timestamp(publish_at, "publish_at")
            if publish_at <= self._now():
                raise ValidationError("publish_at 必须晚于当前时间")
            prerequisites = self._prerequisites(prerequisites)
            confidentiality = self._confidentiality(confidentiality)
            fields = self._fields(fields)
            verify_due_at = self._timestamp(verify_due_at, "verify_due_at")
            if verify_due_at <= self._now():
                raise ValidationError("verify_due_at 必须晚于当前时间")
            preview_hours = self._positive_int(preview_hours, "preview_hours", 720)
            payload = {"actor_id": actor_id, "application_id": application_id, "contributors": contributors,
                       "evidence": evidence, "jurisdictions": jurisdictions, "publish_at": publish_at,
                       "prerequisites": prerequisites, "confidentiality": confidentiality, "fields": fields,
                       "verify_due_at": verify_due_at, "preview_hours": preview_hours}

            def create() -> tuple[str, str, dict[str, Any]]:
                application = self._application_row(connection, application_id)
                if application["status"] != "published":
                    raise ConflictError("只有正式公开的申请可以更正")
                old_version = self._version_row(connection, application["version_id"])
                new_version_id = uuid.uuid4().hex
                revision = old_version["revision"] + 1
                connection.execute(
                    "INSERT INTO launch_versions(version_id,site_id,external_key,revision,contributors_json,"
                    "evidence_json,evidence_snapshot_hash,jurisdictions_json,publish_at,prerequisites_json,"
                    "confidentiality_json,fields_json,payload_hash,supersedes_version_id,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                    (new_version_id, old_version["site_id"], old_version["external_key"], revision,
                     json_canonical(contributors), json_canonical(evidence), snapshot_hash,
                     json_canonical(jurisdictions), publish_at, json_canonical(prerequisites),
                     json_canonical(confidentiality), json_canonical(fields), digest(payload),
                     old_version["version_id"], actor_id, self._now()),
                )
                connection.execute("UPDATE launch_versions SET status='superseded' WHERE version_id=?",
                                   (old_version["version_id"],))
                connection.execute("UPDATE launch_applications SET status='corrected' WHERE application_id=?",
                                   (application_id,))
                revoked = [row["credential_id"] for row in connection.execute(
                    "SELECT credential_id FROM launch_credentials WHERE application_id=? AND status='active'",
                    (application_id,),
                )]
                connection.execute(
                    "UPDATE launch_credentials SET status='revoked' WHERE application_id=? AND status='active'",
                    (application_id,),
                )
                new_application_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO launch_applications(application_id,version_id,site_id,purpose,"
                    "evidence_snapshot_hash,verify_due_at,preview_hours,status,applicant_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'draft',?,?,?)",
                    (new_application_id, new_version_id, old_version["site_id"], application["purpose"],
                     snapshot_hash, verify_due_at, preview_hours, actor_id, self._now(), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="launch.version_registered",
                             resource_type="launch_version", resource_id=new_version_id,
                             detail={"site_id": old_version["site_id"],
                                     "external_key": old_version["external_key"], "revision": revision,
                                     "contributors": contributors, "evidence_snapshot_hash": snapshot_hash,
                                     "jurisdictions": jurisdictions, "publish_at": publish_at,
                                     "prerequisites": prerequisites, "confidentiality": confidentiality,
                                     "field_scope": {name: spec["sensitivity"] for name, spec in fields.items()},
                                     "payload_hash": digest(payload)},
                             occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="launch.application_corrected",
                             resource_type="launch_application", resource_id=application_id,
                             detail={"new_application_id": new_application_id, "new_version_id": new_version_id,
                                     "revision": revision, "revoked_credentials": revoked},
                             occurred_at=self._now())
                return "launch_application", new_application_id, {
                    "application_id": new_application_id, "version_id": new_version_id, "revision": revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.correct_application", payload=payload, create=create)

    def clear_prerequisite(self, *, request_id: str, actor_id: str, version_id: str, item: str,
                           reference: str = "") -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "legal")
            version = self._version_row(connection, version_id)
            item = self._text(item, "item")
            reference = str(reference).strip()
            if len(reference) > 200:
                raise ValidationError("reference 过长")
            payload = {"actor_id": actor_id, "version_id": version_id, "item": item, "reference": reference}

            def create() -> tuple[str, str, dict[str, Any]]:
                if version["status"] != "active":
                    raise ConflictError("成果版本已不可用")
                prerequisites = json.loads(version["prerequisites_json"])
                target = next((entry for entry in prerequisites if entry["item"] == item), None)
                if target is None:
                    raise NotFoundError("前置事项不存在")
                if target["status"] == "cleared":
                    raise ConflictError("前置事项已清结")
                target["status"] = "cleared"
                if reference:
                    target["reference"] = reference
                connection.execute("UPDATE launch_versions SET prerequisites_json=? WHERE version_id=?",
                                   (json_canonical(prerequisites), version_id))
                append_event(connection, actor_id=actor_id, action="launch.prerequisite_cleared",
                             resource_type="launch_version", resource_id=version_id,
                             detail={"item": item, "kind": target["kind"], "reference": target["reference"]},
                             occurred_at=self._now())
                return "launch_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.clear_prerequisite", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 受限开放与访问
    # ------------------------------------------------------------------

    def issue_credential(self, *, request_id: str, actor_id: str, application_id: str, audience_id: str,
                         seats: int, expires_at: str, purpose: str) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "researcher", "media")
            application = self._application_row(connection, application_id)
            audience = self._audience_row(connection, audience_id)
            seats = self._positive_int(seats, "seats", 1000)
            expires_at = self._timestamp(expires_at, "expires_at")
            if expires_at <= self._now():
                raise ValidationError("expires_at 必须晚于当前时间")
            purpose = self._text(purpose, "purpose")
            payload = {"actor_id": actor_id, "application_id": application_id, "audience_id": audience_id,
                       "seats": seats, "expires_at": expires_at, "purpose": purpose}

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] not in ("preview", "published"):
                    raise ConflictError("只有限时预览或正式公开的申请可以开放资料")
                version = self._version_row(connection, application["version_id"])
                if version["evidence_snapshot_hash"] != application["evidence_snapshot_hash"]:
                    raise ConflictError("证据快照与申请不一致，不能开放资料")
                if audience["version_id"] != application["version_id"]:
                    raise ValidationError("受众不属于该成果版本")
                if audience["status"] != "active":
                    raise ConflictError("受众已被冻结")
                if purpose != application["purpose"]:
                    raise ValidationError("凭证用途必须与申请用途一致")
                allowed = sorted(set(json.loads(audience["allowed_fields_json"]))
                                 - self._frozen_fields(connection, application["version_id"]))
                if not allowed:
                    raise ConflictError("可开放字段已全部冻结")
                credential_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO launch_credentials(credential_id,application_id,version_id,audience_id,"
                    "evidence_snapshot_hash,allowed_fields_json,purpose,seats_total,seats_used,expires_at,"
                    "status,issued_by,created_at) VALUES(?,?,?,?,?,?,?,?,0,?,'active',?,?)",
                    (credential_id, application_id, application["version_id"], audience_id,
                     application["evidence_snapshot_hash"], json_canonical(allowed), purpose, seats,
                     expires_at, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="launch.credential_issued",
                             resource_type="launch_credential", resource_id=credential_id,
                             detail={"application_id": application_id, "version_id": application["version_id"],
                                     "audience_id": audience_id, "allowed_fields": allowed, "seats": seats,
                                     "expires_at": expires_at, "purpose": purpose,
                                     "evidence_snapshot_hash": application["evidence_snapshot_hash"]},
                             occurred_at=self._now())
                return "launch_credential", credential_id, {"credential_id": credential_id,
                                                            "allowed_fields": allowed}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.issue_credential", payload=payload, create=create)

    def record_access(self, *, request_id: str, credential_id: str, audience_id: str,
                      accessor_label: str, purpose: str) -> WriteReceipt:
        credential_id = self._identifier(credential_id, "credential_id")
        audience_id = self._identifier(audience_id, "audience_id")
        accessor_label = self._text(accessor_label, "accessor_label")
        purpose = self._text(purpose, "purpose")
        payload = {"credential_id": credential_id, "audience_id": audience_id,
                   "accessor_label": accessor_label, "purpose": purpose}
        try:
            with self._write_lock, self.database.transaction(immediate=True) as connection:

                def create() -> tuple[str, str, dict[str, Any]]:
                    credential = connection.execute(
                        "SELECT * FROM launch_credentials WHERE credential_id=?", (credential_id,)
                    ).fetchone()
                    if credential is None:
                        raise NotFoundError("下载凭证不存在")
                    if credential["audience_id"] != audience_id:
                        raise PermissionDenied("凭证与受众不匹配")
                    if purpose != credential["purpose"]:
                        raise PermissionDenied("访问用途与凭证用途不符")
                    if credential["status"] == "exhausted":
                        raise ConflictError("凭证席位已用完")
                    if credential["status"] != "active":
                        reasons = {"expired": "凭证已过期", "revoked": "凭证已撤回", "frozen": "凭证已冻结"}
                        raise PermissionDenied(reasons.get(credential["status"], "凭证不可用"))
                    if self._now() >= credential["expires_at"]:
                        raise _AccessExpired()
                    application = self._application_row(connection, credential["application_id"])
                    if application["status"] not in ("preview", "published"):
                        raise PermissionDenied("披露申请已关闭")
                    audience = self._audience_row(connection, audience_id)
                    if audience["status"] != "active":
                        raise PermissionDenied("受众已被冻结")
                    fields = sorted(set(json.loads(credential["allowed_fields_json"]))
                                    - self._frozen_fields(connection, credential["version_id"]))
                    if not fields:
                        raise PermissionDenied("可查看字段已全部冻结")
                    cursor = connection.execute(
                        "UPDATE launch_credentials SET seats_used=seats_used+1, "
                        "status=CASE WHEN seats_used+1>=seats_total THEN 'exhausted' ELSE status END "
                        "WHERE credential_id=? AND status='active' AND seats_used<seats_total",
                        (credential_id,),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("凭证席位已用完")
                    version = self._version_row(connection, credential["version_id"])
                    approvals = self._approvals_snapshot(connection, application)
                    access_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO launch_access_events(access_id,credential_id,application_id,version_id,"
                        "revision,audience_id,accessor_label,purpose,fields_json,approvals_json,"
                        "evidence_snapshot_hash,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (access_id, credential_id, credential["application_id"], credential["version_id"],
                         version["revision"], audience_id, accessor_label, purpose, json_canonical(fields),
                         json_canonical(approvals), credential["evidence_snapshot_hash"], self._now()),
                    )
                    append_event(connection, actor_id=f"audience:{audience_id}",
                                 action="launch.disclosure_accessed",
                                 resource_type="launch_access", resource_id=access_id,
                                 detail={"credential_id": credential_id,
                                         "application_id": credential["application_id"],
                                         "version_id": credential["version_id"],
                                         "revision": version["revision"],
                                         "audience_id": audience_id, "accessor_label": accessor_label,
                                         "purpose": purpose, "fields": fields, "approvals": approvals,
                                         "evidence_snapshot_hash": credential["evidence_snapshot_hash"]},
                                 occurred_at=self._now())
                    return "launch_access", access_id, {"access_id": access_id, "fields": fields}

                return self._idempotent(connection, request_id=request_id,
                                        action="launch.record_access", payload=payload, create=create)
        except _AccessExpired:
            # 在独立事务中完成到期回收，避免随本次回滚丢失。
            self.run_maintenance()
            raise PermissionDenied("凭证已过期")

    # ------------------------------------------------------------------
    # 冻结与解除
    # ------------------------------------------------------------------

    def freeze_scope(self, *, request_id: str, actor_id: str, version_id: str, reason: str,
                     detail: str, fields: Any = (), audience_ids: Any = ()) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "legal")
            version = self._version_row(connection, version_id)
            if reason not in FREEZE_REASONS:
                raise ValidationError("冻结原因必须是 rights_objection 或 regulatory_restriction")
            detail = self._text(detail, "detail", 400)
            known_fields = set(json.loads(version["fields_json"]))
            frozen_fields = self._field_names(list(fields), known_fields, "fields")
            known_audiences = {row["audience_id"] for row in connection.execute(
                "SELECT audience_id FROM launch_audiences WHERE version_id=?", (version_id,))}
            frozen_audiences = []
            for item in audience_ids or []:
                audience_id = str(item).strip()
                if audience_id not in known_audiences:
                    raise ValidationError("audience_ids 包含版本之外的受众")
                if audience_id not in frozen_audiences:
                    frozen_audiences.append(audience_id)
            if not frozen_fields and not frozen_audiences:
                raise ValidationError("必须指明真正受影响的字段或受众")
            payload = {"actor_id": actor_id, "version_id": version_id, "reason": reason, "detail": detail,
                       "fields": frozen_fields, "audience_ids": sorted(frozen_audiences)}

            def create() -> tuple[str, str, dict[str, Any]]:
                if version["status"] != "active":
                    raise ConflictError("成果版本已不可用")
                freeze_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO launch_freezes(freeze_id,version_id,fields_json,audience_ids_json,reason,"
                    "detail,status,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                    (freeze_id, version_id, json_canonical(frozen_fields),
                     json_canonical(sorted(frozen_audiences)), reason, detail, actor_id, self._now()),
                )
                for audience_id in frozen_audiences:
                    connection.execute(
                        "UPDATE launch_audiences SET status='frozen' WHERE audience_id=?", (audience_id,))
                    connection.execute(
                        "UPDATE launch_credentials SET status='frozen' "
                        "WHERE version_id=? AND audience_id=? AND status='active'",
                        (version_id, audience_id),
                    )
                append_event(connection, actor_id=actor_id, action="launch.scope_frozen",
                             resource_type="launch_freeze", resource_id=freeze_id,
                             detail={"version_id": version_id, "fields": frozen_fields,
                                     "audience_ids": sorted(frozen_audiences), "reason": reason,
                                     "detail": detail},
                             occurred_at=self._now())
                return "launch_freeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.freeze_scope", payload=payload, create=create)

    def lift_freeze(self, *, request_id: str, actor_id: str, freeze_id: str) -> WriteReceipt:
        with self._write_lock, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "legal")
            payload = {"actor_id": actor_id, "freeze_id": freeze_id}

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze = connection.execute(
                    "SELECT * FROM launch_freezes WHERE freeze_id=?", (freeze_id,)).fetchone()
                if freeze is None:
                    raise NotFoundError("冻结记录不存在")
                if freeze["status"] != "active":
                    raise ConflictError("冻结已经解除")
                connection.execute("UPDATE launch_freezes SET status='lifted' WHERE freeze_id=?",
                                   (freeze_id,))
                restored = []
                for audience_id in json.loads(freeze["audience_ids_json"]):
                    still_frozen = connection.execute(
                        "SELECT 1 FROM launch_freezes WHERE version_id=? AND status='active' "
                        "AND audience_ids_json LIKE ? LIMIT 1",
                        (freeze["version_id"], f'%"{audience_id}"%'),
                    ).fetchone()
                    if still_frozen is None:
                        connection.execute(
                            "UPDATE launch_audiences SET status='active' WHERE audience_id=?", (audience_id,))
                        connection.execute(
                            "UPDATE launch_credentials SET status='active' "
                            "WHERE version_id=? AND audience_id=? AND status='frozen' "
                            "AND seats_used<seats_total AND expires_at>?",
                            (freeze["version_id"], audience_id, self._now()),
                        )
                        restored.append(audience_id)
                append_event(connection, actor_id=actor_id, action="launch.freeze_lifted",
                             resource_type="launch_freeze", resource_id=freeze_id,
                             detail={"version_id": freeze["version_id"], "restored_audiences": restored},
                             occurred_at=self._now())
                return "launch_freeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="launch.lift_freeze", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 到期回收与待核验事项
    # ------------------------------------------------------------------

    def run_maintenance(self) -> dict[str, int]:
        """处理到期凭证回收与逾期未核验事项，可重复执行且恢复后自动继续。"""

        with self._write_lock, self.database.transaction(immediate=True) as connection:
            now = self._now()
            expired = 0
            for row in connection.execute(
                "SELECT credential_id, expires_at FROM launch_credentials WHERE status='active' AND expires_at<=?",
                (now,),
            ).fetchall():
                connection.execute(
                    "UPDATE launch_credentials SET status='expired' WHERE credential_id=? AND status='active'",
                    (row["credential_id"],),
                )
                append_event(connection, actor_id="system", action="launch.credential_expired",
                             resource_type="launch_credential", resource_id=row["credential_id"],
                             detail={"expires_at": row["expires_at"]}, occurred_at=now)
                expired += 1
            escalated = 0
            for row in connection.execute(
                "SELECT application_id, verify_due_at FROM launch_applications "
                "WHERE status='verifying' AND verification_escalated=0 AND verify_due_at<?",
                (now,),
            ).fetchall():
                connection.execute(
                    "UPDATE launch_applications SET verification_escalated=1 WHERE application_id=?",
                    (row["application_id"],),
                )
                append_event(connection, actor_id="system", action="launch.verification_overdue",
                             resource_type="launch_application", resource_id=row["application_id"],
                             detail={"verify_due_at": row["verify_due_at"]}, occurred_at=now)
                escalated += 1
        return {"expired_credentials": expired, "escalated_verifications": escalated}

    # ------------------------------------------------------------------
    # 查询与审计
    # ------------------------------------------------------------------

    def _reader(self, actor_id: str, *roles: str):
        actor = self._actor(self.database.connection, actor_id)
        if roles:
            self._require(actor, *roles)
        return actor

    def get_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        row = self._version_row(self.database.connection, version_id)
        return self._version_dict(row)

    def list_versions(self, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        self._reader(actor_id)
        rows = self.database.connection.execute(
            "SELECT * FROM launch_versions WHERE site_id=? ORDER BY external_key, revision", (site_id,)
        ).fetchall()
        return [self._version_dict(row) for row in rows]

    def _version_dict(self, row) -> dict[str, Any]:
        return {"version_id": row["version_id"], "site_id": row["site_id"],
                "external_key": row["external_key"], "revision": row["revision"],
                "contributors": json.loads(row["contributors_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "evidence_snapshot_hash": row["evidence_snapshot_hash"],
                "jurisdictions": json.loads(row["jurisdictions_json"]),
                "publish_at": row["publish_at"],
                "prerequisites": json.loads(row["prerequisites_json"]),
                "confidentiality": json.loads(row["confidentiality_json"]),
                "fields": json.loads(row["fields_json"]),
                "supersedes_version_id": row["supersedes_version_id"],
                "status": row["status"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def get_application(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        connection = self.database.connection
        row = self._application_row(connection, application_id)
        countersigns = [
            {"duty": item["duty"], "actor_id": item["actor_id"], "note": item["note"],
             "created_at": item["created_at"]}
            for item in connection.execute(
                "SELECT * FROM launch_countersigns WHERE application_id=? ORDER BY duty",
                (application_id,))
        ]
        return {"application_id": row["application_id"], "version_id": row["version_id"],
                "site_id": row["site_id"], "purpose": row["purpose"], "status": row["status"],
                "evidence_snapshot_hash": row["evidence_snapshot_hash"],
                "verify_due_at": row["verify_due_at"],
                "verification_escalated": bool(row["verification_escalated"]),
                "verified_by": row["verified_by"], "verified_at": row["verified_at"],
                "preview_hours": row["preview_hours"],
                "preview_starts_at": row["preview_starts_at"],
                "preview_ends_at": row["preview_ends_at"],
                "applicant_id": row["applicant_id"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "countersigns": countersigns}

    def list_applications(self, actor_id: str, version_id: str) -> list[dict[str, Any]]:
        self._reader(actor_id)
        rows = self.database.connection.execute(
            "SELECT application_id FROM launch_applications WHERE version_id=? ORDER BY created_at",
            (version_id,),
        ).fetchall()
        return [self.get_application(actor_id, row["application_id"]) for row in rows]

    def list_audiences(self, actor_id: str, version_id: str) -> list[dict[str, Any]]:
        self._reader(actor_id)
        rows = self.database.connection.execute(
            "SELECT * FROM launch_audiences WHERE version_id=? ORDER BY created_at", (version_id,)
        ).fetchall()
        return [{"audience_id": row["audience_id"], "version_id": row["version_id"],
                 "label": row["label"], "organization": row["organization"],
                 "relationship": row["relationship"], "risk_note": row["risk_note"],
                 "confidentiality_ref": row["confidentiality_ref"],
                 "allowed_fields": json.loads(row["allowed_fields_json"]),
                 "status": row["status"], "created_at": row["created_at"]} for row in rows]

    def get_credential(self, actor_id: str, credential_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        row = self.database.connection.execute(
            "SELECT * FROM launch_credentials WHERE credential_id=?", (credential_id,)).fetchone()
        if row is None:
            raise NotFoundError("下载凭证不存在")
        return {"credential_id": row["credential_id"], "application_id": row["application_id"],
                "version_id": row["version_id"], "audience_id": row["audience_id"],
                "evidence_snapshot_hash": row["evidence_snapshot_hash"],
                "allowed_fields": json.loads(row["allowed_fields_json"]),
                "purpose": row["purpose"], "seats_total": row["seats_total"],
                "seats_used": row["seats_used"], "expires_at": row["expires_at"],
                "status": row["status"], "issued_by": row["issued_by"], "created_at": row["created_at"]}

    def list_access_events(self, actor_id: str, *, version_id: str | None = None,
                           audience_id: str | None = None, since: str | None = None,
                           until: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, "auditor", "admin")
        query = "SELECT * FROM launch_access_events WHERE 1=1"
        parameters: list[Any] = []
        if version_id:
            query += " AND version_id=?"
            parameters.append(version_id)
        if audience_id:
            query += " AND audience_id=?"
            parameters.append(audience_id)
        if since:
            query += " AND occurred_at>=?"
            parameters.append(self._timestamp(since, "since"))
        if until:
            query += " AND occurred_at<=?"
            parameters.append(self._timestamp(until, "until"))
        query += " ORDER BY rowid"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._access_dict(row) for row in rows]

    def explain_access(self, actor_id: str, access_id: str) -> dict[str, Any]:
        self._reader(actor_id, "auditor", "admin")
        row = self.database.connection.execute(
            "SELECT * FROM launch_access_events WHERE access_id=?", (access_id,)).fetchone()
        if row is None:
            raise NotFoundError("访问记录不存在")
        return self._access_dict(row)

    def _access_dict(self, row) -> dict[str, Any]:
        return {"access_id": row["access_id"], "credential_id": row["credential_id"],
                "application_id": row["application_id"], "version_id": row["version_id"],
                "revision": row["revision"], "audience_id": row["audience_id"],
                "accessor_label": row["accessor_label"], "purpose": row["purpose"],
                "fields": json.loads(row["fields_json"]),
                "approvals": json.loads(row["approvals_json"]),
                "evidence_snapshot_hash": row["evidence_snapshot_hash"],
                "occurred_at": row["occurred_at"]}


def json_canonical(value: Any) -> str:
    """生成存储用的稳定 JSON 文本。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
