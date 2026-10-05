"""技术首发与受限披露管理服务：成果版本、披露流转、受限访问与审计查询。"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from digital_trade_foundation.audit import append_event, canonical_json, digest, verify_chain
from digital_trade_foundation.clock import Clock, SystemClock
from digital_trade_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from digital_trade_foundation.models import WriteReceipt

from .models import AccessView, Actor, Audience, DisclosureView, VersionView
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ROLES = frozenset({"admin", "research", "legal", "media", "auditor"})
APPROVAL_ROLES = ("research", "legal", "media")
RELATIONSHIPS = frozenset({"investor", "partner", "media", "regulator", "competitor_affiliated", "other"})
PREREQUISITE_KINDS = frozenset({"patent", "regulatory"})
PREREQUISITE_STATUSES = frozenset({"pending", "cleared", "waived", "blocking"})
CLEARED_STATUSES = frozenset({"cleared", "waived"})
OPEN_STATUSES = ("preview", "published")
VERIFY_GRACE = timedelta(hours=48)


def _iso(value: datetime) -> str:
    """把时间统一规范为秒级 UTC 文本，保证可排序比较。"""

    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str, field: str) -> datetime:
    """解析外部时间输入并要求带时区。"""

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


class DisclosureService:
    """协调披露流转、职责分离、幂等、冻结与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return _iso(self.clock.now())

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _version_row(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM achievement_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("成果版本不存在")
        return row

    def _disclosure_row(self, connection, disclosure_id: str):
        row = connection.execute("SELECT * FROM disclosures WHERE disclosure_id=?", (disclosure_id,)).fetchone()
        if row is None:
            raise NotFoundError("披露申请不存在")
        return row

    # ------------------------------------------------------------------
    # 主体登记
    # ------------------------------------------------------------------

    def register_actor(self, *, request_id: str, actor_id: str, new_actor_id: str,
                       display_name: str, role: str, organization: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "new_actor_id": new_actor_id, "display_name": display_name,
                   "role": role, "organization": organization}
        with self.database.transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM actors").fetchone()["count"]
            if count:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin")
            elif actor_id != "bootstrap":
                raise PermissionDenied("首位管理员必须由 bootstrap 创建")
            new_actor_id = self._identifier(new_actor_id, "new_actor_id")
            display_name = self._text(display_name, "display_name")
            organization = self._text(organization, "organization")
            if role not in ROLES:
                raise ValidationError("role 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO actors(actor_id,display_name,role,organization,active,created_at) VALUES(?,?,?,?,1,?)",
                        (new_actor_id, display_name, role, organization, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("操作者编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="actor.registered",
                             resource_type="actor", resource_id=new_actor_id,
                             detail={"display_name": display_name, "role": role, "organization": organization},
                             occurred_at=self._now())
                return "actor", new_actor_id, {"actor_id": new_actor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_actor", payload=payload, create=create)

    def register_audience(self, *, request_id: str, actor_id: str, audience_id: str, display_name: str,
                          organization: str, relationship: str, commitments: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "audience_id": audience_id, "display_name": display_name,
                   "organization": organization, "relationship": relationship, "commitments": commitments}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "media")
            audience_id = self._identifier(audience_id, "audience_id")
            display_name = self._text(display_name, "display_name")
            organization = self._text(organization, "organization")
            if relationship not in RELATIONSHIPS:
                raise ValidationError("relationship 不在允许范围内")
            if not isinstance(commitments, list):
                raise ValidationError("commitments 必须是数组")
            normalized_commitments = sorted({self._identifier(item, "commitments") for item in commitments})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO audiences(audience_id,display_name,organization,relationship,commitments_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (audience_id, display_name, organization, relationship,
                         canonical_json(normalized_commitments), self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("受众编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="audience.registered",
                             resource_type="audience", resource_id=audience_id,
                             detail={"display_name": display_name, "organization": organization,
                                     "relationship": relationship, "commitments": normalized_commitments},
                             occurred_at=self._now())
                return "audience", audience_id, {"audience_id": audience_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_audience", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 成果与版本
    # ------------------------------------------------------------------

    def register_achievement(self, *, request_id: str, actor_id: str,
                             achievement_id: str, title: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "achievement_id": achievement_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research")
            achievement_id = self._identifier(achievement_id, "achievement_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO achievements(achievement_id,title,created_by,created_at) VALUES(?,?,?,?)",
                        (achievement_id, title, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("成果编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="achievement.registered",
                             resource_type="achievement", resource_id=achievement_id,
                             detail={"title": title}, occurred_at=self._now())
                return "achievement", achievement_id, {"achievement_id": achievement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_achievement", payload=payload, create=create)

    def _validate_contributors(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("contributors 必须是非空数组")
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("contributors 元素必须是对象")
            party = self._text(item.get("party", ""), "contributors.party")
            share = item.get("share", "")
            if not isinstance(share, (str, int, float)):
                raise ValidationError("contributors.share 必须是字符串或数字")
            normalized.append({"party": party, "share": share})
        return normalized

    def _validate_evidence(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("evidence 必须是非空数组")
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("evidence 元素必须是对象")
            normalized.append({
                "evidence_id": self._identifier(item.get("evidence_id", ""), "evidence.evidence_id"),
                "kind": self._text(item.get("kind", ""), "evidence.kind", 80),
                "hash": self._text(item.get("hash", ""), "evidence.hash", 128),
            })
        return normalized

    def _validate_prerequisites(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise ValidationError("prerequisites 必须是数组")
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("prerequisites 元素必须是对象")
            kind = str(item.get("kind", "")).strip()
            status = str(item.get("status", "")).strip()
            if kind not in PREREQUISITE_KINDS:
                raise ValidationError("prerequisites.kind 必须是 patent 或 regulatory")
            if status not in PREREQUISITE_STATUSES:
                raise ValidationError("prerequisites.status 不在允许范围内")
            normalized.append({"kind": kind,
                               "reference": self._text(item.get("reference", ""), "prerequisites.reference"),
                               "status": status})
        return normalized

    def _validate_confidentiality(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("confidentiality 必须是对象")
        nda_required = bool(value.get("nda_required", False))
        clauses = value.get("clauses", [])
        if not isinstance(clauses, list):
            raise ValidationError("confidentiality.clauses 必须是数组")
        normalized = sorted({self._identifier(item, "confidentiality.clauses") for item in clauses})
        if nda_required and not normalized:
            raise ValidationError("需要保密承诺时 clauses 不能为空")
        return {"nda_required": nda_required, "clauses": normalized}

    def _validate_fields(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("fields 必须是非空对象")
        for key in value:
            self._identifier(key, "fields 键")
        try:
            canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("fields 必须可序列化为 JSON") from exc
        return dict(value)

    def _validate_audience_rules(self, value: Any, field_names: set[str]) -> dict[str, list[str]]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("audience_rules 必须是非空对象")
        normalized: dict[str, list[str]] = {}
        for relationship, fields in value.items():
            if relationship not in RELATIONSHIPS:
                raise ValidationError("audience_rules 包含未知受众关系")
            if not isinstance(fields, list) or not fields:
                raise ValidationError("audience_rules 的字段列表不能为空")
            names = []
            for name in fields:
                name = self._identifier(name, "audience_rules 字段")
                if name not in field_names:
                    raise ValidationError("audience_rules 引用了不存在的字段")
                names.append(name)
            normalized[relationship] = sorted(set(names))
        return normalized

    def register_version(self, *, request_id: str, actor_id: str, version_id: str, achievement_id: str,
                         version_no: int, contributors: list[dict[str, Any]], evidence: list[dict[str, Any]],
                         jurisdictions: list[str], publish_at: str, prerequisites: list[dict[str, Any]],
                         confidentiality: dict[str, Any], audience_rules: dict[str, list[str]],
                         fields: dict[str, Any], supersedes_version_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id, "achievement_id": achievement_id,
                   "version_no": version_no, "contributors": contributors, "evidence": evidence,
                   "jurisdictions": jurisdictions, "publish_at": publish_at, "prerequisites": prerequisites,
                   "confidentiality": confidentiality, "audience_rules": audience_rules, "fields": fields,
                   "supersedes_version_id": supersedes_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research")
            if connection.execute("SELECT 1 FROM achievements WHERE achievement_id=?",
                                  (achievement_id,)).fetchone() is None:
                raise NotFoundError("成果不存在")
            version_id = self._identifier(version_id, "version_id")
            if not isinstance(version_no, int) or version_no < 1:
                raise ValidationError("version_no 必须是正整数")
            contributors = self._validate_contributors(contributors)
            evidence = self._validate_evidence(evidence)
            if not isinstance(jurisdictions, list) or not jurisdictions:
                raise ValidationError("jurisdictions 必须是非空数组")
            jurisdictions = sorted({self._identifier(item, "jurisdictions") for item in jurisdictions})
            publish_at = _iso(_parse_time(publish_at, "publish_at"))
            prerequisites = self._validate_prerequisites(prerequisites)
            confidentiality = self._validate_confidentiality(confidentiality)
            fields = self._validate_fields(fields)
            audience_rules = self._validate_audience_rules(audience_rules, set(fields))
            if supersedes_version_id is not None:
                previous = self._version_row(connection, supersedes_version_id)
                if previous["achievement_id"] != achievement_id:
                    raise ValidationError("被更正版本必须属于同一成果")
            snapshot_hash = digest({
                "achievement_id": achievement_id, "version_no": version_no,
                "contributors": contributors, "evidence": evidence, "jurisdictions": jurisdictions,
                "publish_at": publish_at, "prerequisites": prerequisites,
                "confidentiality": confidentiality, "audience_rules": audience_rules, "fields": fields,
            })

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO achievement_versions(version_id,achievement_id,version_no,contributors_json,"
                        "evidence_json,jurisdictions_json,publish_at,prerequisites_json,confidentiality_json,"
                        "audience_rules_json,fields_json,snapshot_hash,supersedes_version_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (version_id, achievement_id, version_no, canonical_json(contributors),
                         canonical_json(evidence), canonical_json(jurisdictions), publish_at,
                         canonical_json(prerequisites), canonical_json(confidentiality),
                         canonical_json(audience_rules), canonical_json(fields), snapshot_hash,
                         supersedes_version_id, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("版本编号或版本号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="version.registered",
                             resource_type="version", resource_id=version_id,
                             detail={"achievement_id": achievement_id, "version_no": version_no,
                                     "snapshot_hash": snapshot_hash, "jurisdictions": jurisdictions,
                                     "publish_at": publish_at},
                             occurred_at=self._now())
                return "version", version_id, {"version_id": version_id, "snapshot_hash": snapshot_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_version", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 披露申请流转
    # ------------------------------------------------------------------

    def create_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str, version_id: str,
                          snapshot_hash: str, purpose: str, audience_ids: list[str],
                          seat_limit: int, preview_ends_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id, "version_id": version_id,
                   "snapshot_hash": snapshot_hash, "purpose": purpose, "audience_ids": audience_ids,
                   "seat_limit": seat_limit, "preview_ends_at": preview_ends_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research")
            version = self._version_row(connection, version_id)
            snapshot_hash = str(snapshot_hash).strip()
            if version["snapshot_hash"] != snapshot_hash:
                raise ConflictError("披露申请必须引用版本当前的证据快照")
            disclosure_id = self._identifier(disclosure_id, "disclosure_id")
            purpose = self._text(purpose, "purpose", 120)
            if not isinstance(audience_ids, list) or not audience_ids:
                raise ValidationError("audience_ids 必须是非空数组")
            audience_ids = sorted({self._identifier(item, "audience_ids") for item in audience_ids})
            for audience_id in audience_ids:
                if connection.execute("SELECT 1 FROM audiences WHERE audience_id=?",
                                      (audience_id,)).fetchone() is None:
                    raise NotFoundError(f"受众不存在: {audience_id}")
            if not isinstance(seat_limit, int) or seat_limit < 1:
                raise ValidationError("seat_limit 必须是正整数")
            preview_ends_at = _iso(_parse_time(preview_ends_at, "preview_ends_at"))
            if preview_ends_at <= self._now():
                raise ValidationError("preview_ends_at 必须晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO disclosures(disclosure_id,version_id,snapshot_hash,purpose,audience_ids_json,"
                        "seat_limit,status,preview_ends_at,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,'draft',?,?,?,?)",
                        (disclosure_id, version_id, snapshot_hash, purpose, canonical_json(audience_ids),
                         seat_limit, preview_ends_at, actor_id, self._now(), self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("披露编号已存在或该版本已有进行中的披露申请") from exc
                append_event(connection, actor_id=actor_id, action="disclosure.created",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"version_id": version_id, "snapshot_hash": snapshot_hash,
                                     "purpose": purpose, "audience_ids": audience_ids,
                                     "seat_limit": seat_limit, "preview_ends_at": preview_ends_at},
                             occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_disclosure", payload=payload, create=create)

    def _transition(self, connection, disclosure_id: str, status: str) -> None:
        connection.execute("UPDATE disclosures SET status=?, updated_at=? WHERE disclosure_id=?",
                           (status, self._now(), disclosure_id))

    def submit_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str,
                          verify_due_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id, "verify_due_at": verify_due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] != "draft":
                raise ConflictError("只有草拟状态的申请可以提交核验")
            if verify_due_at is None:
                due = _iso(self.clock.now() + VERIFY_GRACE)
            else:
                due = _iso(_parse_time(verify_due_at, "verify_due_at"))
            if due <= self._now():
                raise ValidationError("verify_due_at 必须晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE disclosures SET status='verifying', verify_due_at=?, verification_overdue=0,"
                    " verified_by=NULL, verified_at=NULL, updated_at=? WHERE disclosure_id=?",
                    (due, self._now(), disclosure_id),
                )
                append_event(connection, actor_id=actor_id, action="disclosure.submitted",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"verify_due_at": due}, occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": "verifying"}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_disclosure", payload=payload, create=create)

    def verify_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str,
                          decision: str = "pass") -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id, "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "legal")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] != "verifying":
                raise ConflictError("只有核验中的申请可以核验")
            if decision not in ("pass", "reject"):
                raise ValidationError("decision 必须是 pass 或 reject")
            version = self._version_row(connection, disclosure["version_id"])
            if decision == "pass":
                outstanding = [item["reference"] for item in json.loads(version["prerequisites_json"])
                               if item["status"] not in CLEARED_STATUSES]
                if outstanding:
                    raise ConflictError(f"专利或监管前置事项未清: {','.join(outstanding)}")
                next_status = "countersign"
            else:
                next_status = "draft"

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE disclosures SET status=?, verified_by=?, verified_at=?, updated_at=? WHERE disclosure_id=?",
                    (next_status, actor_id, self._now(), self._now(), disclosure_id),
                )
                append_event(connection, actor_id=actor_id, action="disclosure.verified",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"decision": decision, "next_status": next_status},
                             occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": next_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_disclosure", payload=payload, create=create)

    def approve_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *APPROVAL_ROLES)
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] != "countersign":
                raise ConflictError("只有会签中的申请可以批准")
            if disclosure["created_by"] == actor_id:
                raise PermissionDenied("申请创建人不能参与会签批准")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO disclosure_approvals(approval_id,disclosure_id,role,actor_id,decided_at) "
                        "VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, disclosure_id, actor.role, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该角色已完成会签或该操作者已参与批准") from exc
                append_event(connection, actor_id=actor_id, action="disclosure.approved",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"role": actor.role}, occurred_at=self._now())
                approved = connection.execute(
                    "SELECT COUNT(*) AS count FROM disclosure_approvals WHERE disclosure_id=?",
                    (disclosure_id,),
                ).fetchone()["count"]
                status = "countersign"
                if approved == len(APPROVAL_ROLES):
                    if disclosure["preview_ends_at"] <= self._now():
                        raise ConflictError("预览窗口已过期，无法进入限时预览")
                    status = "preview"
                    self._transition(connection, disclosure_id, status)
                    append_event(connection, actor_id=actor_id, action="disclosure.preview_started",
                                 resource_type="disclosure", resource_id=disclosure_id,
                                 detail={"preview_ends_at": disclosure["preview_ends_at"]},
                                 occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": status,
                                                     "approvals": approved}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_disclosure", payload=payload, create=create)

    def publish_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research", "media")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] != "preview":
                raise ConflictError("只有限时预览中的申请可以正式公开")
            version = self._version_row(connection, disclosure["version_id"])
            if self._now() < version["publish_at"]:
                raise ConflictError("未到版本约定的公开时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._transition(connection, disclosure_id, "published")
                append_event(connection, actor_id=actor_id, action="disclosure.published",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"publish_at": version["publish_at"]}, occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": "published"}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_disclosure", payload=payload, create=create)

    def _revoke_credentials(self, connection, disclosure_id: str) -> list[str]:
        rows = connection.execute(
            "SELECT credential_id FROM credentials WHERE disclosure_id=? AND status='active'",
            (disclosure_id,),
        ).fetchall()
        revoked = [row["credential_id"] for row in rows]
        if revoked:
            connection.execute(
                "UPDATE credentials SET status='revoked' WHERE disclosure_id=? AND status='active'",
                (disclosure_id,),
            )
        return revoked

    def withdraw_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str,
                            reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "research", "legal")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] in ("withdrawn", "corrected"):
                raise ConflictError("申请已经终止")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._transition(connection, disclosure_id, "withdrawn")
                revoked = self._revoke_credentials(connection, disclosure_id)
                append_event(connection, actor_id=actor_id, action="disclosure.withdrawn",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"reason": reason, "revoked_credentials": revoked},
                             occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": "withdrawn"}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_disclosure", payload=payload, create=create)

    def correct_disclosure(self, *, request_id: str, actor_id: str, disclosure_id: str,
                           new_version_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "disclosure_id": disclosure_id,
                   "new_version_id": new_version_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "legal")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] != "published":
                raise ConflictError("只有已公开的申请可以更正")
            old_version = self._version_row(connection, disclosure["version_id"])
            new_version = self._version_row(connection, new_version_id)
            if new_version["achievement_id"] != old_version["achievement_id"]:
                raise ValidationError("更正版本必须属于同一成果")
            if new_version["version_no"] <= old_version["version_no"]:
                raise ValidationError("更正版本的版本号必须更大")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._transition(connection, disclosure_id, "corrected")
                revoked = self._revoke_credentials(connection, disclosure_id)
                append_event(connection, actor_id=actor_id, action="disclosure.corrected",
                             resource_type="disclosure", resource_id=disclosure_id,
                             detail={"reason": reason, "new_version_id": new_version_id,
                                     "new_snapshot_hash": new_version["snapshot_hash"],
                                     "revoked_credentials": revoked},
                             occurred_at=self._now())
                return "disclosure", disclosure_id, {"disclosure_id": disclosure_id, "status": "corrected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="correct_disclosure", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 下载凭证与受限访问
    # ------------------------------------------------------------------

    def issue_credential(self, *, request_id: str, actor_id: str, credential_id: str, disclosure_id: str,
                         audience_id: str, seat: str, purpose: str, expires_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "credential_id": credential_id, "disclosure_id": disclosure_id,
                   "audience_id": audience_id, "seat": seat, "purpose": purpose, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "media")
            disclosure = self._disclosure_row(connection, disclosure_id)
            if disclosure["status"] not in OPEN_STATUSES:
                raise ConflictError("披露未处于开放状态，不能签发凭证")
            credential_id = self._identifier(credential_id, "credential_id")
            seat = self._identifier(seat, "seat")
            purpose = self._text(purpose, "purpose", 120)
            if purpose != disclosure["purpose"]:
                raise PermissionDenied("凭证用途必须与披露目的一致")
            audience = connection.execute("SELECT * FROM audiences WHERE audience_id=?",
                                          (audience_id,)).fetchone()
            if audience is None:
                raise NotFoundError("受众不存在")
            allowed_audiences = json.loads(disclosure["audience_ids_json"])
            if audience_id not in allowed_audiences:
                raise PermissionDenied("受众不在本披露的名单内")
            version = self._version_row(connection, disclosure["version_id"])
            confidentiality = json.loads(version["confidentiality_json"])
            commitments = set(json.loads(audience["commitments_json"]))
            missing = [clause for clause in confidentiality["clauses"] if clause not in commitments]
            if confidentiality["nda_required"] and missing:
                raise PermissionDenied(f"受众缺少保密承诺: {','.join(missing)}")
            expires_at = _iso(_parse_time(expires_at, "expires_at"))
            if expires_at <= self._now():
                raise ValidationError("expires_at 必须晚于当前时间")
            seats = connection.execute(
                "SELECT COUNT(DISTINCT seat) AS count FROM credentials "
                "WHERE disclosure_id=? AND status='active'",
                (disclosure_id,),
            ).fetchone()["count"]
            seat_taken = connection.execute(
                "SELECT 1 FROM credentials WHERE disclosure_id=? AND seat=? AND status='active'",
                (disclosure_id, seat),
            ).fetchone()
            if seats >= disclosure["seat_limit"] and seat_taken is None:
                raise ConflictError("该披露的席位已经用尽")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO credentials(credential_id,disclosure_id,audience_id,seat,purpose,"
                        "expires_at,status,issued_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                        (credential_id, disclosure_id, audience_id, seat, purpose,
                         expires_at, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("凭证编号已存在或该受众席位已持有凭证") from exc
                append_event(connection, actor_id=actor_id, action="credential.issued",
                             resource_type="credential", resource_id=credential_id,
                             detail={"disclosure_id": disclosure_id, "audience_id": audience_id,
                                     "seat": seat, "purpose": purpose, "expires_at": expires_at},
                             occurred_at=self._now())
                return "credential", credential_id, {"credential_id": credential_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="issue_credential", payload=payload, create=create)

    def _visible_fields(self, connection, version, relationship: str) -> tuple[dict[str, Any], list[str]]:
        rules = json.loads(version["audience_rules_json"])
        allowed = rules.get(relationship, [])
        fields = json.loads(version["fields_json"])
        frozen: set[str] = set()
        for freeze in connection.execute(
                "SELECT * FROM freezes WHERE version_id=? AND status='active'", (version["version_id"],)):
            relationships = json.loads(freeze["relationships_json"])
            if not relationships or relationship in relationships:
                frozen.update(json.loads(freeze["fields_json"]))
        visible = {name: fields[name] for name in allowed if name not in frozen}
        return visible, allowed

    def _access_view(self, row, replayed: bool) -> AccessView:
        return AccessView(access_id=row["access_id"], audience_id=row["audience_id"],
                          disclosure_id=row["disclosure_id"], version_id=row["version_id"],
                          snapshot_hash=row["snapshot_hash"], fields=json.loads(row["fields_json"]),
                          basis=json.loads(row["basis_json"]), created_at=row["created_at"],
                          replayed=replayed)

    def download(self, *, request_id: str, credential_id: str, purpose: str) -> AccessView:
        request_id = self._identifier(request_id, "request_id")
        payload = {"credential_id": credential_id, "purpose": purpose}
        payload_hash = digest(payload)
        with self.database.transaction(immediate=True) as connection:
            receipt = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                         (request_id,)).fetchone()
            if receipt:
                if receipt["action"] != "download" or receipt["payload_hash"] != payload_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                existing = connection.execute("SELECT * FROM access_records WHERE request_id=?",
                                              (request_id,)).fetchone()
                return self._access_view(existing, True)
            credential = connection.execute("SELECT * FROM credentials WHERE credential_id=?",
                                            (credential_id,)).fetchone()
            if credential is None:
                raise NotFoundError("下载凭证不存在")
            if credential["status"] != "active":
                raise PermissionDenied("下载凭证已失效")
            if credential["expires_at"] <= self._now():
                raise PermissionDenied("下载凭证已过期限")
            purpose = self._text(purpose, "purpose", 120)
            if purpose != credential["purpose"]:
                raise PermissionDenied("访问用途与凭证用途不一致")
            disclosure = self._disclosure_row(connection, credential["disclosure_id"])
            if disclosure["status"] == "preview":
                if self._now() > disclosure["preview_ends_at"]:
                    raise ConflictError("限时预览窗口已结束")
            elif disclosure["status"] != "published":
                raise ConflictError("披露未处于开放状态")
            audience = connection.execute("SELECT * FROM audiences WHERE audience_id=?",
                                          (credential["audience_id"],)).fetchone()
            version = self._version_row(connection, disclosure["version_id"])
            visible, allowed = self._visible_fields(connection, version, audience["relationship"])
            if not allowed:
                raise PermissionDenied("该受众关系不在允许查看范围")
            if not visible:
                raise ConflictError("允许查看的字段当前全部被冻结")
            approvals = [
                {"role": row["role"], "actor_id": row["actor_id"], "decided_at": row["decided_at"]}
                for row in connection.execute(
                    "SELECT * FROM disclosure_approvals WHERE disclosure_id=? ORDER BY decided_at, role",
                    (disclosure["disclosure_id"],))
            ]
            basis = {"snapshot_hash": version["snapshot_hash"], "approvals": approvals,
                     "verified_by": disclosure["verified_by"], "verified_at": disclosure["verified_at"],
                     "credential_id": credential_id, "seat": credential["seat"], "purpose": purpose}
            access_id = uuid.uuid4().hex
            response = {"access_id": access_id, "fields": sorted(visible)}
            try:
                connection.execute(
                    "INSERT INTO access_records(access_id,request_id,disclosure_id,version_id,snapshot_hash,"
                    "credential_id,audience_id,fields_json,basis_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (access_id, request_id, disclosure["disclosure_id"], version["version_id"],
                     version["snapshot_hash"], credential_id, credential["audience_id"],
                     canonical_json(visible), canonical_json(basis), self._now()),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute("SELECT * FROM access_records WHERE request_id=?",
                                              (request_id,)).fetchone()
                return self._access_view(existing, True)
            connection.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
                "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, "download", payload_hash, "access_record", access_id,
                 canonical_json(response), self._now()),
            )
            append_event(connection, actor_id=credential["audience_id"], action="access.recorded",
                         resource_type="access_record", resource_id=access_id,
                         detail={"disclosure_id": disclosure["disclosure_id"],
                                 "version_id": version["version_id"],
                                 "snapshot_hash": version["snapshot_hash"],
                                 "credential_id": credential_id, "seat": credential["seat"],
                                 "fields": sorted(visible)},
                         occurred_at=self._now())
            row = connection.execute("SELECT * FROM access_records WHERE access_id=?",
                                     (access_id,)).fetchone()
            return self._access_view(row, False)

    # ------------------------------------------------------------------
    # 冻结与解除
    # ------------------------------------------------------------------

    def raise_freeze(self, *, request_id: str, actor_id: str, freeze_id: str, version_id: str,
                     fields: list[str], relationships: list[str], reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "freeze_id": freeze_id, "version_id": version_id,
                   "fields": fields, "relationships": relationships, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "legal")
            version = self._version_row(connection, version_id)
            freeze_id = self._identifier(freeze_id, "freeze_id")
            if not isinstance(fields, list) or not fields:
                raise ValidationError("fields 必须是非空数组")
            known_fields = set(json.loads(version["fields_json"]))
            fields = sorted({self._identifier(item, "fields") for item in fields})
            unknown = [name for name in fields if name not in known_fields]
            if unknown:
                raise ValidationError(f"冻结字段不存在于版本中: {','.join(unknown)}")
            if not isinstance(relationships, list):
                raise ValidationError("relationships 必须是数组")
            relationships = sorted({str(item).strip() for item in relationships})
            for relationship in relationships:
                if relationship not in RELATIONSHIPS:
                    raise ValidationError("relationships 包含未知受众关系")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO freezes(freeze_id,version_id,fields_json,relationships_json,reason,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (freeze_id, version_id, canonical_json(fields), canonical_json(relationships),
                         reason, actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("冻结编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="freeze.raised",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"version_id": version_id, "fields": fields,
                                     "relationships": relationships, "reason": reason},
                             occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="raise_freeze", payload=payload, create=create)

    def lift_freeze(self, *, request_id: str, actor_id: str, freeze_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "freeze_id": freeze_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "legal")
            row = connection.execute("SELECT * FROM freezes WHERE freeze_id=?", (freeze_id,)).fetchone()
            if row is None:
                raise NotFoundError("冻结记录不存在")
            if row["status"] != "active":
                raise ConflictError("冻结已经解除")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE freezes SET status='lifted', lifted_at=? WHERE freeze_id=?",
                                   (self._now(), freeze_id))
                append_event(connection, actor_id=actor_id, action="freeze.lifted",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"version_id": row["version_id"]}, occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id, "status": "lifted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="lift_freeze", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 恢复处理与审计查询
    # ------------------------------------------------------------------

    def recover_expired(self, *, request_id: str, actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                expired = [row["credential_id"] for row in connection.execute(
                    "SELECT credential_id FROM credentials WHERE status='active' AND expires_at<=? "
                    "ORDER BY credential_id", (now,))]
                for credential_id in expired:
                    connection.execute("UPDATE credentials SET status='expired' WHERE credential_id=?",
                                       (credential_id,))
                    append_event(connection, actor_id=actor_id, action="credential.expired",
                                 resource_type="credential", resource_id=credential_id,
                                 detail={"recovered_at": now}, occurred_at=now)
                overdue = [row["disclosure_id"] for row in connection.execute(
                    "SELECT disclosure_id FROM disclosures WHERE status='verifying' "
                    "AND verification_overdue=0 AND verify_due_at IS NOT NULL AND verify_due_at<? "
                    "ORDER BY disclosure_id", (now,))]
                for disclosure_id in overdue:
                    connection.execute(
                        "UPDATE disclosures SET verification_overdue=1, updated_at=? WHERE disclosure_id=?",
                        (now, disclosure_id),
                    )
                    append_event(connection, actor_id=actor_id, action="disclosure.verification_overdue",
                                 resource_type="disclosure", resource_id=disclosure_id,
                                 detail={"flagged_at": now}, occurred_at=now)
                sweep_id = uuid.uuid4().hex
                response = {"sweep_id": sweep_id, "expired_credentials": expired,
                            "overdue_verifications": overdue}
                return "recovery", sweep_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="recover_expired", payload=payload, create=create)

    def access_report(self, *, actor_id: str, audience_id: str | None = None,
                      at: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            query = "SELECT * FROM access_records"
            conditions: list[str] = []
            parameters: list[Any] = []
            if audience_id is not None:
                conditions.append("audience_id=?")
                parameters.append(audience_id)
            if at is not None:
                conditions.append("created_at<=?")
                parameters.append(_iso(_parse_time(at, "at")))
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at, access_id"
            return [
                {"access_id": row["access_id"], "audience_id": row["audience_id"],
                 "disclosure_id": row["disclosure_id"], "version_id": row["version_id"],
                 "snapshot_hash": row["snapshot_hash"], "fields": json.loads(row["fields_json"]),
                 "basis": json.loads(row["basis_json"]), "created_at": row["created_at"]}
                for row in connection.execute(query, parameters)
            ]

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------

    def get_version(self, version_id: str) -> VersionView:
        row = self.database.connection.execute(
            "SELECT * FROM achievement_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("成果版本不存在")
        return VersionView(row["version_id"], row["achievement_id"], row["version_no"],
                           row["snapshot_hash"], row["publish_at"], row["created_by"], row["created_at"])

    def get_disclosure(self, disclosure_id: str) -> DisclosureView:
        row = self.database.connection.execute(
            "SELECT * FROM disclosures WHERE disclosure_id=?", (disclosure_id,)).fetchone()
        if row is None:
            raise NotFoundError("披露申请不存在")
        return DisclosureView(row["disclosure_id"], row["version_id"], row["snapshot_hash"], row["status"],
                              row["purpose"], tuple(json.loads(row["audience_ids_json"])),
                              row["seat_limit"], row["preview_ends_at"], row["verify_due_at"],
                              bool(row["verification_overdue"]), row["created_by"], row["updated_at"])

    def list_disclosures(self, version_id: str | None = None) -> list[DisclosureView]:
        query = "SELECT * FROM disclosures"
        parameters: list[Any] = []
        if version_id is not None:
            query += " WHERE version_id=?"
            parameters.append(version_id)
        query += " ORDER BY created_at, disclosure_id"
        views = []
        for row in self.database.connection.execute(query, parameters):
            views.append(DisclosureView(row["disclosure_id"], row["version_id"], row["snapshot_hash"],
                                        row["status"], row["purpose"],
                                        tuple(json.loads(row["audience_ids_json"])), row["seat_limit"],
                                        row["preview_ends_at"], row["verify_due_at"],
                                        bool(row["verification_overdue"]), row["created_by"],
                                        row["updated_at"]))
        return views

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
        ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                 "action": row["action"], "resource_type": row["resource_type"],
                 "resource_id": row["resource_id"], "detail": json.loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]} for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)
