"""封装技术首发与受限披露管理服务的 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'research', 'legal', 'media', 'auditor')),
    organization TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audiences (
    audience_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    organization TEXT NOT NULL,
    relationship TEXT NOT NULL,
    commitments_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS achievements (
    achievement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS achievement_versions (
    version_id TEXT PRIMARY KEY,
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    contributors_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    jurisdictions_json TEXT NOT NULL,
    publish_at TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL,
    confidentiality_json TEXT NOT NULL,
    audience_rules_json TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE,
    supersedes_version_id TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(achievement_id, version_no)
);
CREATE TABLE IF NOT EXISTS disclosures (
    disclosure_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES achievement_versions(version_id),
    snapshot_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    audience_ids_json TEXT NOT NULL,
    seat_limit INTEGER NOT NULL CHECK(seat_limit >= 1),
    status TEXT NOT NULL CHECK(status IN
        ('draft', 'verifying', 'countersign', 'preview', 'published', 'withdrawn', 'corrected')),
    preview_ends_at TEXT NOT NULL,
    verify_due_at TEXT,
    verification_overdue INTEGER NOT NULL DEFAULT 0 CHECK(verification_overdue IN (0, 1)),
    verified_by TEXT,
    verified_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_disclosure_per_version
    ON disclosures(version_id)
    WHERE status IN ('draft', 'verifying', 'countersign', 'preview');
CREATE TABLE IF NOT EXISTS disclosure_approvals (
    approval_id TEXT PRIMARY KEY,
    disclosure_id TEXT NOT NULL REFERENCES disclosures(disclosure_id),
    role TEXT NOT NULL CHECK(role IN ('research', 'legal', 'media')),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    decided_at TEXT NOT NULL,
    UNIQUE(disclosure_id, role),
    UNIQUE(disclosure_id, actor_id)
);
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    disclosure_id TEXT NOT NULL REFERENCES disclosures(disclosure_id),
    audience_id TEXT NOT NULL REFERENCES audiences(audience_id),
    seat TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'revoked')),
    issued_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(disclosure_id, audience_id, seat)
);
CREATE TABLE IF NOT EXISTS freezes (
    freeze_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES achievement_versions(version_id),
    fields_json TEXT NOT NULL,
    relationships_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    lifted_at TEXT
);
CREATE TABLE IF NOT EXISTS access_records (
    access_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    disclosure_id TEXT NOT NULL REFERENCES disclosures(disclosure_id),
    version_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    credential_id TEXT NOT NULL REFERENCES credentials(credential_id),
    audience_id TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为披露服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
