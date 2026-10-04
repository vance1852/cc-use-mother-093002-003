"""在基础服务数据库上扩展首发披露所需的表结构。"""

from __future__ import annotations

from pathlib import Path

from digital_trade_foundation.storage import Database


SCHEMA_EXTENSION = """
CREATE TABLE IF NOT EXISTS launch_versions (
    version_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    external_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    contributors_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_snapshot_hash TEXT NOT NULL,
    jurisdictions_json TEXT NOT NULL,
    publish_at TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL,
    confidentiality_json TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    supersedes_version_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'withdrawn')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, external_key, revision)
);
CREATE TABLE IF NOT EXISTS launch_audiences (
    audience_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES launch_versions(version_id),
    label TEXT NOT NULL,
    organization TEXT NOT NULL,
    relationship TEXT NOT NULL CHECK(relationship IN ('investor', 'media', 'partner', 'regulator', 'other')),
    risk_note TEXT NOT NULL DEFAULT '',
    confidentiality_ref TEXT NOT NULL,
    allowed_fields_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'frozen')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS launch_applications (
    application_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES launch_versions(version_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    purpose TEXT NOT NULL,
    evidence_snapshot_hash TEXT NOT NULL,
    verify_due_at TEXT NOT NULL,
    verification_escalated INTEGER NOT NULL DEFAULT 0 CHECK(verification_escalated IN (0, 1)),
    verified_by TEXT,
    verified_at TEXT,
    preview_hours INTEGER NOT NULL CHECK(preview_hours >= 1),
    preview_starts_at TEXT,
    preview_ends_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('draft', 'verifying', 'countersign', 'preview', 'published', 'withdrawn', 'corrected')),
    applicant_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_launch_applications_active
    ON launch_applications(version_id)
    WHERE status NOT IN ('withdrawn', 'corrected');
CREATE TABLE IF NOT EXISTS launch_countersigns (
    application_id TEXT NOT NULL REFERENCES launch_applications(application_id),
    duty TEXT NOT NULL CHECK(duty IN ('research', 'legal', 'media')),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(application_id, duty)
);
CREATE TABLE IF NOT EXISTS launch_credentials (
    credential_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES launch_applications(application_id),
    version_id TEXT NOT NULL REFERENCES launch_versions(version_id),
    audience_id TEXT NOT NULL REFERENCES launch_audiences(audience_id),
    evidence_snapshot_hash TEXT NOT NULL,
    allowed_fields_json TEXT NOT NULL,
    purpose TEXT NOT NULL,
    seats_total INTEGER NOT NULL CHECK(seats_total >= 1),
    seats_used INTEGER NOT NULL DEFAULT 0 CHECK(seats_used >= 0),
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'exhausted', 'expired', 'revoked', 'frozen')),
    issued_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS launch_access_events (
    access_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL REFERENCES launch_credentials(credential_id),
    application_id TEXT NOT NULL REFERENCES launch_applications(application_id),
    version_id TEXT NOT NULL REFERENCES launch_versions(version_id),
    revision INTEGER NOT NULL,
    audience_id TEXT NOT NULL REFERENCES launch_audiences(audience_id),
    accessor_label TEXT NOT NULL,
    purpose TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    approvals_json TEXT NOT NULL,
    evidence_snapshot_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS launch_freezes (
    freeze_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES launch_versions(version_id),
    fields_json TEXT NOT NULL,
    audience_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('rights_objection', 'regulatory_restriction')),
    detail TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
"""


class LaunchDatabase(Database):
    """在基础库结构之上追加首发披露表。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(SCHEMA_EXTENSION)
