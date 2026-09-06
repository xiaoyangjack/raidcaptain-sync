"""
数据库初始化 - RaidCaptain Sync Server v3.1
启动时确保表/索引存在。与 M2.5 原 schema 完全兼容 + 新增模块化表。
"""
from raidcaptain_sync.config import settings


SCHEMA = """
-- ═══════════════════════════════════════════════════════════════
--  M2.5 兼容表（保持不变，确保向后兼容）
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS family(
    id TEXT PRIMARY KEY,
    pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
    parent_token TEXT, parent_token_exp INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS device(
    token_hash TEXT PRIMARY KEY,
    family_id TEXT NOT NULL, name TEXT NOT NULL,
    last_seen INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task(
    task_id TEXT NOT NULL, family_id TEXT NOT NULL,
    title TEXT NOT NULL, due_time TEXT NOT NULL,
    days_mask INTEGER NOT NULL DEFAULT 127,
    priority TEXT NOT NULL DEFAULT 'MED',
    mandatory INTEGER NOT NULL DEFAULT 0,
    merit_reward INTEGER NOT NULL DEFAULT 0, merit_penalty INTEGER NOT NULL DEFAULT 0,
    points_reward INTEGER NOT NULL DEFAULT 0, points_penalty INTEGER NOT NULL DEFAULT 0,
    require_evidence INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL,
    PRIMARY KEY(family_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_revision(
    family_id TEXT PRIMARY KEY, rev INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS event(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL, device_name TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS template(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL, template_id TEXT NOT NULL,
    name TEXT NOT NULL, title TEXT NOT NULL, due_time TEXT NOT NULL,
    days_mask INTEGER NOT NULL DEFAULT 127,
    priority TEXT NOT NULL DEFAULT 'MED',
    mandatory INTEGER NOT NULL DEFAULT 0,
    merit_reward INTEGER NOT NULL DEFAULT 0, merit_penalty INTEGER NOT NULL DEFAULT 0,
    points_reward INTEGER NOT NULL DEFAULT 0, points_penalty INTEGER NOT NULL DEFAULT 0,
    require_evidence INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(family_id, template_id)
);
CREATE TABLE IF NOT EXISTS appeal(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL, device_name TEXT NOT NULL,
    session_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    evidence_photo TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT 'CAUGHT',
    submitted_at INTEGER NOT NULL,
    reviewed_at INTEGER,
    result TEXT NOT NULL DEFAULT 'PENDING',
    reviewed_by TEXT,
    UNIQUE(family_id, session_id)
);
CREATE TABLE IF NOT EXISTS evidence_file(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    event_id INTEGER NOT NULL DEFAULT 0,
    task_id TEXT NOT NULL DEFAULT '',
    task_title TEXT NOT NULL DEFAULT '',
    device_name TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'image/jpeg',
    data_b64 TEXT NOT NULL DEFAULT '',
    oss_key TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    appeal_session_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS patrol_session(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    started_at INTEGER NOT NULL,
    ended_at INTEGER NOT NULL,
    valid_minutes INTEGER NOT NULL DEFAULT 0,
    points_delta INTEGER NOT NULL DEFAULT 0,
    merit_delta INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0,
    task_name TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT ''
);

-- ═══════════════════════════════════════════════════════════════
--  v3.1 新增：模块化基础设施
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS module_revision(
    family_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(family_id, module_id)
);
CREATE TABLE IF NOT EXISTS module_info(
    module_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0.0',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    registered_at INTEGER NOT NULL,
    config TEXT NOT NULL DEFAULT '{}'
);

-- ═══════════════════════════════════════════════════════════════
--  v3.1 新增：故事线 Bundle 系统
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS storyline_bundle(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    total_chapters INTEGER NOT NULL DEFAULT 0,
    total_episodes INTEGER NOT NULL DEFAULT 0,
    thumbnail_key TEXT NOT NULL DEFAULT '',
    bundle_json TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    UNIQUE(family_id, bundle_id, version)
);
CREATE TABLE IF NOT EXISTS storyline_progress(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    device_token_hash TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    current_chapter TEXT NOT NULL DEFAULT '',
    completed_episodes TEXT NOT NULL DEFAULT '[]',
    unlocked_chapters TEXT NOT NULL DEFAULT '[]',
    downloaded_at INTEGER NOT NULL,
    last_progress_at INTEGER NOT NULL,
    UNIQUE(family_id, bundle_id, device_token_hash)
);

-- ═══════════════════════════════════════════════════════════════
--  v3.1 新增：成就系统
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS achievement(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    achievement_id TEXT NOT NULL UNIQUE,
    module_id TEXT NOT NULL DEFAULT 'global',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon_key TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    rarity TEXT NOT NULL DEFAULT 'common',
    trigger_config TEXT NOT NULL,
    reward_merit INTEGER NOT NULL DEFAULT 0,
    reward_points INTEGER NOT NULL DEFAULT 0,
    reward_items TEXT NOT NULL DEFAULT '[]',
    display_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS family_achievement(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    target INTEGER NOT NULL DEFAULT 1,
    unlocked_at INTEGER,
    claimed INTEGER NOT NULL DEFAULT 0,
    claimed_at INTEGER,
    UNIQUE(family_id, achievement_id)
);

-- ═══════════════════════════════════════════════════════════════
--  v3.1 新增：公告/通知
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS announcement(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    read INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);

-- ═══════════════════════════════════════════════════════════════
--  v3.3 新增：Admin Dashboard 后端
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS admin(
    admin_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'editor',
    is_active INTEGER NOT NULL DEFAULT 1,
    last_login_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_audit_log(
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    family_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

-- 剧情订阅表：admin 可将 Bundle 分发给指定家庭
CREATE TABLE IF NOT EXISTS storyline_subscription(
    family_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    auto_download INTEGER NOT NULL DEFAULT 1,
    distributed_at INTEGER NOT NULL,
    PRIMARY KEY(family_id, bundle_id, version)
);

-- ═══════════════════════════════════════════════════════════════
--  索引：核心查询优化
-- ═══════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_event_family_created
    ON event(family_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_family_kind
    ON event(family_id, kind);
CREATE INDEX IF NOT EXISTS idx_task_family_active
    ON task(family_id, active);
CREATE INDEX IF NOT EXISTS idx_evidence_family_created
    ON evidence_file(family_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_task
    ON evidence_file(family_id, task_id);
CREATE INDEX IF NOT EXISTS idx_evidence_appeal
    ON evidence_file(family_id, appeal_session_id);
CREATE INDEX IF NOT EXISTS idx_patrol_family_started
    ON patrol_session(family_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_appeal_family_result
    ON appeal(family_id, result);

-- v3.1 新增索引
CREATE INDEX IF NOT EXISTS idx_module_revision_family
    ON module_revision(family_id, module_id);
CREATE INDEX IF NOT EXISTS idx_storyline_family_active
    ON storyline_bundle(family_id, active DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_storyline_progress_bundle
    ON storyline_progress(family_id, bundle_id);
CREATE INDEX IF NOT EXISTS idx_achievement_active_order
    ON achievement(active, display_order);
CREATE INDEX IF NOT EXISTS idx_family_achievement_family
    ON family_achievement(family_id, claimed, unlocked_at DESC);
CREATE INDEX IF NOT EXISTS idx_announcement_family_created
    ON announcement(family_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_announcement_unread
    ON announcement(family_id, read, created_at DESC);

-- v3.3 索引
CREATE INDEX IF NOT EXISTS idx_audit_admin
    ON admin_audit_log(admin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_subscription_bundle
    ON storyline_subscription(bundle_id, family_id);
"""
# Migration: sync legacy task_revision -> new module_revision
MIGRATION_SYNC_TASK_REVISION = """
INSERT OR IGNORE INTO module_revision(family_id, module_id, rev, updated_at)
SELECT family_id, 'tasks', rev, strftime('%s','now')*1000 FROM task_revision
WHERE NOT EXISTS (
    SELECT 1 FROM module_revision
    WHERE module_revision.family_id = task_revision.family_id
      AND module_revision.module_id = 'tasks'
);
"""

