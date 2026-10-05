"""定义技术首发与受限披露管理服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization: str
    active: bool


@dataclass(frozen=True)
class Audience:
    """表示登记在册的外部受众及其保密承诺。"""

    audience_id: str
    display_name: str
    organization: str
    relationship: str
    commitments: tuple[str, ...]


@dataclass(frozen=True)
class VersionView:
    """表示一个已登记的成果版本及其证据快照。"""

    version_id: str
    achievement_id: str
    version_no: int
    snapshot_hash: str
    publish_at: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class DisclosureView:
    """表示一条披露申请及其当前流转状态。"""

    disclosure_id: str
    version_id: str
    snapshot_hash: str
    status: str
    purpose: str
    audience_ids: tuple[str, ...]
    seat_limit: int
    preview_ends_at: str
    verify_due_at: str | None
    verification_overdue: bool
    created_by: str
    updated_at: str


@dataclass(frozen=True)
class AccessView:
    """表示一次已经发生的受限资料访问。"""

    access_id: str
    audience_id: str
    disclosure_id: str
    version_id: str
    snapshot_hash: str
    fields: dict[str, Any]
    basis: dict[str, Any]
    created_at: str
    replayed: bool = field(default=False)
