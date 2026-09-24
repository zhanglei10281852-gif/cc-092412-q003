from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "township.db"
_local = threading.local()

SCHEMA = r''' 
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_events(resource_type, resource_id);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS residents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    id_card TEXT NOT NULL UNIQUE,
    gender TEXT NOT NULL CHECK(gender IN ('男', '女')),
    birth_date TEXT NOT NULL,
    phone TEXT,
    address TEXT NOT NULL,
    village TEXT NOT NULL,
    household_head TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS affairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('户籍','社保','医保','低保','建房','计生','其他')),
    applicant_id INTEGER NOT NULL REFERENCES residents(id),
    description TEXT,
    status TEXT NOT NULL DEFAULT '待受理' CHECK(status IN ('待受理','办理中','待复核','已办结','已退回')),
    department_id INTEGER REFERENCES departments(id),
    handler TEXT,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 职责分离策略：按事务类别配置进入办结前是否必须经办、复核分离
CREATE TABLE IF NOT EXISTS affair_review_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 复核轮次：受控事务每提交一次办理结果开启一轮，退回重办另开新一轮，旧轮次保留
CREATE TABLE IF NOT EXISTS affair_review_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    affair_id INTEGER NOT NULL REFERENCES affairs(id) ON DELETE CASCADE,
    round_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_review','approved','returned')),
    handler_user_id INTEGER REFERENCES users(id),
    handler_name TEXT NOT NULL,
    handler_department_id INTEGER,
    result TEXT,
    submitted_at TEXT NOT NULL,
    reviewer_user_id INTEGER REFERENCES users(id),
    reviewer_name TEXT,
    review_opinion TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(affair_id, round_no)
);

CREATE INDEX IF NOT EXISTS idx_affair_rounds ON affair_review_rounds(affair_id, round_no);

-- 决定证据：经办提交、复核结论等不可变决定；同一轮同一阶段只允许一条有效决定
CREATE TABLE IF NOT EXISTS affair_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    affair_id INTEGER NOT NULL REFERENCES affairs(id) ON DELETE CASCADE,
    round_no INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL CHECK(stage IN ('handle','review')),
    decision TEXT NOT NULL CHECK(decision IN ('submitted','approved','returned')),
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    department_id INTEGER,
    opinion TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(affair_id, round_no, stage),
    UNIQUE(affair_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_affair_decisions ON affair_decisions(affair_id, id);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('通知','公告','政策','公示')),
    publisher TEXT NOT NULL,
    is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS petitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('投诉举报','意见建议','求助咨询','信息公开申请')),
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    demand TEXT,
    contact TEXT,
    is_anonymous INTEGER NOT NULL DEFAULT 0 CHECK(is_anonymous IN (0,1)),
    status TEXT NOT NULL DEFAULT '待签收' CHECK(status IN ('待签收','待分派','办理中','待审核','已办结','退回重办','复查中','复查完结')),
    department_id INTEGER REFERENCES departments(id),
    deadline TEXT,
    process_result TEXT,
    review_opinion TEXT,
    review_result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS petition_urges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    petition_id INTEGER NOT NULL REFERENCES petitions(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS petition_flow_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    petition_id INTEGER NOT NULL REFERENCES petitions(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    operator TEXT,
    remark TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, department_id, starts_at)
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status, available_at);
'''

PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看部门", "departments", "read"),
    ("departments.write", "维护部门", "departments", "write"),
    ("residents.read", "查看居民", "residents", "read"),
    ("residents.write", "维护居民", "residents", "write"),
    ("affairs.read", "查看事务", "affairs", "read"),
    ("affairs.write", "办理事务", "affairs", "write"),
    ("affairs.review", "复核事务", "affairs", "review"),
    ("affairs.policy.write", "配置事务职责分离", "affairs", "policy.write"),
    ("petitions.read", "查看信访", "petitions", "read"),
    ("petitions.write", "办理信访", "petitions", "write"),
    ("announcements.write", "维护公告", "announcements", "write"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
]

# 默认纳入职责分离控制的民政补助类事务类别及说明
DEFAULT_REVIEW_CATEGORIES = {
    "低保": "民政补助类事务，办结前须由不同人员完成经办与复核",
}


def database_path() -> Path:
    raw = os.getenv("TOWNSHIP_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def init_db() -> None:
    now = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        _migrate_affairs_status(connection)
        connection.executescript(SCHEMA)
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        connection.execute(
            "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES('administrator','系统管理员','拥有全部系统权限',1,?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES('clerk','综合经办员','可处理居民、事务与信访业务',1,?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES('auditor','审计查看员','只读查看业务与审计记录',1,?,?)",
            (now, now),
        )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, now),
        )
        # 民政补助类默认受控：低保事务进入办结前必须经办、复核分离
        for category, note in DEFAULT_REVIEW_CATEGORIES.items():
            connection.execute(
                "INSERT OR IGNORE INTO affair_review_policies(category,is_active,note,created_at,updated_at) VALUES(?,1,?,?,?)",
                (category, note, now, now),
            )


def _migrate_affairs_status(connection: sqlite3.Connection) -> None:
    """旧库 affairs.status 的 CHECK 约束不含“待复核”，需要在新 SCHEMA 建立前重建该表。"""
    columns = {
        str(row[1]).lower() for row in connection.execute("PRAGMA table_info(affairs)").fetchall()
    }
    if not columns:
        return
    check_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='affairs'"
    ).fetchone()[0]
    if "待复核" in check_sql:
        return
    connection.executescript(
        """
        ALTER TABLE affairs RENAME TO affairs_legacy;
        CREATE TABLE affairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('户籍','社保','医保','低保','建房','计生','其他')),
            applicant_id INTEGER NOT NULL REFERENCES residents(id),
            description TEXT,
            status TEXT NOT NULL DEFAULT '待受理' CHECK(status IN ('待受理','办理中','待复核','已办结','已退回')),
            department_id INTEGER REFERENCES departments(id),
            handler TEXT,
            result TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO affairs(id,title,category,applicant_id,description,status,department_id,handler,result,created_at,updated_at)
        SELECT id,title,category,applicant_id,description,status,department_id,handler,result,created_at,updated_at FROM affairs_legacy;
        DROP TABLE affairs_legacy;
        """
    )


def migrate_db() -> None:
    init_db()
