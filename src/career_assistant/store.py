"""Versioned course records in Radar's SQLite database; no second business DB."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid

from career_assistant.db import _SCHEMA as RADAR_SCHEMA
from career_assistant.models import now_hk, to_iso
from .profile_context import PROFILE_SECTIONS, normalize_profile_data, validate_profile_item
from .resume_boundary import BoundaryEvaluation, RefusalLevel, evaluation_from_audit
from .resume_document import validate_delivery_profile

UPSTREAM_COMMIT = "e2bcb2ba917644b8a88d9981d4e3eb514be98c65"
SCHEMA_VERSION = 14
TABLES = {
    name: f"course_{name}" for name in (
        "resumes", "resume_versions", "reports", "preferences", "job_snapshots",
        "matches", "materials", "application_events", "tasks", "email_attempts", "preparations",
        "favorites", "tailored_resumes", "profile_versions", "resume_delivery_profiles",
        "output_approvals",
        "task_outbox",
        "share_tokens",
        "application_packs",
    )
}
TABLES["applications"] = "applications"
TABLES["jobs"] = "jobs"
# 看板与申请筛选共用同一组状态，顺序即看板从左到右的列顺序。
APPLICATION_STATUS_ORDER = ("saved", "preparing", "applied", "interview", "offer",
                             "rejected", "withdrawn")
APPLICATION_STATUSES = set(APPLICATION_STATUS_ORDER)
APPLICATION_LABELS = {"saved": "收藏", "preparing": "准备中", "applied": "已投递",
                      "interview": "面试", "offer": "Offer", "rejected": "拒绝",
                      "withdrawn": "撤回"}

SCHEMA = """
CREATE TABLE course_resumes (
 id INTEGER PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
 sha256 TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE course_reports (
 id INTEGER PRIMARY KEY, resume_id INTEGER NOT NULL REFERENCES course_resumes(id),
 data_json TEXT NOT NULL, task_id INTEGER UNIQUE REFERENCES course_tasks(id),
 created_at TEXT NOT NULL
);
CREATE TABLE course_resume_versions (
 id INTEGER PRIMARY KEY, resume_id INTEGER NOT NULL REFERENCES course_resumes(id),
 version INTEGER NOT NULL, facts_json TEXT NOT NULL,
 report_id INTEGER REFERENCES course_reports(id), confirmed_at TEXT NOT NULL,
 UNIQUE(resume_id,version)
);
CREATE TABLE course_preferences (
 id INTEGER PRIMARY KEY,
 resume_version_id INTEGER NOT NULL REFERENCES course_resume_versions(id),
 data_json TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE TABLE course_profile_versions (
 id INTEGER PRIMARY KEY,
 version INTEGER NOT NULL UNIQUE,
 data_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE course_job_snapshots (
 id INTEGER PRIMARY KEY, job_uid TEXT NOT NULL REFERENCES jobs(job_uid),
 title TEXT NOT NULL, company TEXT NOT NULL, source TEXT NOT NULL,
 external_id TEXT NOT NULL, url TEXT NOT NULL, jd TEXT NOT NULL,
 is_full INTEGER NOT NULL CHECK(is_full IN (0,1)), fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(job_uid,fingerprint)
);
CREATE TABLE course_matches (
 id INTEGER PRIMARY KEY,
 resume_version_id INTEGER NOT NULL REFERENCES course_resume_versions(id),
 profile_version_id INTEGER REFERENCES course_profile_versions(id),
 snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id),
 preference_id INTEGER REFERENCES course_preferences(id),
 data_json TEXT NOT NULL, task_id INTEGER UNIQUE REFERENCES course_tasks(id),
 created_at TEXT NOT NULL
);
CREATE TABLE course_materials (
 id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL REFERENCES course_matches(id),
 version INTEGER NOT NULL, data_json TEXT NOT NULL,
 parent_id INTEGER REFERENCES course_materials(id),
 task_id INTEGER UNIQUE REFERENCES course_tasks(id), created_at TEXT NOT NULL,
 UNIQUE(match_id,version)
);
CREATE TABLE course_application_events (
 id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE course_tasks (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'queued'
   CHECK(status IN ('queued','running','succeeded','failed','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
 side_effect INTEGER NOT NULL DEFAULT 0 CHECK(side_effect IN (0,1)),
 owner TEXT, error TEXT NOT NULL DEFAULT '', result_json TEXT,
 available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX course_tasks_pending ON course_tasks(status,available_at,id);
CREATE TABLE course_task_outbox (
 id INTEGER PRIMARY KEY,
 task_id INTEGER NOT NULL UNIQUE REFERENCES course_tasks(id),
 topic TEXT NOT NULL,
 event_key TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','published')),
 attempts INTEGER NOT NULL DEFAULT 0,
 available_at TEXT NOT NULL,
 published_at TEXT,
 last_error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX course_task_outbox_pending
 ON course_task_outbox(status,available_at,id);
CREATE TABLE course_tailored_resumes (
 id INTEGER PRIMARY KEY,
 snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id),
 resume_id INTEGER REFERENCES course_resumes(id),
 title TEXT NOT NULL,
 body TEXT NOT NULL,
 resume_version_id INTEGER REFERENCES course_resume_versions(id),
 profile_version_id INTEGER REFERENCES course_profile_versions(id),
 delivery_profile_id INTEGER REFERENCES course_resume_delivery_profiles(id),
 document_json TEXT,
 audit_json TEXT,
 plan_json TEXT,
 boundary_json TEXT,
 refusal_level TEXT NOT NULL DEFAULT 'review',
 pdf_path TEXT,
 pdf_sha256 TEXT,
 content_sha256 TEXT,
 template_version TEXT,
 renderer_version TEXT,
 page_count INTEGER,
 artifact_status TEXT NOT NULL DEFAULT 'legacy',
 created_at TEXT NOT NULL
);
CREATE TABLE course_tailored_resume_deletions (
 tailored_resume_id INTEGER PRIMARY KEY REFERENCES course_tailored_resumes(id),
 deleted_by INTEGER NOT NULL REFERENCES course_users(id),
 deleted_at TEXT NOT NULL
);
CREATE TABLE course_resume_delivery_profiles (
 id INTEGER PRIMARY KEY,
 version INTEGER NOT NULL UNIQUE,
 data_json TEXT NOT NULL,
 confirmed_at TEXT NOT NULL
);
CREATE TABLE course_email_attempts (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES course_tasks(id),
 status TEXT NOT NULL CHECK(status IN ('preview','failed','accepted','uncertain')),
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""


def stamp() -> str:
    return to_iso(now_hk())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def canonical_output_text(value: str) -> str:
    """Normalize only line endings and surrounding whitespace for approval."""
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def output_content_hash(value: str) -> str:
    normalized = canonical_output_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    return result


class Store:
    """Short-lived per-operation connections also work with Flask and a worker thread."""

    def __init__(self, db_path: str | Path, task_backend: str | None = None):
        self.path = Path(db_path).expanduser().resolve()
        self.db_path = str(self.path)
        backend = (task_backend or os.environ.get("TASK_BACKEND", "sqlite")).strip().lower()
        if backend not in {"sqlite", "kafka"}:
            raise ValueError("TASK_BACKEND 只支持 sqlite 或 kafka")
        self.task_backend = backend
        self.task_topic = os.environ.get("KAFKA_TASK_TOPIC", "career-assistant.tasks").strip()
        if not self.task_topic:
            raise ValueError("KAFKA_TASK_TOPIC 不能为空")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_v1(self) -> str | None:
        """Back up an existing database before an explicit, atomic schema migration.

        sqlite3.backup includes committed WAL pages. The backup remains a complete
        readable pre-migration database. Already migrated databases are untouched.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        backup = None
        with self.connect() as conn:
            has_migrations = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            if has_migrations:
                current = conn.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if current and current > 1:
                    raise RuntimeError("数据库版本比本程序新；请使用相应版本的程序。")
                if current == 1:
                    return None
            if existed:
                backup = str(self.path.with_name(
                    f"{self.path.name}.before-course-v1-{uuid.uuid4().hex[:12]}.bak"
                ))
                with closing(sqlite3.connect(backup)) as destination:
                    conn.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("数据库备份校验失败，未执行迁移。")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS course_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, upstream_commit TEXT NOT NULL)"
            )
            # Another startup may have completed the migration before this lock.
            if conn.execute("SELECT 1 FROM course_migrations WHERE version=1").fetchone():
                return backup
            for statement in (RADAR_SCHEMA + SCHEMA).split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
            additions = {
                "course_resume_version_id": "INTEGER REFERENCES course_resume_versions(id)",
                "course_material_id": "INTEGER REFERENCES course_materials(id)",
                "course_snapshot_id": "INTEGER REFERENCES course_job_snapshots(id)",
                "created_at": "TEXT", "updated_at": "TEXT",
            }
            for column, definition in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {definition}")
            # Immutable evidence must survive later edits and application changes.
            for table in (
                "reports", "resume_versions", "preferences",
                "job_snapshots", "matches", "materials", "application_events",
            ):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER course_{table}_no_{action.lower()} "
                        f"BEFORE {action} ON course_{table} BEGIN "
                        "SELECT RAISE(ABORT,'历史版本不可修改；请另存新版本'); END"
                    )
            conn.execute(
                "CREATE TRIGGER course_resumes_no_update BEFORE UPDATE ON course_resumes "
                "WHEN OLD.text != '' OR NEW.text = '' OR NEW.filename != OLD.filename "
                "OR NEW.path != OLD.path OR NEW.sha256 != OLD.sha256 "
                "OR NEW.created_at != OLD.created_at BEGIN "
                "SELECT RAISE(ABORT,'简历原文只能首次写入'); END"
            )
            conn.execute(
                "CREATE TRIGGER course_resumes_no_delete BEFORE DELETE ON course_resumes "
                "BEGIN SELECT RAISE(ABORT,'简历原件不可删除'); END"
            )
            conn.execute(
                "INSERT INTO course_migrations VALUES(?,?,?)",
                (1, stamp(), UPSTREAM_COMMIT),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"迁移发现外键错误：{len(violations)} 条；已回滚。")
        return backup

    def _migrate_v2(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = conn.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 2:
            raise RuntimeError("数据库版本比本程序新；请使用相应版本的程序。")
        if current == 2:
            return None
        first_backup = self._migrate_v1() if not current else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v2-{uuid.uuid4().hex[:12]}.bak")) if current else None
        with self.connect() as conn:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    conn.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("数据库备份校验失败")
            # Rebuild only the nullable legacy preference link. Preserve IDs and all FK targets.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM course_migrations WHERE version=2").fetchone():
                return backup
            conn.execute("CREATE TABLE course_preferences_v2 (id INTEGER PRIMARY KEY, "
                         "resume_version_id INTEGER REFERENCES course_resume_versions(id), "
                         "data_json TEXT NOT NULL, confirmed_at TEXT NOT NULL)")
            conn.execute("INSERT INTO course_preferences_v2 SELECT * FROM course_preferences")
            conn.execute("DROP TABLE course_preferences")
            conn.execute("ALTER TABLE course_preferences_v2 RENAME TO course_preferences")
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER course_preferences_no_{action.lower()} BEFORE {action} "
                             "ON course_preferences BEGIN SELECT RAISE(ABORT,'历史版本不可修改'); END")
            conn.execute("ALTER TABLE course_resume_versions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user_edited'")
            conn.execute("ALTER TABLE course_resume_versions ADD COLUMN extraction_task_id INTEGER REFERENCES course_tasks(id)")
            conn.execute("CREATE UNIQUE INDEX course_extraction_task ON course_resume_versions(extraction_task_id)")
            conn.execute("ALTER TABLE course_application_events ADD COLUMN basis_json TEXT NOT NULL DEFAULT '{}'")
            conn.execute("CREATE TABLE course_preparations (id INTEGER PRIMARY KEY, "
                         "original_snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                         "snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                         "resume_id INTEGER REFERENCES course_resumes(id), "
                         "resume_version_id INTEGER REFERENCES course_resume_versions(id), "
                         "preference_id INTEGER REFERENCES course_preferences(id), "
                         "match_id INTEGER REFERENCES course_matches(id), material_id INTEGER REFERENCES course_materials(id), "
                         "state TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '', "
                         "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO course_migrations VALUES(?,?,?)", (2, stamp(), UPSTREAM_COMMIT))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v2 迁移外键校验失败")
        return first_backup or backup

    def migrate(self) -> str | None:
        # A live worker must never race schema changes. A current schema is read-only.
        if self.path.exists():
            with self.connect() as c:
                exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
                if exists and (c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] or 0) >= SCHEMA_VERSION:
                    return self._migrate_v14()
        from .worker import FileLock
        lock = FileLock(Path(f"{self.path}.worker.lock"))
        lock.acquire()
        try:
            return self._migrate_v14()
        finally:
            lock.release()

    def _migrate_v3(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 3:
            raise RuntimeError("数据库版本比本程序新")
        if current == 3:
            return None
        first = self._migrate_v2() if current != 2 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v3-{uuid.uuid4().hex[:12]}.bak")) if current == 2 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=3").fetchone():
                return first or backup
            c.execute("ALTER TABLE course_preparations ADD COLUMN scope TEXT NOT NULL DEFAULT 'analysis'")
            c.execute("ALTER TABLE course_tasks ADD COLUMN checkpoint_json TEXT")
            c.execute("ALTER TABLE course_application_events ADD COLUMN event_date TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE applications ADD COLUMN course_planned_json TEXT NOT NULL DEFAULT '{}'")
            c.execute("ALTER TABLE applications ADD COLUMN course_actual_json TEXT NOT NULL DEFAULT '{}'")
            for row in c.execute("SELECT * FROM applications").fetchall():
                basis = {"snapshot_id": row["course_snapshot_id"], "resume_version_id": row["course_resume_version_id"],
                         "material_id": row["course_material_id"]}
                column = "course_actual_json" if row["applied_date"] else "course_planned_json"
                c.execute(f"UPDATE applications SET {column}=? WHERE id=?", (encode(basis), row["id"]))
            c.execute("UPDATE course_preparations SET state='matched',error='' WHERE match_id IS NOT NULL AND material_id IS NULL")
            c.execute("UPDATE course_tasks SET status='failed',error='v3 已停用自动填写',finished_at=?,updated_at=? "
                      "WHERE kind='fill' AND status IN ('queued','running','uncertain')", (stamp(), stamp()))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v3 迁移外键校验失败")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (3, stamp(), UPSTREAM_COMMIT))
        return first or backup

    def _migrate_v4(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 4:
            raise RuntimeError("数据库版本比本程序新")
        if current == 4:
            return None
        first = self._migrate_v3() if current != 3 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v4-{uuid.uuid4().hex[:12]}.bak")) if current == 3 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            c.execute("ALTER TABLE jobs ADD COLUMN last_fetched_at TEXT")
            # Legacy snapshot timestamps include local edits, not just fetches.
            # Use the known first acquisition, never migration time or local edits.
            c.execute("UPDATE jobs SET last_fetched_at=first_seen_at")
            c.execute("INSERT OR REPLACE INTO kv_store(key,value) VALUES('course_snapshot_sequence', "
                      "(SELECT COALESCE(MAX(id),0) FROM course_job_snapshots))")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (4, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v4 迁移外键校验失败")
        return first or backup

    def _migrate_v5(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 5:
            raise RuntimeError("数据库版本比本程序新")
        if current == 5:
            return None
        first = self._migrate_v4() if current != 4 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v5-{uuid.uuid4().hex[:12]}.bak")) if current == 4 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=5").fetchone():
                return first or backup
            c.execute("CREATE TABLE IF NOT EXISTS course_favorites ("
                      "id INTEGER PRIMARY KEY, "
                      "job_uid TEXT NOT NULL UNIQUE REFERENCES jobs(job_uid), "
                      "snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                      "created_at TEXT NOT NULL)")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (5, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v5 迁移外键校验失败")
        return first or backup

    def _migrate_v6(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 6:
            raise RuntimeError("数据库版本比本程序新")
        if current == 6:
            return None
        first = self._migrate_v5() if current != 5 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v6-{uuid.uuid4().hex[:12]}.bak")) if current == 5 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=6").fetchone():
                return first or backup
            c.execute("CREATE TABLE IF NOT EXISTS course_tailored_resumes ("
                      "id INTEGER PRIMARY KEY, "
                      "snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                      "resume_id INTEGER REFERENCES course_resumes(id), "
                      "title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (6, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v6 迁移外键校验失败")
        return first or backup

    def _migrate_v7(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 7:
            raise RuntimeError("数据库版本比本程序新")
        if current == 7:
            return None
        first = self._migrate_v6() if current != 6 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v7-{uuid.uuid4().hex[:12]}.bak")) if current == 6 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=7").fetchone():
                return first or backup
            c.execute("CREATE TABLE course_users ("
                      "id INTEGER PRIMARY KEY CHECK(id=1), "
                      "username TEXT NOT NULL COLLATE NOCASE UNIQUE, "
                      "password_hash TEXT NOT NULL, "
                      "created_at TEXT NOT NULL, "
                      "last_login_at TEXT)")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (7, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v7 迁移外键校验失败")
        return first or backup

    def _migrate_v8(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 8:
            raise RuntimeError("数据库版本比本程序新")
        if current == 8:
            return None
        first = self._migrate_v7() if current != 7 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v8-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 7 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=8").fetchone():
                return first or backup
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_profile_versions ("
                "id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE, "
                "data_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            match_columns = {row["name"] for row in c.execute("PRAGMA table_info(course_matches)")}
            if "profile_version_id" not in match_columns:
                c.execute(
                    "ALTER TABLE course_matches ADD COLUMN profile_version_id "
                    "INTEGER REFERENCES course_profile_versions(id)"
                )
            preparation_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(course_preparations)")
            }
            if "profile_version_id" not in preparation_columns:
                c.execute(
                    "ALTER TABLE course_preparations ADD COLUMN profile_version_id "
                    "INTEGER REFERENCES course_profile_versions(id)"
                )
            if c.execute("SELECT 1 FROM course_profile_versions LIMIT 1").fetchone() is None:
                c.execute(
                    "INSERT INTO course_profile_versions(version,data_json,created_at) VALUES(1,?,?)",
                    (encode(normalize_profile_data({})), stamp()),
                )
            for action in ("UPDATE", "DELETE"):
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS course_profile_versions_no_{action.lower()} "
                    f"BEFORE {action} ON course_profile_versions BEGIN "
                    "SELECT RAISE(ABORT,'资料历史不可修改；请保存新版本'); END"
                )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (8, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v8 迁移外键校验失败")
        return first or backup

    def _migrate_v9(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 9:
            raise RuntimeError("数据库版本比本程序新")
        if current == 9:
            return None
        first = self._migrate_v8() if current != 8 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v9-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 8 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=9").fetchone():
                return first or backup

            tailored_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(course_tailored_resumes)")
            }
            if "resume_version_id" not in tailored_columns:
                c.execute(
                    "ALTER TABLE course_tailored_resumes ADD COLUMN resume_version_id "
                    "INTEGER REFERENCES course_resume_versions(id)"
                )
            if "profile_version_id" not in tailored_columns:
                c.execute(
                    "ALTER TABLE course_tailored_resumes ADD COLUMN profile_version_id "
                    "INTEGER REFERENCES course_profile_versions(id)"
                )

            c.execute(
                "CREATE TABLE IF NOT EXISTS course_output_approvals ("
                "id INTEGER PRIMARY KEY, "
                "artifact_type TEXT NOT NULL CHECK(artifact_type IN ('material','tailored_resume')), "
                "artifact_id INTEGER NOT NULL, "
                "artifact_version INTEGER NOT NULL, "
                "content_hash TEXT NOT NULL CHECK(length(content_hash)=64), "
                "audit_snapshot_json TEXT NOT NULL, "
                "approved_by INTEGER NOT NULL REFERENCES course_users(id), "
                "approved_at TEXT NOT NULL, "
                "approval_scope TEXT NOT NULL CHECK(approval_scope IN ('export','download'))"
                ")"
            )
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS course_output_approvals_version "
                "ON course_output_approvals("
                "artifact_type,artifact_id,artifact_version,content_hash,approval_scope)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS course_output_approvals_lookup "
                "ON course_output_approvals("
                "artifact_type,artifact_id,content_hash,approval_scope,id)"
            )
            for action in ("UPDATE", "DELETE"):
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS course_output_approvals_no_{action.lower()} "
                    f"BEFORE {action} ON course_output_approvals BEGIN "
                    "SELECT RAISE(ABORT,'审批记录不可修改'); END"
                )
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS course_tailored_resumes_no_{action.lower()} "
                    f"BEFORE {action} ON course_tailored_resumes BEGIN "
                    "SELECT RAISE(ABORT,'针对性简历版本不可修改'); END"
                )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (9, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v9 迁移外键校验失败")
        return first or backup

    def _migrate_v10(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            current = (
                c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if exists else 0
            )
        if current and current > 10:
            raise RuntimeError("数据库版本比本程序新")
        if current == 10:
            return None
        first = self._migrate_v9() if current != 9 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v10-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 9 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("v10 备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=10").fetchone():
                return first or backup
            columns = {
                row["name"]
                for row in c.execute("PRAGMA table_info(course_tailored_resumes)")
            }
            additions = {
                "document_json": "TEXT",
                "audit_json": "TEXT",
                "pdf_path": "TEXT",
                "pdf_sha256": "TEXT",
                "content_sha256": "TEXT",
                "template_version": "TEXT",
                "renderer_version": "TEXT",
                "page_count": "INTEGER",
                "artifact_status": "TEXT NOT NULL DEFAULT 'legacy'",
                "delivery_profile_id": "INTEGER REFERENCES course_resume_delivery_profiles(id)",
            }
            for column, definition in additions.items():
                if column not in columns:
                    c.execute(
                        f"ALTER TABLE course_tailored_resumes "
                        f"ADD COLUMN {column} {definition}"
                    )
            c.execute(
                "UPDATE course_tailored_resumes SET artifact_status='legacy' "
                "WHERE artifact_status IS NULL OR artifact_status=''"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_resume_delivery_profiles ("
                "id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE, "
                "data_json TEXT NOT NULL, confirmed_at TEXT NOT NULL)"
            )
            for action in ("UPDATE", "DELETE"):
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS "
                    f"course_resume_delivery_profiles_no_{action.lower()} "
                    f"BEFORE {action} ON course_resume_delivery_profiles BEGIN "
                    "SELECT RAISE(ABORT,'投递信息版本不可修改'); END"
                )
            c.execute(
                "INSERT INTO course_migrations VALUES(?,?,?)",
                (10, stamp(), UPSTREAM_COMMIT),
            )
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v10 迁移外键校验失败")
        return first or backup

    def _migrate_v11(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            current = (
                c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if exists else 0
            )
        if current and current > 11:
            raise RuntimeError("数据库版本比本程序新")
        if current == 11:
            return None
        first = self._migrate_v10() if current != 10 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v11-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 10 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("v11 备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=11").fetchone():
                return first or backup
            columns = {
                row["name"]
                for row in c.execute("PRAGMA table_info(course_tailored_resumes)")
            }
            additions = {
                "plan_json": "TEXT",
                "boundary_json": "TEXT",
                "refusal_level": "TEXT NOT NULL DEFAULT 'review'",
            }
            for column, definition in additions.items():
                if column not in columns:
                    c.execute(
                        f"ALTER TABLE course_tailored_resumes "
                        f"ADD COLUMN {column} {definition}"
                    )
            c.execute("DROP TRIGGER IF EXISTS course_tailored_resumes_no_update")
            c.execute(
                "UPDATE course_tailored_resumes SET refusal_level=CASE "
                "WHEN artifact_status='audit_pass' THEN 'allow' "
                "ELSE 'review' END "
            )
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS course_tailored_resumes_no_update "
                "BEFORE UPDATE ON course_tailored_resumes BEGIN "
                "SELECT RAISE(ABORT,'针对性简历版本不可修改'); END"
            )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (11, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v11 迁移外键校验失败")
        return first or backup

    def _migrate_v12(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            current = (
                c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if exists else 0
            )
        if current and current > 12:
            raise RuntimeError("数据库版本比本程序新")
        if current == 12:
            return None
        first = self._migrate_v11() if current != 11 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v12-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 11 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("v12 备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=12").fetchone():
                return first or backup
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_tailored_resume_deletions ("
                "tailored_resume_id INTEGER PRIMARY KEY "
                "REFERENCES course_tailored_resumes(id), "
                "deleted_by INTEGER NOT NULL REFERENCES course_users(id), "
                "deleted_at TEXT NOT NULL)"
            )
            for action in ("UPDATE", "DELETE"):
                c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS "
                    f"course_tailored_resume_deletions_no_{action.lower()} "
                    f"BEFORE {action} ON course_tailored_resume_deletions BEGIN "
                    "SELECT RAISE(ABORT,'针对性简历删除记录不可修改'); END"
                )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (12, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v12 迁移外键校验失败")
        return first or backup

    def _migrate_v13(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            current = (
                c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if exists else 0
            )
        if current and current > 13:
            raise RuntimeError("数据库版本比本程序新")
        if current == 13:
            return None
        first = self._migrate_v12() if current != 12 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v13-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 12 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("v13 备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=13").fetchone():
                return first or backup
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_task_outbox ("
                "id INTEGER PRIMARY KEY, "
                "task_id INTEGER NOT NULL UNIQUE REFERENCES course_tasks(id), "
                "topic TEXT NOT NULL, event_key TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending' "
                "CHECK(status IN ('pending','published')), "
                "attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, "
                "published_at TEXT, last_error TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS course_task_outbox_pending "
                "ON course_task_outbox(status,available_at,id)"
            )
            # Backfill queued work so enabling Kafka does not strand existing tasks.
            c.execute(
                "INSERT OR IGNORE INTO course_task_outbox("
                "task_id,topic,event_key,status,attempts,available_at,created_at,updated_at) "
                "SELECT id,?,CAST(id AS TEXT),'pending',0,available_at,created_at,updated_at "
                "FROM course_tasks WHERE status='queued'",
                (self.task_topic,),
            )
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS course_task_outbox_no_delete "
                "BEFORE DELETE ON course_task_outbox BEGIN "
                "SELECT RAISE(ABORT,'任务发件箱记录不可删除'); END"
            )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (13, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v13 迁移外键校验失败")
        return first or backup

    def _migrate_v14(self) -> str | None:
        """Add the share-token ledger and the local application-pack versions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            current = (
                c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if exists else 0
            )
        if current and current > 14:
            raise RuntimeError("数据库版本比本程序新")
        if current == 14:
            return None
        first = self._migrate_v13() if current != 13 else None
        backup = str(self.path.with_name(
            f"{self.path.name}.before-course-v14-{uuid.uuid4().hex[:12]}.bak"
        )) if current == 13 else None
        with self.connect() as c:
            if backup:
                with closing(sqlite3.connect(backup)) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("v14 备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=14").fetchone():
                return first or backup
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_share_tokens ("
                "id INTEGER PRIMARY KEY, "
                "token_hash TEXT NOT NULL UNIQUE, "
                "token_hint TEXT NOT NULL, "
                "scope TEXT NOT NULL, "
                "subject_type TEXT NOT NULL, "
                "subject_id INTEGER, "
                "base_url TEXT NOT NULL DEFAULT '', "
                "summary_json TEXT NOT NULL DEFAULT '{}', "
                "created_at TEXT NOT NULL, "
                "expires_at TEXT, "
                "revoked_at TEXT, "
                "access_count INTEGER NOT NULL DEFAULT 0, "
                "last_accessed_at TEXT)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS course_share_tokens_active "
                "ON course_share_tokens(scope,revoked_at,expires_at)"
            )
            # 本地投递信息包：不可变版本，保存新版本不覆盖历史。
            c.execute(
                "CREATE TABLE IF NOT EXISTS course_application_packs ("
                "id INTEGER PRIMARY KEY, "
                "application_id INTEGER NOT NULL REFERENCES applications(id), "
                "version INTEGER NOT NULL, "
                "data_json TEXT NOT NULL, "
                "created_at TEXT NOT NULL, "
                "UNIQUE(application_id,version))"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS course_application_packs_recent "
                "ON course_application_packs(application_id,version)"
            )
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (14, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v14 迁移外键校验失败")
        return first or backup

    def cleanup_job_cache(self, *, now=None) -> int:
        """Delete only expired, unreferenced source cache in one transaction.

        Failed tasks remain retryable, so conservatively protect their inputs too.
        All preparations and upstream feedback are retained as historical evidence.
        """
        cutoff = to_iso((now or now_hk()) - timedelta(days=30))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            protected_uids, protected_snapshots = set(), set()
            def references(value, key=''):
                if isinstance(value, dict):
                    for k, v in value.items(): references(v, k)
                elif isinstance(value, list):
                    for v in value: references(v, key)
                elif key in {'snapshot_id', 'snapshot_ids', 'original_snapshot_id'}:
                    protected_snapshots.add(value)
                elif key in {'job_uid', 'job_uids'}:
                    protected_uids.add(value)
            for row in c.execute("SELECT payload_json,result_json,checkpoint_json FROM course_tasks WHERE status!='succeeded'"):
                for value in row:
                    if value: references(json.loads(value))
            candidates = c.execute("""
                SELECT job_uid FROM jobs j
                WHERE source!='manual' AND status NOT IN ('interested','saved')
                AND julianday(COALESCE(last_fetched_at,first_seen_at))<=julianday(?)
                AND NOT EXISTS(SELECT 1 FROM applications a WHERE a.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM user_feedback f WHERE f.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_favorites f WHERE f.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN applications a
                    ON a.course_snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN course_matches m
                    ON m.snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN course_preparations p
                    ON p.snapshot_id=s.id OR p.original_snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN course_tailored_resumes t
                    ON t.snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s WHERE s.job_uid=j.job_uid AND s.source='manual')
            """, (cutoff,)).fetchall()
            deleted = 0
            # Remove the blanket guard only within this write-locked transaction.
            # Restore it before commit; any error rolls back both DDL and deletes.
            trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='course_job_snapshots_no_delete'").fetchone()[0]
            c.execute("DROP TRIGGER course_job_snapshots_no_delete")
            for row in candidates:
                uid = row['job_uid']
                ids = {r[0] for r in c.execute("SELECT id FROM course_job_snapshots WHERE job_uid=?", (uid,))}
                if uid in protected_uids or ids & protected_snapshots:
                    continue
                c.execute("DELETE FROM course_job_snapshots WHERE job_uid=?", (uid,))
                c.execute("DELETE FROM jobs WHERE job_uid=?", (uid,))
                deleted += 1
            c.execute(trigger)
        return deleted

    def checkpoint(self, task_id, data):
        with self.connect() as c:
            c.execute("UPDATE course_tasks SET checkpoint_json=? WHERE id=?", (encode(data), task_id))

    def accept_material_inputs(self, task_id, submitted):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = decode(c.execute("SELECT * FROM course_tasks WHERE id=?", (task_id,)).fetchone())
            if not task or task["kind"] != "materials" or (task.get("result") or {}).get("status") != "waiting_input":
                raise ValueError("当前任务不等待材料补充")
            cp = task["checkpoint"] or {}
            if not cp.get("input_saved"):
                questions = cp["analysis"]["questions"]
                answers = {q: submitted.get(f"answer_{i}", "").strip() for i, q in enumerate(questions)}
                cp.update(answers=answers, skipped=[q for q in questions if not answers[q]], input_saved=True)
                c.execute("UPDATE course_tasks SET checkpoint_json=? WHERE id=?", (encode(cp), task_id))
            return cp

    def resume_waiting(self, resume_id):
        versions = self.list("resume_versions", resume_id=resume_id)
        if not versions:
            return
        for item in self.list("preparations", resume_id=resume_id):
            if item["resume_version_id"] is not None:
                continue
            self.update_preparation(item["id"], resume_version_id=versions[0]["id"])
            if item["state"] == "waiting_facts":
                self.update_preparation(item["id"], state="queued", error="")
                self.enqueue("prepare", {"preparation_id": item["id"]},
                             f"facts-ready:{item['id']}:{versions[0]['id']}", max_attempts=2)

    @staticmethod
    def _table(name: str) -> str:
        try:
            return TABLES[name]
        except KeyError:
            raise ValueError(f"未知数据表：{name}") from None

    def user_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM course_users").fetchone()[0])

    def create_initial_user(self, username: str, password_hash: str) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM course_users LIMIT 1").fetchone():
                raise ValueError("本机账户已经创建，请直接登录")
            cursor = conn.execute(
                "INSERT INTO course_users(id,username,password_hash,created_at) VALUES(1,?,?,?)",
                (username, password_hash, stamp()),
            )
            return int(cursor.lastrowid)

    def get_user(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id,username,created_at,last_login_at FROM course_users WHERE id=?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,created_at,last_login_at "
                "FROM course_users WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def mark_user_login(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE course_users SET last_login_at=? WHERE id=?",
                (stamp(), user_id),
            )

    def get(self, table: str, record_id: int | str) -> dict | None:
        key = "job_uid" if table == "jobs" else "id"
        with self.connect() as conn:
            return decode(conn.execute(
                f"SELECT * FROM {self._table(table)} WHERE {key}=?", (record_id,)
            ).fetchone())

    def list(self, table: str, **filters: Any) -> list[dict]:
        name = self._table(table)
        with self.connect() as conn:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}
            if not set(filters).issubset(columns):
                raise ValueError("未知筛选字段")
            where = " AND ".join(f"{key}=?" for key in filters) or "1"
            ordering = "first_seen_at DESC" if table == "jobs" else "id DESC"
            return [decode(row) for row in conn.execute(
                f"SELECT * FROM {name} WHERE {where} ORDER BY {ordering}", tuple(filters.values())
            )]

    def _insert(self, table: str, values: dict) -> int:
        with self.connect() as conn:
            return self._insert_on(conn, table, values)

    @staticmethod
    def _insert_on(conn: sqlite3.Connection, table: str, values: dict) -> int:
        # Called with internal column names only; callers never provide raw SQL.
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        cursor = conn.execute(
            f"INSERT INTO {Store._table(table)} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return int(cursor.lastrowid)

    def add_resume(self, filename: str, path: str | Path, sha256: str, text: str = "") -> int:
        return self._insert("resumes", dict(
            filename=filename, path=str(Path(path).resolve()), sha256=sha256,
            text=text, created_at=stamp(),
        ))

    def set_resume_text(self, resume_id: int, text: str) -> None:
        if not text.strip():
            raise ValueError("简历原文不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            resume = conn.execute("SELECT text FROM course_resumes WHERE id=?", (resume_id,)).fetchone()
            if resume is None:
                raise ValueError("简历不存在")
            if resume["text"] == text:
                return
            if resume["text"]:
                raise ValueError("不能覆盖已经提取的简历原文")
            conn.execute("UPDATE course_resumes SET text=? WHERE id=?", (text, resume_id))

    def save_report(self, resume_id: int, data: dict, task_id: int | None = None) -> int:
        if task_id is not None:
            existing = self.list("reports", task_id=task_id)
            if existing:
                return existing[0]["id"]
        return self._insert("reports", dict(
            resume_id=resume_id, data_json=encode(data), task_id=task_id, created_at=stamp(),
        ))

    def confirm_facts(self, resume_id: int, facts: dict, report_id: int | None = None, *, origin="user_edited", extraction_task_id=None) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if extraction_task_id is not None:
                existing = conn.execute("SELECT id FROM course_resume_versions WHERE extraction_task_id=?", (extraction_task_id,)).fetchone()
                if existing:
                    return existing["id"]
            resume = conn.execute("SELECT text FROM course_resumes WHERE id=?", (resume_id,)).fetchone()
            if resume is None or not resume["text"]:
                raise ValueError("简历原文未解析，暂不能确认事实")
            if report_id is not None:
                report = conn.execute("SELECT resume_id FROM course_reports WHERE id=?", (report_id,)).fetchone()
                if report is None or report["resume_id"] != resume_id:
                    raise ValueError("报告与简历不一致")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM course_resume_versions WHERE resume_id=?",
                (resume_id,),
            ).fetchone()[0]
            return self._insert_on(conn, "resume_versions", dict(
                resume_id=resume_id, version=version, facts_json=encode(facts),
                report_id=report_id, confirmed_at=stamp(), origin=origin, extraction_task_id=extraction_task_id,
            ))

    def confirm_preferences(self, resume_version_id: int, data: dict) -> int:
        return self._insert("preferences", dict(
            resume_version_id=resume_version_id, data_json=encode(data), confirmed_at=stamp(),
        ))

    def latest_profile_version(self) -> dict | None:
        with self.connect() as conn:
            return decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())

    def latest_delivery_profile(self) -> dict | None:
        with self.connect() as conn:
            return decode(conn.execute(
                "SELECT * FROM course_resume_delivery_profiles "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone())

    def confirm_delivery_profile(self, values: object) -> int:
        """Save an immutable delivery-header version, reusing an identical one."""
        profile = validate_delivery_profile(values)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_resume_delivery_profiles "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone())
            if latest and latest["data"] == profile:
                return int(latest["id"])
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 "
                "FROM course_resume_delivery_profiles"
            ).fetchone()[0]
            return self._insert_on(conn, "resume_delivery_profiles", dict(
                version=version,
                data_json=encode(profile),
                confirmed_at=stamp(),
            ))

    def get_profile_version(self, version_id: int) -> dict | None:
        return self.get("profile_versions", int(version_id))

    @staticmethod
    def _insert_profile_version(conn: sqlite3.Connection, data: object) -> int:
        profile = normalize_profile_data(data)
        version = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM course_profile_versions"
        ).fetchone()[0]
        return Store._insert_on(conn, "profile_versions", dict(
            version=version, data_json=encode(profile), created_at=stamp(),
        ))

    def confirm_profile(self, data: object) -> int:
        """Save one immutable profile snapshot and return its version ID."""
        profile = normalize_profile_data(data)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            if latest and normalize_profile_data(latest["data"]) == profile:
                return latest["id"]
            return self._insert_profile_version(conn, profile)

    def set_profile_avatar(self, avatar: object) -> int:
        """Attach one sanitized avatar file to a new immutable profile version."""
        normalized = normalize_profile_data({"avatar": avatar})["avatar"]
        if normalized is None:
            raise ValueError("头像信息无效，请重新上传")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            if profile["avatar"] == normalized:
                return latest["id"]
            profile["avatar"] = normalized
            return self._insert_profile_version(conn, profile)

    def delete_profile_avatar(self) -> int:
        """Remove the avatar reference while retaining historical profile files."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            if profile["avatar"] is None:
                if latest:
                    return latest["id"]
                raise ValueError("当前资料库没有头像")
            profile["avatar"] = None
            return self._insert_profile_version(conn, profile)

    def add_profile_item(self, section: str, values: dict) -> int:
        item = {"id": uuid.uuid4().hex, **validate_profile_item(section, values)}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            profile[section].append(item)
            return self._insert_profile_version(conn, profile)

    def update_profile_item(self, section: str, item_id: str, values: dict) -> int:
        validated = validate_profile_item(section, values)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            for index, item in enumerate(profile[section]):
                if item.get("id") == item_id:
                    updated = {"id": item_id, **validated}
                    if item == updated:
                        return latest["id"]
                    profile[section][index] = updated
                    break
            else:
                raise ValueError("资料条目不存在，请刷新后重试")
            return self._insert_profile_version(conn, profile)

    def delete_profile_item(self, section: str, item_id: str) -> int:
        if section not in normalize_profile_data({}):
            raise ValueError("未知资料分类")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            remaining = [item for item in profile[section] if item.get("id") != item_id]
            if len(remaining) == len(profile[section]):
                raise ValueError("资料条目不存在，请刷新后重试")
            profile[section] = remaining
            return self._insert_profile_version(conn, profile)

    @staticmethod
    def _profile_item_key(section: str, item: dict) -> tuple[str, ...]:
        def clean(key: str) -> str:
            return re.sub(r"\s+", " ", str(item.get(key) or "")).strip().casefold()

        if section == "skills":
            return (clean("name"),)
        if section == "experiences":
            return (clean("type"), clean("role"), clean("organization"), clean("start_date"))
        if section == "certificates":
            return (clean("name"), clean("issuer"))
        return (clean("title"), clean("content"))

    def merge_profile_items(self, imported: object) -> dict:
        """Merge structured profile rows into one new immutable profile version."""
        source = imported if isinstance(imported, dict) else {}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = decode(conn.execute(
                "SELECT * FROM course_profile_versions ORDER BY version DESC LIMIT 1"
            ).fetchone())
            profile = normalize_profile_data(latest["data"] if latest else {})
            known = {
                section: {self._profile_item_key(section, item) for item in profile[section]}
                for section in PROFILE_SECTIONS
            }
            added = {section: 0 for section in PROFILE_SECTIONS}
            duplicates = {section: 0 for section in PROFILE_SECTIONS}
            invalid = {section: 0 for section in PROFILE_SECTIONS}
            for section in PROFILE_SECTIONS:
                rows = source.get(section)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        invalid[section] += 1
                        continue
                    try:
                        values = validate_profile_item(section, row)
                    except ValueError:
                        invalid[section] += 1
                        continue
                    item = {"id": uuid.uuid4().hex, **values}
                    key = self._profile_item_key(section, item)
                    if key in known[section]:
                        duplicates[section] += 1
                        continue
                    profile[section].append(item)
                    known[section].add(key)
                    added[section] += 1
            total_added = sum(added.values())
            version_id = latest["id"] if latest and not total_added else self._insert_profile_version(conn, profile)
            return {
                "profile_version_id": version_id,
                "added": added,
                "added_total": total_added,
                "duplicates": duplicates,
                "duplicates_total": sum(duplicates.values()),
                "invalid": invalid,
                "invalid_total": sum(invalid.values()),
            }

    def snapshot_job(self, job_uid: str, jd: str, is_full: bool = False, metadata: dict | None = None, *, fetched: bool = False) -> int:
        job = self.get("jobs", job_uid)
        if job is None:
            raise ValueError("岗位不存在")
        if is_full and not jd.strip():
            raise ValueError("完整岗位描述不能为空")
        values = {key: job[key] for key in ("job_uid", "title", "company", "source", "external_id", "url")}
        if metadata is not None:
            for key in ("title", "company", "source", "external_id", "url"):
                if not isinstance(metadata.get(key), str) or (key in {"title", "source"} and not metadata[key].strip()):
                    raise ValueError(f"岗位快照缺少 {key}")
                values[key] = metadata[key]
        if values["source"] == "manual":
            values["external_id"] = ""
        values.update(jd=jd, is_full=int(is_full))
        fingerprint = hashlib.sha256(encode(values).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if fetched:
                conn.execute("UPDATE jobs SET last_fetched_at=? WHERE job_uid=?", (stamp(), job_uid))
            existing = conn.execute(
                "SELECT id FROM course_job_snapshots WHERE job_uid=? AND fingerprint=?",
                (job_uid, fingerprint),
            ).fetchone()
            if existing:
                return existing["id"]
            counter = conn.execute("SELECT value FROM kv_store WHERE key='course_snapshot_sequence'").fetchone()
            sequence = max(int(counter[0]) if counter else 0,
                           conn.execute("SELECT COALESCE(MAX(id),0) FROM course_job_snapshots").fetchone()[0]) + 1
            conn.execute("INSERT OR REPLACE INTO kv_store(key,value) VALUES('course_snapshot_sequence',?)", (str(sequence),))
            return self._insert_on(conn, "job_snapshots", dict(
                id=sequence, **values, fingerprint=fingerprint, created_at=stamp(),
            ))

    def toggle_favorite(self, snapshot_id: int) -> bool:
        """Bookmark one job for the personal profile; returns the new state."""
        snapshot = self.get("job_snapshots", snapshot_id)
        if snapshot is None:
            raise ValueError("岗位不存在")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM course_favorites WHERE job_uid=?", (snapshot["job_uid"],)
            ).fetchone()
            if existing:
                conn.execute("DELETE FROM course_favorites WHERE job_uid=?", (snapshot["job_uid"],))
                return False
            conn.execute(
                "INSERT INTO course_favorites(job_uid,snapshot_id,created_at) VALUES(?,?,?)",
                (snapshot["job_uid"], snapshot_id, stamp()),
            )
            return True

    def is_favorite(self, job_uid: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM course_favorites WHERE job_uid=?", (job_uid,)
            ).fetchone() is not None

    def list_favorites(self) -> list[dict]:
        """Favorites joined to their job record for the profile page."""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT f.id AS favorite_id, f.job_uid, f.snapshot_id, f.created_at,
                       s.title AS title, s.company AS company, s.source AS source,
                       s.url AS url, s.is_full AS is_full
                FROM course_favorites f
                JOIN course_job_snapshots s ON s.id=f.snapshot_id
                ORDER BY f.created_at DESC, f.id DESC
            """).fetchall()
            return [dict(row) for row in rows]

    def save_tailored_resume(
        self,
        snapshot_id: int,
        resume_id: int | None,
        title: str,
        body: str,
        resume_version_id: int | None = None,
        profile_version_id: int | None = None,
        delivery_profile_id: int | None = None,
        document: dict | None = None,
        audit: dict | None = None,
        plan: dict | None = None,
        boundary: dict | BoundaryEvaluation | None = None,
        pdf_path: str | None = None,
        pdf_sha256: str | None = None,
        content_sha256: str | None = None,
        template_version: str | None = None,
        renderer_version: str | None = None,
        page_count: int | None = None,
        artifact_status: str = "legacy",
    ) -> int:
        snapshot = self.get("job_snapshots", snapshot_id)
        if not snapshot:
            raise ValueError("岗位不存在")
        text = (body or "").strip()
        if len(text) < 40:
            raise ValueError("简历正文过短，未保存")
        if resume_id is not None:
            if self.get("resumes", int(resume_id)) is None:
                raise ValueError("对照简历不存在")
        if resume_version_id is not None:
            version = self.get("resume_versions", int(resume_version_id))
            if version is None or (resume_id is not None and version["resume_id"] != int(resume_id)):
                raise ValueError("简历事实版本与所选简历不一致")
        if profile_version_id is not None and self.get("profile_versions", int(profile_version_id)) is None:
            raise ValueError("职业资料版本不存在")
        if (
            delivery_profile_id is not None
            and self.get("resume_delivery_profiles", int(delivery_profile_id)) is None
        ):
            raise ValueError("投递信息版本不存在")
        heading = (title or "").strip() or f"{snapshot['title']} · 针对性简历"
        if artifact_status not in {"legacy", "audit_pass", "audit_issue"}:
            raise ValueError("未知简历产物状态")
        if plan is not None and not isinstance(plan, dict):
            raise ValueError("简历策略必须是结构化对象")
        evidence_refs = []
        if isinstance(audit, dict):
            evidence_refs = [
                fact_id
                for finding in audit.get("findings") or []
                if isinstance(finding, dict)
                for fact_id in finding.get("fact_ids") or []
            ]
        if boundary is None:
            boundary_evaluation = evaluation_from_audit(
                audit,
                legacy=artifact_status == "legacy",
                evidence_refs=evidence_refs,
            )
        else:
            boundary_evaluation = BoundaryEvaluation.from_dict(boundary)
        if artifact_status == "audit_pass" and boundary_evaluation.level is not RefusalLevel.ALLOW:
            raise ValueError("边界结论与审计通过状态不一致")
        if artifact_status == "audit_issue" and boundary_evaluation.level is not RefusalLevel.REVIEW:
            raise ValueError("边界结论与审计问题状态不一致")
        if artifact_status == "legacy" and boundary_evaluation.level is not RefusalLevel.REVIEW:
            raise ValueError("历史文件必须保留人工复核等级")
        if isinstance(audit, dict):
            if artifact_status == "audit_pass" and audit.get("approved") is not True:
                raise ValueError("审计结论未通过，不能保存为审计通过状态")
            if artifact_status == "audit_issue" and audit.get("approved") is not False:
                raise ValueError("审计结论有异议，不能保存为审计通过状态")
        if not boundary_evaluation.permission("save"):
            blocking = boundary_evaluation.blocking_checks[0]
            raise ValueError(
                f"边界评估为 {blocking.level.value.upper()}，禁止保存："
                f"{blocking.code} - {blocking.summary}"
            )
        if artifact_status != "legacy":
            if not isinstance(document, dict) or not isinstance(audit, dict):
                raise ValueError("PDF 简历缺少结构化文档或审计报告")
            if not pdf_path or len(str(pdf_path)) > 2000:
                raise ValueError("PDF 简历缺少有效文件路径")
            if not re.fullmatch(r"[0-9a-f]{64}", str(pdf_sha256 or "")):
                raise ValueError("PDF 文件指纹无效")
            if not re.fullmatch(r"[0-9a-f]{64}", str(content_sha256 or "")):
                raise ValueError("简历内容指纹无效")
            if not template_version or not renderer_version:
                raise ValueError("PDF 简历缺少渲染版本")
            if not isinstance(page_count, int) or page_count < 1:
                raise ValueError("PDF 页数无效")
        return self._insert("tailored_resumes", dict(
            snapshot_id=int(snapshot_id),
            resume_id=int(resume_id) if resume_id is not None else None,
            resume_version_id=int(resume_version_id) if resume_version_id is not None else None,
            profile_version_id=int(profile_version_id) if profile_version_id is not None else None,
            delivery_profile_id=(
                int(delivery_profile_id) if delivery_profile_id is not None else None
            ),
            title=heading[:200],
            body=text,
            document_json=encode(document) if document is not None else None,
            audit_json=encode(audit) if audit is not None else None,
            plan_json=encode(plan) if plan is not None else None,
            boundary_json=encode(boundary_evaluation.to_dict()),
            refusal_level=boundary_evaluation.level.value,
            pdf_path=pdf_path,
            pdf_sha256=pdf_sha256,
            content_sha256=content_sha256,
            template_version=template_version,
            renderer_version=renderer_version,
            page_count=page_count,
            artifact_status=artifact_status,
            created_at=stamp(),
        ))

    def list_tailored_resumes(self, job_uid: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM course_tailored_resumes t
                JOIN course_job_snapshots s ON s.id=t.snapshot_id
                WHERE s.job_uid=?
                  AND NOT EXISTS(
                    SELECT 1 FROM course_tailored_resume_deletions d
                    WHERE d.tailored_resume_id=t.id
                  )
                ORDER BY t.id DESC
                """,
                (job_uid,),
            ).fetchall()
            return [decode(row) for row in rows]

    def tailored_resume_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.job_uid AS job_uid, COUNT(t.id) AS n
                FROM course_tailored_resumes t
                JOIN course_job_snapshots s ON s.id=t.snapshot_id
                WHERE NOT EXISTS(
                    SELECT 1 FROM course_tailored_resume_deletions d
                    WHERE d.tailored_resume_id=t.id
                )
                GROUP BY s.job_uid
                """
            ).fetchall()
            return {row["job_uid"]: int(row["n"]) for row in rows}

    def matched_job_uids(self) -> set[str]:
        """Job UIDs that already have at least one match analysis report."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT s.job_uid AS job_uid
                FROM course_matches m
                JOIN course_job_snapshots s ON s.id=m.snapshot_id
                """
            ).fetchall()
            return {row["job_uid"] for row in rows}

    def get_tailored_resume(self, snapshot_id: int, record_id: int) -> dict | None:
        snapshot = self.get("job_snapshots", snapshot_id)
        item = self.get("tailored_resumes", record_id)
        if not snapshot or not item:
            return None
        owner = self.get("job_snapshots", item["snapshot_id"])
        if not owner or owner["job_uid"] != snapshot["job_uid"]:
            return None
        with self.connect() as conn:
            deleted = conn.execute(
                "SELECT 1 FROM course_tailored_resume_deletions "
                "WHERE tailored_resume_id=?",
                (int(record_id),),
            ).fetchone()
        if deleted:
            return None
        return item

    def delete_tailored_resume(
        self,
        snapshot_id: int,
        record_id: int,
        user_id: int,
    ) -> bool:
        """Remove a tailored resume from user-facing views without erasing evidence."""
        if self.get_user(int(user_id)) is None:
            raise ValueError("操作用户不存在")
        snapshot = self.get("job_snapshots", snapshot_id)
        item = self.get("tailored_resumes", record_id)
        if not snapshot or not item:
            return False
        owner = self.get("job_snapshots", item["snapshot_id"])
        if not owner or owner["job_uid"] != snapshot["job_uid"]:
            return False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM course_tailored_resume_deletions "
                "WHERE tailored_resume_id=?",
                (int(record_id),),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO course_tailored_resume_deletions("
                "tailored_resume_id,deleted_by,deleted_at) VALUES(?,?,?)",
                (int(record_id), int(user_id), stamp()),
            )
            return True

    def find_output_approval(
        self,
        artifact_type: str,
        artifact_id: int,
        content_hash: str,
        approval_scope: str,
    ) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM course_output_approvals "
                "WHERE artifact_type=? AND artifact_id=? AND content_hash=? AND approval_scope=? "
                "ORDER BY id DESC LIMIT 1",
                (artifact_type, int(artifact_id), content_hash, approval_scope),
            ).fetchone()
            return decode(row)

    def record_output_approval(
        self,
        artifact_type: str,
        artifact_id: int,
        artifact_version: int,
        content_hash: str,
        audit_snapshot: dict,
        approved_by: int,
        approval_scope: str,
    ) -> int:
        if self.get_user(approved_by) is None:
            raise ValueError("审批用户不存在")
        if artifact_type not in {"material", "tailored_resume"}:
            raise ValueError("未知输出类型")
        if approval_scope not in {"export", "download"}:
            raise ValueError("未知审批用途")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM course_output_approvals "
                "WHERE artifact_type=? AND artifact_id=? AND artifact_version=? "
                "AND content_hash=? AND approval_scope=?",
                (
                    artifact_type,
                    int(artifact_id),
                    int(artifact_version),
                    content_hash,
                    approval_scope,
                ),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = conn.execute(
                "INSERT INTO course_output_approvals("
                "artifact_type,artifact_id,artifact_version,content_hash,"
                "audit_snapshot_json,approved_by,approved_at,approval_scope"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    artifact_type,
                    int(artifact_id),
                    int(artifact_version),
                    content_hash,
                    encode(audit_snapshot),
                    int(approved_by),
                    stamp(),
                    approval_scope,
                ),
            )
            return int(cursor.lastrowid)

    def save_match(self, resume_version_id: int, snapshot_id: int, data: dict,
                   preference_id: int | None = None, task_id: int | None = None,
                   profile_version_id: int | None = None) -> int:
        if task_id is not None:
            existing = self.list("matches", task_id=task_id)
            if existing:
                return existing[0]["id"]
        if self.get("resume_versions", resume_version_id) is None:
            raise ValueError("简历事实版本不存在")
        if profile_version_id is None:
            latest_profile = self.latest_profile_version()
            profile_version_id = latest_profile["id"] if latest_profile else None
        if profile_version_id is None or self.get("profile_versions", profile_version_id) is None:
            raise ValueError("结构化资料版本不存在")
        snapshot = self.get("job_snapshots", snapshot_id)
        if not snapshot or not snapshot["is_full"]:
            raise ValueError("需要完整 JD 才能保存详细匹配")
        if preference_id is not None:
            preference = self.get("preferences", preference_id)
            if preference is None:
                raise ValueError("求职偏好与简历确认版本不一致")
        return self._insert("matches", dict(
            resume_version_id=resume_version_id, snapshot_id=snapshot_id,
            profile_version_id=profile_version_id,
            preference_id=preference_id, data_json=encode(data), task_id=task_id,
            created_at=stamp(),
        ))

    def save_material(self, match_id: int, data: dict, parent_id: int | None = None,
                      task_id: int | None = None) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if task_id is not None:
                existing = conn.execute("SELECT id FROM course_materials WHERE task_id=?", (task_id,)).fetchone()
                if existing:
                    return existing["id"]
            if parent_id is not None:
                parent = conn.execute("SELECT match_id FROM course_materials WHERE id=?", (parent_id,)).fetchone()
                if parent is None or parent["match_id"] != match_id:
                    raise ValueError("材料父版本与匹配记录不一致")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM course_materials WHERE match_id=?",
                (match_id,),
            ).fetchone()[0]
            return self._insert_on(conn, "materials", dict(
                match_id=match_id, version=version, data_json=encode(data),
                parent_id=parent_id, task_id=task_id, created_at=stamp(),
            ))

    def create_application(self, material_id: int, status: str = "saved") -> int:
        material = self.get("materials", material_id)
        if material is None:
            raise ValueError("材料不存在")
        match = self.get("matches", material["match_id"])
        return self.application_for_job(match["snapshot_id"], status, material_id=material_id)

    def application_for_job(self, snapshot_id, status="saved", material_id=None, note="", event_date=""):
        if status not in APPLICATION_STATUSES:
            raise ValueError("未知申请状态")
        from datetime import date
        event_date = event_date or stamp()[:10]
        date.fromisoformat(event_date)
        snapshot = self.get("job_snapshots", snapshot_id)
        if snapshot is None:
            raise ValueError("岗位不存在")
        version_id = None
        if material_id:
            material = self.get("materials", material_id)
            match = self.get("matches", material["match_id"]) if material else None
            if not match or match["snapshot_id"] != snapshot_id:
                raise ValueError("材料与岗位依据不一致")
            version_id = match["resume_version_id"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id FROM applications WHERE job_uid=? ORDER BY id LIMIT 1", (snapshot["job_uid"],)).fetchone()
            if existing:
                return existing["id"]
            app_id = self._insert_on(conn, "applications", dict(
                job_uid=snapshot["job_uid"], title=snapshot["title"], company=snapshot["company"],
                applied_date=event_date if status == "applied" else "",
                channel=snapshot["source"], resume_version=str(version_id or ""), status=status,
                course_snapshot_id=snapshot_id, course_resume_version_id=version_id,
                course_material_id=material_id, created_at=stamp(), updated_at=stamp()))
            self._insert_on(conn, "application_events", dict(application_id=app_id, status=status,
                note=note or "用户建立申请记录", event_date=event_date, created_at=stamp(), basis_json=encode({
                    "snapshot_id": snapshot_id, "resume_version_id": version_id, "material_id": material_id,
                    "usage": "actual" if status in {"applied", "interview", "offer", "rejected"} else "planned"})))
            basis = {"snapshot_id": snapshot_id, "resume_version_id": version_id, "material_id": material_id}
            column = "course_actual_json" if status == "applied" else "course_planned_json"
            conn.execute(f"UPDATE applications SET {column}=? WHERE id=?", (encode(basis), app_id))
            return app_id

    def prepare(self, snapshot_id, resume_id, preference_id, profile_version_id=None):
        versions = self.list("resume_versions", resume_id=resume_id) if resume_id else []
        version_id = versions[0]["id"] if versions else None
        if profile_version_id is None:
            latest_profile = self.latest_profile_version()
            profile_version_id = latest_profile["id"] if latest_profile else None
        if profile_version_id is None or self.get("profile_versions", profile_version_id) is None:
            raise ValueError("结构化资料版本不存在")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            original = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if original is None:
                raise ValueError("岗位不存在")
            cached = conn.execute("SELECT id FROM course_job_snapshots WHERE job_uid=? AND is_full=1 ORDER BY id DESC LIMIT 1",
                                  (original['job_uid'],)).fetchone() if not original['is_full'] else None
            selected_id = cached['id'] if cached else snapshot_id
            row = conn.execute("SELECT id FROM course_preparations WHERE original_snapshot_id=? AND snapshot_id=? "
                "AND resume_id IS ? AND resume_version_id IS ? AND preference_id IS ? "
                "AND profile_version_id IS ? ORDER BY id DESC LIMIT 1",
                (snapshot_id, selected_id, resume_id, version_id, preference_id,
                 profile_version_id)).fetchone()
            if row:
                return row["id"]
            return self._insert_on(conn, "preparations", dict(original_snapshot_id=snapshot_id,
                snapshot_id=selected_id, resume_id=resume_id, resume_version_id=version_id,
                preference_id=preference_id, profile_version_id=profile_version_id,
                created_at=stamp(), updated_at=stamp()))

    def update_preparation(self, preparation_id, **values):
        allowed = {"snapshot_id", "resume_id", "resume_version_id", "preference_id",
                   "profile_version_id", "match_id", "material_id", "state", "error"}
        if not set(values) <= allowed:
            raise ValueError("未知准备字段")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM course_preparations WHERE id=?", (preparation_id,)).fetchone()
            for key in ("resume_version_id", "preference_id", "profile_version_id"):
                if key in values and values[key] != old[key] and (key == "preference_id" or old[key] is not None):
                    raise ValueError("已固定的依据不能替换，请重新分析")
            if "snapshot_id" in values and values["snapshot_id"] != old["snapshot_id"]:
                snapshot = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (old["snapshot_id"],)).fetchone()
                new = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (values["snapshot_id"],)).fetchone()
                if snapshot["is_full"] or not new or snapshot["job_uid"] != new["job_uid"]:
                    raise ValueError("已固定的 JD 不能替换")
            values["updated_at"] = stamp()
            conn.execute("UPDATE course_preparations SET " + ",".join(k+"=?" for k in values) + " WHERE id=?", (*values.values(), preparation_id))

    def transition_application(self, application_id: int, status: str, note: str = "",
                               confirmed: bool = False, event_date: str = "") -> None:
        from datetime import date
        if status not in APPLICATION_STATUSES:
            raise ValueError("未知申请状态")
        event_date = event_date or stamp()[:10]
        date.fromisoformat(event_date)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            application = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
            if application is None:
                raise ValueError("申请不存在")
            last = conn.execute("SELECT status,note,event_date FROM course_application_events WHERE application_id=? ORDER BY id DESC LIMIT 1", (application_id,)).fetchone()
            if last and last["status"] == status and last["note"] == note and last["event_date"] == event_date:
                return
            applied_date = application["applied_date"]
            if status == "applied":
                applied_date = event_date
            conn.execute(
                "UPDATE applications SET status=?, applied_date=?, updated_at=? WHERE id=?",
                (status, applied_date, stamp(), application_id),
            )
            self._insert_on(conn, "application_events", dict(
                application_id=application_id, status=status, note=note, event_date=event_date, created_at=stamp(),
                basis_json=encode({**json.loads(application["course_actual_json"] if applied_date else application["course_planned_json"]),
                    "usage": "actual" if applied_date else "planned"}),
            ))

    def enqueue(self, kind: str, payload: dict, idempotency_key: str,
                max_attempts: int = 3, side_effect: bool = False, checkpoint=None, result=None, resume_preparation=None) -> int:
        if kind == "fill":
            raise ValueError("v2 已停用自动填写任务")
        if not kind or not idempotency_key or not 1 <= max_attempts <= 5:
            raise ValueError("任务类型、幂等键和 1–5 次尝试次数必须有效")
        payload_json = encode(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM course_tasks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if (existing["kind"] != kind or existing["payload_json"] != payload_json
                        or existing["side_effect"] != int(side_effect)
                        or existing["max_attempts"] != max_attempts):
                    raise ValueError("同一幂等键不能对应不同任务")
                if self.task_backend == "kafka" and existing["status"] == "queued":
                    self._insert_outbox_on(conn, existing)
                return existing["id"]
            if resume_preparation is not None:
                changed = conn.execute("UPDATE course_preparations SET state='queued',error='',updated_at=? "
                    "WHERE id=? AND state IN ('waiting_facts','waiting_jd','failed','problem_draft')",
                    (stamp(), resume_preparation)).rowcount
                if not changed:
                    raise ValueError("当前请求已在处理或完成，无需重复继续")
            task_id = self._insert_on(conn, "tasks", dict(
                kind=kind, payload_json=payload_json, idempotency_key=idempotency_key,
                checkpoint_json=encode(checkpoint) if checkpoint is not None else None,
                result_json=encode(result) if result is not None else None,
                max_attempts=max_attempts, side_effect=int(side_effect),
                available_at=stamp(), created_at=stamp(), updated_at=stamp(),
            ))
            if self.task_backend == "kafka":
                task = conn.execute("SELECT * FROM course_tasks WHERE id=?", (task_id,)).fetchone()
                self._insert_outbox_on(conn, task)
            return task_id

    @staticmethod
    def task_event(task: dict) -> dict:
        """Return the intentionally small, non-sensitive Kafka message body."""
        return {
            "schema_version": 1,
            "task_id": int(task["id"]),
            "kind": str(task["kind"]),
            "idempotency_key": str(task["idempotency_key"]),
        }

    def _insert_outbox_on(self, conn: sqlite3.Connection, task: sqlite3.Row | dict) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO course_task_outbox("
            "task_id,topic,event_key,status,attempts,available_at,created_at,updated_at) "
            "VALUES(?,?,?,'pending',0,?,?,?)",
            (
                int(task["id"]),
                self.task_topic,
                str(task["id"]),
                task["available_at"],
                task["created_at"],
                stamp(),
            ),
        )

    def ensure_queued_outbox(self) -> int:
        """Backfill queue rows created before Kafka was enabled."""
        now = stamp()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO course_task_outbox("
                "task_id,topic,event_key,status,attempts,available_at,created_at,updated_at) "
                "SELECT id,?,CAST(id AS TEXT),'pending',0,available_at,created_at,? "
                "FROM course_tasks WHERE status='queued' "
                "AND NOT EXISTS(SELECT 1 FROM course_task_outbox o WHERE o.task_id=course_tasks.id)",
                (self.task_topic, now),
            )
            return int(cursor.rowcount)

    def claim_outbox_batch(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("outbox 批次必须在 1–100 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM course_task_outbox "
                "WHERE status='pending' AND available_at<=? ORDER BY id LIMIT ?",
                (stamp(), limit),
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE course_task_outbox SET attempts=attempts+1,updated_at=? WHERE id=?",
                    [(stamp(), row["id"]) for row in rows],
                )
            return [decode(row) for row in rows]

    def mark_outbox_published(self, outbox_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE course_task_outbox SET status='published',published_at=?,"
                "last_error='',updated_at=? WHERE id=? AND status='pending'",
                (stamp(), stamp(), outbox_id),
            )

    def mark_outbox_failed(self, outbox_id: int, error: str, retry_seconds: int = 5) -> None:
        available = to_iso(now_hk() + timedelta(seconds=max(1, retry_seconds)))
        with self.connect() as conn:
            conn.execute(
                "UPDATE course_task_outbox SET last_error=?,available_at=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (str(error)[:2000], available, stamp(), outbox_id),
            )

    def outbox_pending_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM course_task_outbox WHERE status='pending'"
            ).fetchone()[0])

    def record_email_attempt(self, task_id: int, status: str, detail: str = "") -> int:
        return self._insert("email_attempts", dict(
            task_id=task_id, status=status, detail=detail, created_at=stamp(),
        ))

    def retry(self, task_id: int) -> int:
        """An explicit user retry creates a new attempt without rewriting history."""
        task = self.get("tasks", task_id)
        if task and task['kind'] == 'email' and self.list('email_attempts', task_id=task_id, status='accepted'):
            raise ValueError('SMTP 已有服务器接受记录，不能重复重试。')
        preview = (task is not None and task["kind"] == "email"
                   and task["status"] == "succeeded"
                   and isinstance(task["result"], dict)
                   and task["result"].get("status") == "preview")
        partial = task is not None and task["kind"] == "search" and any(
            r.get("status") == "failed" for r in (task.get("result") or {}).get("sources", {}).values())
        if task is None or (task["status"] not in {"failed", "uncertain"} and not preview and not partial):
            raise ValueError("只有失败、结果不确定或未发送的邮件预览可手动重试")
        checkpoint = dict(task.get("checkpoint") or {})
        if task["kind"] == "materials" and (task.get("result") or {}).get("material_id"):
            checkpoint["parent_id"] = task["result"]["material_id"]
        return self.enqueue(task["kind"], task["payload"], f"retry:{task_id}",
            max_attempts=task["max_attempts"], side_effect=bool(task["side_effect"]),
            checkpoint=checkpoint or None,
            result=task.get("result") if task["kind"] == "search" else None)

    def link_application_basis(self, application_id, version_id, material_id, note, usage=None):
        app = self.get('applications', application_id)
        if not app: raise ValueError('申请不存在')
        snapshot_id = app['course_snapshot_id']
        if version_id and not self.get('resume_versions', version_id): raise ValueError('事实版本不存在')
        if material_id:
            material = self.get('materials', material_id)
            match = self.get('matches', material['match_id']) if material else None
            snapshot = self.get('job_snapshots', match['snapshot_id']) if match else None
            if not snapshot or snapshot['job_uid'] != app['job_uid']:
                raise ValueError('材料与申请岗位不一致')
            if version_id and match['resume_version_id'] != version_id:
                raise ValueError('材料与事实版本不一致')
            version_id = match['resume_version_id']
            snapshot_id = match['snapshot_id']
        usage = usage or ('actual' if app['applied_date'] else 'planned')
        if usage not in {'planned', 'actual'}: raise ValueError('未知资料用途')
        basis = {'resume_version_id': version_id, 'material_id': material_id,
                 'snapshot_id': snapshot_id, 'usage': usage}
        events = self.list('application_events', application_id=application_id)
        if events and events[0].get('basis') == basis and events[0]['note'] == note: return
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._insert_on(c, 'application_events', dict(application_id=application_id, status=app['status'],
                note='关联修改前的依据', created_at=stamp(), basis_json=encode({**app[f'course_{usage}'], 'usage': usage})))
            c.execute(f'UPDATE applications SET course_{usage}_json=? WHERE id=?', (encode(basis), application_id))
            if usage == 'actual' or not app['course_actual']:
                c.execute('UPDATE applications SET course_resume_version_id=?,course_material_id=?,course_snapshot_id=?,updated_at=? WHERE id=?',
                          (version_id,material_id,snapshot_id,stamp(),application_id))
            self._insert_on(c, 'application_events', dict(application_id=application_id,status=app['status'],note=note,
                created_at=stamp(),basis_json=encode(basis)))

    def find_job_by_url(self, url_normalized: str) -> dict | None:
        """按归一化链接定位一条岗位；写入后回查用，避免依赖列表顺序。"""
        if not url_normalized:
            return None
        with self.connect() as conn:
            return decode(conn.execute(
                "SELECT * FROM jobs WHERE url_normalized=?", (url_normalized,)
            ).fetchone())

    def query_applications(self, *, status: str = "", keyword: str = "",
                           date_from: str = "", date_to: str = "",
                           limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """申请筛选：状态、关键词与日期区间，返回本页记录与总数。"""
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if keyword:
            where.append("(title LIKE ? OR company LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        day = "date(COALESCE(NULLIF(applied_date,''), substr(created_at,1,10)))"
        if date_from:
            where.append(f"{day} >= date(?)")
            params.append(date_from)
        if date_to:
            where.append(f"{day} <= date(?)")
            params.append(date_to)
        clause = " AND ".join(where) or "1"
        with self.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM applications WHERE {clause}", tuple(params)
            ).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM applications WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [decode(row) for row in rows], total

    def query_tasks(self, *, kind: str = "", status: str = "",
                    limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """任务列表只返回状态字段，不返回载荷，避免带出简历内容。"""
        where, params = [], []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if status:
            where.append("status=?")
            params.append(status)
        clause = " AND ".join(where) or "1"
        with self.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM course_tasks WHERE {clause}", tuple(params)
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT id,kind,status,attempts,max_attempts,side_effect,owner,error,"
                "available_at,started_at,finished_at,created_at,updated_at "
                f"FROM course_tasks WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], total

    def query_jobs(self, *, keyword: str = "", source: str = "",
                   limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """岗位列表：按关键词与来源筛选。"""
        where, params = [], []
        if keyword:
            where.append("(title LIKE ? OR company LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if source:
            where.append("source=?")
            params.append(source)
        clause = " AND ".join(where) or "1"
        with self.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {clause}", tuple(params)
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT job_uid,source,external_id,title,company,url,location,salary_text,"
                "status,first_seen_at,last_fetched_at,llm_score FROM jobs "
                f"WHERE {clause} ORDER BY first_seen_at DESC, job_uid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], total

    def query_application_board(self) -> list[dict]:
        """看板数据：每条申请一行，附带最后一次状态事件。"""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT a.id, a.title, a.company, a.status, a.applied_date,
                       a.created_at, a.updated_at,
                       e.status AS stage_status, e.event_date AS stage_event_date,
                       e.created_at AS stage_changed_at
                FROM applications a
                LEFT JOIN course_application_events e ON e.id = (
                    SELECT x.id FROM course_application_events x
                    WHERE x.application_id = a.id ORDER BY x.id DESC LIMIT 1)
                ORDER BY a.id DESC
            """).fetchall()
        return [dict(row) for row in rows]

    def board_summary(self) -> dict:
        """看板汇总：只输出计数，不含任何个人材料。"""
        today = now_hk().date().isoformat()
        with self.connect() as conn:
            applications = {
                row[0]: int(row[1]) for row in conn.execute(
                    "SELECT status, COUNT(*) FROM applications GROUP BY status")
            }
            tasks = {
                row[0]: int(row[1]) for row in conn.execute(
                    "SELECT status, COUNT(*) FROM course_tasks GROUP BY status")
            }
            jobs_total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            jobs_today = int(conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE substr(first_seen_at,1,10)=?", (today,)
            ).fetchone()[0])
            counts = {}
            for key, table in (
                ("resumes", "course_resumes"),
                ("reports", "course_reports"),
                ("snapshots", "course_job_snapshots"),
                ("materials", "course_materials"),
                ("favorites", "course_favorites"),
                ("tailored_resumes", "course_tailored_resumes"),
                ("shares", "course_share_tokens"),
                ("application_packs", "course_application_packs"),
            ):
                counts[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return {
            "applications_total": sum(applications.values()),
            "applications_by_status": applications,
            "tasks_total": sum(tasks.values()),
            "tasks_by_status": tasks,
            "jobs_total": jobs_total,
            "jobs_today": jobs_today,
            **counts,
        }

    def create_share_token(self, token_hash: str, token_hint: str, scope: str,
                           subject_type: str, subject_id: int | None,
                           summary: dict, expires_at: str | None, base_url: str = "") -> int:
        """登记一条分享令牌；只持久化摘要与站点根地址，明文令牌不落库。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM course_share_tokens WHERE token_hash=?", (token_hash,)
            ).fetchone():
                raise ValueError("分享令牌冲突，请重新生成")
            return self._insert_on(conn, "share_tokens", dict(
                token_hash=token_hash, token_hint=token_hint, scope=scope,
                subject_type=subject_type, subject_id=subject_id,
                base_url=str(base_url or "").rstrip("/"),
                summary_json=encode(summary or {}), created_at=stamp(),
                expires_at=expires_at, revoked_at=None, access_count=0,
                last_accessed_at=None,
            ))

    def get_share_token(self, token_hash: str) -> dict | None:
        with self.connect() as conn:
            return decode(conn.execute(
                "SELECT * FROM course_share_tokens WHERE token_hash=?", (token_hash,)
            ).fetchone())

    def list_share_tokens(self, *, include_revoked: bool = True) -> list[dict]:
        with self.connect() as conn:
            sql = "SELECT * FROM course_share_tokens"
            if not include_revoked:
                sql += " WHERE revoked_at IS NULL"
            sql += " ORDER BY id DESC"
            rows = conn.execute(sql).fetchall()
        return [decode(row) for row in rows]

    def revoke_share_token(self, token_hash: str) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE course_share_tokens SET revoked_at=? "
                "WHERE token_hash=? AND revoked_at IS NULL",
                (stamp(), token_hash),
            )
            return bool(cursor.rowcount)

    def touch_share_token(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE course_share_tokens SET access_count=access_count+1,last_accessed_at=? "
                "WHERE token_hash=?",
                (stamp(), token_hash),
            )

    def save_application_pack(self, application_id: int, data: dict) -> int:
        """保存一个不可变的投递信息包版本，历史版本不覆盖。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM course_application_packs WHERE application_id=?",
                (int(application_id),),
            ).fetchone()[0]
            return self._insert_on(conn, "application_packs", dict(
                application_id=int(application_id), version=version,
                data_json=encode(data), created_at=stamp(),
            ))

    def latest_application_pack(self, application_id: int) -> dict | None:
        with self.connect() as conn:
            return decode(conn.execute(
                "SELECT * FROM course_application_packs WHERE application_id=? "
                "ORDER BY version DESC LIMIT 1", (int(application_id),),
            ).fetchone())

    def list_application_packs(self, application_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM course_application_packs WHERE application_id=? ORDER BY version DESC",
                (int(application_id),),
            ).fetchall()
        return [decode(row) for row in rows]
