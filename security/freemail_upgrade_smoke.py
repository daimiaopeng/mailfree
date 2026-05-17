#!/usr/bin/env python3
"""
Freemail 旧库升级冒烟测试。

这个脚本不连接线上数据库，只用 Python 标准库 sqlite3 模拟 D1 的老表结构：
- 验证旧版 mailboxes 表缺字段时可以补齐。
- 验证 audit_logs 表和索引可以重复创建。
- 验证新接口依赖的代表性 SELECT/UPDATE/DELETE 语句不会因为缺列失败。

如果这里失败，说明旧部署直接升级有较高概率在启动或登录阶段报错。
"""

from __future__ import annotations

import sqlite3


REQUIRED_MAILBOX_COLUMNS = {
    "id",
    "address",
    "local_part",
    "domain",
    "password_hash",
    "created_at",
    "last_accessed_at",
    "expires_at",
    "is_pinned",
    "can_login",
    "forward_to",
    "is_favorite",
    "note",
    "tags",
    "purpose",
}


def execute_script(conn: sqlite3.Connection, sql: str) -> None:
    """执行 SQL 脚本；sqlite3 与 D1 都接受这些基础 DDL。"""
    conn.executescript(sql)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """读取表字段名。"""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """模拟运行时迁移：字段不存在才补字段。"""
    if column not in table_columns(conn, table):
        conn.execute(ddl)


def apply_runtime_migration(conn: sqlite3.Connection) -> None:
    """复刻 src/db/init.js 的关键兼容迁移。"""
    add_column_if_missing(conn, "mailboxes", "expires_at", "ALTER TABLE mailboxes ADD COLUMN expires_at TEXT")
    add_column_if_missing(conn, "mailboxes", "can_login", "ALTER TABLE mailboxes ADD COLUMN can_login INTEGER DEFAULT 0")
    add_column_if_missing(conn, "mailboxes", "forward_to", "ALTER TABLE mailboxes ADD COLUMN forward_to TEXT DEFAULT NULL")
    add_column_if_missing(conn, "mailboxes", "is_favorite", "ALTER TABLE mailboxes ADD COLUMN is_favorite INTEGER DEFAULT 0")
    add_column_if_missing(conn, "mailboxes", "note", "ALTER TABLE mailboxes ADD COLUMN note TEXT DEFAULT ''")
    add_column_if_missing(conn, "mailboxes", "tags", "ALTER TABLE mailboxes ADD COLUMN tags TEXT DEFAULT ''")
    add_column_if_missing(conn, "mailboxes", "purpose", "ALTER TABLE mailboxes ADD COLUMN purpose TEXT DEFAULT ''")

    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_role TEXT,
          actor_user_id INTEGER,
          actor_username TEXT,
          action TEXT NOT NULL,
          target_type TEXT,
          target_id TEXT,
          target_address TEXT,
          metadata TEXT,
          ip TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_mailboxes_address ON mailboxes(address);
        CREATE INDEX IF NOT EXISTS idx_mailboxes_is_favorite ON mailboxes(is_favorite DESC);
        CREATE INDEX IF NOT EXISTS idx_mailboxes_domain ON mailboxes(domain);
        CREATE INDEX IF NOT EXISTS idx_mailboxes_expires_at ON mailboxes(expires_at);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action, created_at DESC);
        """,
    )
    conn.commit()


def create_required_related_tables(conn: sqlite3.Connection) -> None:
    """创建代表性关联表，方便跑新列表查询。"""
    execute_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT,
          role TEXT NOT NULL DEFAULT 'user',
          can_send INTEGER NOT NULL DEFAULT 0,
          mailbox_limit INTEGER NOT NULL DEFAULT 10,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_mailboxes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          mailbox_id INTEGER NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          is_pinned INTEGER NOT NULL DEFAULT 0,
          UNIQUE(user_id, mailbox_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mailbox_id INTEGER NOT NULL,
          sender TEXT NOT NULL,
          to_addrs TEXT NOT NULL DEFAULT '',
          subject TEXT NOT NULL,
          verification_code TEXT,
          preview TEXT,
          r2_bucket TEXT NOT NULL DEFAULT 'mail-eml',
          r2_object_key TEXT NOT NULL DEFAULT '',
          received_at TEXT DEFAULT CURRENT_TIMESTAMP,
          is_read INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sent_emails (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          resend_id TEXT,
          from_name TEXT,
          from_addr TEXT NOT NULL,
          to_addrs TEXT NOT NULL,
          subject TEXT NOT NULL,
          html_content TEXT,
          text_content TEXT,
          status TEXT DEFAULT 'queued',
          scheduled_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
    )


def assert_new_queries_work(conn: sqlite3.Connection) -> None:
    """执行新版本常用查询，确认升级后的旧库不会缺列。"""
    conn.execute(
        """
        SELECT id, address, is_favorite, forward_to, can_login, note, tags, purpose, expires_at
        FROM mailboxes
        WHERE address = ?
        LIMIT 1
        """,
        ("old@example.com",),
    ).fetchall()

    conn.execute(
        """
        SELECT m.id, m.address, m.created_at, COALESCE(um.is_pinned, 0) AS is_pinned,
               COALESCE(m.can_login, 0) AS can_login,
               m.forward_to, COALESCE(m.is_favorite, 0) AS is_favorite,
               m.note, m.tags, m.purpose, m.expires_at
        FROM mailboxes m
        LEFT JOIN user_mailboxes um ON m.id = um.mailbox_id AND um.user_id = ?
        WHERE (m.address LIKE ? OR COALESCE(m.note, '') LIKE ? OR COALESCE(m.tags, '') LIKE ? OR COALESCE(m.purpose, '') LIKE ?)
        ORDER BY COALESCE(um.is_pinned, 0) DESC, m.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (1, "%old%", "%old%", "%old%", "%old%", 20, 0),
    ).fetchall()

    conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM mailboxes
        WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')
        """
    ).fetchone()

    conn.execute(
        """
        INSERT INTO audit_logs (actor_role, action, target_type, target_address)
        VALUES (?, ?, ?, ?)
        """,
        ("user", "upgrade.smoke", "mailbox", "old@example.com"),
    )

    conn.execute(
        """
        SELECT id, actor_role, action, target_type, target_address, created_at
        FROM audit_logs
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 10
        """
    ).fetchall()


def run_case(name: str, mailbox_schema: str) -> None:
    """运行一个旧库结构场景。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    execute_script(conn, mailbox_schema)
    create_required_related_tables(conn)
    conn.execute(
        """
        INSERT INTO mailboxes (address, local_part, domain, password_hash)
        VALUES ('old@example.com', 'old', 'example.com', NULL)
        """
    )
    conn.commit()

    apply_runtime_migration(conn)
    apply_runtime_migration(conn)

    columns = table_columns(conn, "mailboxes")
    missing = REQUIRED_MAILBOX_COLUMNS - columns
    if missing:
        raise AssertionError(f"{name}: mailboxes 缺少字段 {sorted(missing)}")
    if "audit_logs" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        raise AssertionError(f"{name}: audit_logs 表未创建")

    assert_new_queries_work(conn)
    conn.close()


