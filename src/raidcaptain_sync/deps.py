"""
依赖注入 - RaidCaptain Sync Server
get_db, auth_device, auth_parent 等全局依赖。
"""
import hashlib
import hmac
import secrets
import time
from typing import Generator, Optional

import sqlite3
from fastapi import Depends, Header, HTTPException

from raidcaptain_sync.config import settings


# ── 数据库连接 ─────────────────────────────────────────────────

def get_db() -> Generator[sqlite3.Connection, None, None]:
    """上下文管理器：每次请求打开一个 SQLite 连接。
    启用 WAL 模式，写并发更安全。check_same_thread=False 以支持
    FastAPI 依赖注入的线程池。"""
    conn = sqlite3.connect(
        str(settings.db_path),
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        settings.pbkdf2_iterations,
    ).hex()


# ── 认证 ────────────────────────────────────────────────────────


def auth_device(
    db: sqlite3.Connection,
    authorization: Optional[str] = Header(None),
) -> sqlite3.Row:
    """验证设备 Token，返回 device 行。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少设备令牌")
    token = authorization.removeprefix("Bearer ").strip()
    row = db.execute(
        "SELECT * FROM device WHERE token_hash=?",
        (sha256_hex(token),),
    ).fetchone()
    if not row:
        raise HTTPException(401, "设备令牌无效")
    db.execute(
        "UPDATE device SET last_seen=? WHERE token_hash=?",
        (int(time.time()), sha256_hex(token)),
    )
    db.commit()
    return row


def auth_parent(
    db: sqlite3.Connection,
    authorization: Optional[str] = Header(None),
) -> sqlite3.Row:
    """验证家长 Token，返回 family 行。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = authorization.removeprefix("Bearer ").strip()
    row = db.execute(
        "SELECT * FROM family WHERE parent_token=? AND parent_token_exp>?",
        (sha256_hex(token), int(time.time())),
    ).fetchone()
    if not row:
        raise HTTPException(401, "登录已过期，请重新登录")
    return row


# ── revision ───────────────────────────────────────────────────


def get_revision(db: sqlite3.Connection, family_id: str) -> int:
    row = db.execute(
        "SELECT rev FROM task_revision WHERE family_id=?",
        (family_id,),
    ).fetchone()
    return row["rev"] if row else 0


def bump_revision(db: sqlite3.Connection, family_id: str) -> int:
    row = db.execute(
        "SELECT rev FROM task_revision WHERE family_id=?",
        (family_id,),
    ).fetchone()
    rev = (row["rev"] + 1) if row else 1
    db.execute(
        "INSERT INTO task_revision(family_id, rev) VALUES(?,?) "
        "ON CONFLICT(family_id) DO UPDATE SET rev=excluded.rev",
        (family_id, rev),
    )
    return rev


# ── WebSocket 在线登记 ─────────────────────────────────────────

device_sockets: dict[str, list] = {}   # family_id -> [ws]
parent_sockets: dict[str, list] = {}   # family_id -> [ws]


async def ws_push(family_id: str, group: dict, message: dict):
    """向指定家庭的所有在线连接推送消息。"""
    for ws in list(group.get(family_id, [])):
        try:
            await ws.send_json(message)
        except Exception:
            try:
                group.get(family_id, []).remove(ws)
            except ValueError:
                pass