# v3.3 single ALTER (SQLite executescript does not support ALTER in one tx)
BUNDLE_COL_MIGRATIONS = [
    "ALTER TABLE storyline_bundle ADD COLUMN version TEXT NOT NULL DEFAULT '1.0.0'",
    "ALTER TABLE storyline_bundle ADD COLUMN title TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE storyline_bundle ADD COLUMN story_graph TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE storyline_bundle ADD COLUMN reward_graph TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE storyline_bundle ADD COLUMN migration TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE storyline_bundle ADD COLUMN chapter_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE storyline_bundle ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE storyline_bundle ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 2",
    "ALTER TABLE storyline_bundle ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
]


def init_db() -> None:
    # Ensure DB dir/schema/indexes/migration ready.
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    try:
        conn.executescript(SCHEMA)
        # 迁移：从旧 task_revision 同步到新 module_revision（保留旧表，向后兼容）
        conn.executescript(MIGRATION_SYNC_TASK_REVISION)
        # v3.3 迁移：扩展 storyline_bundle 列（逐条 ALTER，失败忽略）
        for sql in BUNDLE_COL_MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass
        # v3.3.1 迁移：经济系统字段修正（SQLite 改默认 + 修正历史数据）
        _ECONOMIC_MIGRATIONS = [
            # 修正 store_item.price_currency 默认值（已在 reward_module 同步）
            # 如果历史数据 price_currency='points'，修正为 'merit'（Android 端 prize.points_cost 实际就是 merit）
            "UPDATE store_item SET price_currency='merit' WHERE price_currency='points'",
            # 修正 family_rank.required_currency 默认
            "UPDATE rank_def SET required_currency='discipline' WHERE required_currency='points'",
            "UPDATE rank_def SET required_currency='discipline' WHERE required_currency='merit'",
        ]
        for sql in _ECONOMIC_MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()