def main() -> int:
    old_schema_without_new_fields = """
    CREATE TABLE mailboxes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      address TEXT NOT NULL UNIQUE,
      local_part TEXT NOT NULL,
      domain TEXT NOT NULL,
      password_hash TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      last_accessed_at TEXT,
      is_pinned INTEGER DEFAULT 0
    );
    """

    old_schema_with_partial_fields = """
    CREATE TABLE mailboxes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      address TEXT NOT NULL UNIQUE,
      local_part TEXT NOT NULL,
      domain TEXT NOT NULL,
      password_hash TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      last_accessed_at TEXT,
      expires_at TEXT,
      is_pinned INTEGER DEFAULT 0,
      can_login INTEGER DEFAULT 0,
      forward_to TEXT DEFAULT NULL
    );
    """

    current_schema_without_audit = """
    CREATE TABLE mailboxes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      address TEXT NOT NULL UNIQUE,
      local_part TEXT NOT NULL,
      domain TEXT NOT NULL,
      password_hash TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      last_accessed_at TEXT,
      expires_at TEXT,
      is_pinned INTEGER DEFAULT 0,
      can_login INTEGER DEFAULT 0,
      forward_to TEXT DEFAULT NULL,
      is_favorite INTEGER DEFAULT 0,
      note TEXT DEFAULT '',
      tags TEXT DEFAULT '',
      purpose TEXT DEFAULT ''
    );
    """

    cases = {
        "旧版缺新增字段": old_schema_without_new_fields,
        "旧版部分字段": old_schema_with_partial_fields,
        "当前表缺审计表": current_schema_without_audit,
    }
    for name, schema in cases.items():
        run_case(name, schema)
        print(f"PASS {name}")
    print("旧部署升级冒烟测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
