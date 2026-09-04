#!/usr/bin/env python3
"""
锐察队长 · M2.5 同步服务器
==========================
孩子设备 ↔ 家长网页 的轻量同步中枢。

功能：
- 家庭注册 / 家长登录（PBKDF2 哈希，不存明文）
- 设备配对（孩子端 App 在家长模式下输入家庭码绑定，签发 device_token）
- 任务单下发（家长网页编辑 → 设备拉取，带 revision 版本号）
- 学习成果上报（任务完成/逾期、对局结算、申诉事件回流家长端）
- 设备在线状态（WebSocket 心跳，<90s 视为在线）
- 双向实时：家长改任务 → WS 推设备"tasks_changed"；设备上线/上报 → WS 推家长端

零第三方推送依赖、数据全部自持。启动：uvicorn main:app --host 0.0.0.0 --port 8000
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RAID_SYNC_DB", BASE_DIR / "sync.db"))
STATIC_DIR = BASE_DIR / "static"

PBKDF2_ITER = 120_000
ONLINE_WINDOW_S = 90

app = FastAPI(title="RaidCaptain Sync", version="1.0.0")

# ── 数据库 ──────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS family(
    id TEXT PRIMARY KEY,                -- 家庭码（8位数字字符串）
    pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
    parent_token TEXT, parent_token_exp INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS device(
    token_hash TEXT PRIMARY KEY,        -- sha256(token)
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
    data_b64 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    appeal_session_id TEXT NOT NULL DEFAULT ''
);
"""


_schema_ready = False


@contextmanager
def get_db():
    global _schema_ready
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_ready:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn.executescript(SCHEMA)
            _schema_ready = True
        yield conn
        conn.commit()
    finally:
        conn.close()


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITER).hex()


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

# ── WebSocket 在线登记 ─────────────────────────────────────────

device_sockets: Dict[str, List[WebSocket]] = {}     # family_id -> [ws]
parent_sockets: Dict[str, List[WebSocket]] = {}     # family_id -> [ws]


async def ws_push(family_id: str, group: Dict[str, List[WebSocket]], message: dict):
    for ws in list(group.get(family_id, [])):
        try:
            await ws.send_json(message)
        except Exception:
            try:
                group.get(family_id, []).remove(ws)
            except ValueError:
                pass

# ── 鉴权工具 ────────────────────────────────────────────────────


def auth_device(conn: sqlite3.Connection, authorization: Optional[str]) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少设备令牌")
    token = authorization.removeprefix("Bearer ").strip()
    row = conn.execute("SELECT * FROM device WHERE token_hash=?", (sha256(token),)).fetchone()
    if not row:
        raise HTTPException(401, "设备令牌无效")
    conn.execute("UPDATE device SET last_seen=? WHERE token_hash=?", (int(time.time()), row["token_hash"]))
    return row


def auth_parent(conn: sqlite3.Connection, authorization: Optional[str]) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = authorization.removeprefix("Bearer ").strip()
    row = conn.execute(
        "SELECT * FROM family WHERE parent_token=?", (sha256(token),)
    ).fetchone()
    if not row or (row["parent_token_exp"] or 0) < int(time.time()):
        raise HTTPException(401, "登录已过期，请重新登录")
    return row


def get_revision(conn: sqlite3.Connection, family_id: str) -> int:
    """只读：返回当前 revision，不递增。"""
    row = conn.execute("SELECT rev FROM task_revision WHERE family_id=?", (family_id,)).fetchone()
    return row["rev"] if row else 0


def bump_revision(conn: sqlite3.Connection, family_id: str) -> int:
    row = conn.execute("SELECT rev FROM task_revision WHERE family_id=?", (family_id,)).fetchone()
    rev = (row["rev"] + 1) if row else 1
    conn.execute(
        "INSERT INTO task_revision(family_id, rev) VALUES(?,?) "
        "ON CONFLICT(family_id) DO UPDATE SET rev=excluded.rev", (family_id, rev)
    )
    return rev

# ── 家长端 HTTP API ────────────────────────────────────────────


@app.post("/api/family/register")
def family_register(body: dict):
    """家长首次使用：注册家庭。返回家庭码（孩子端配对 + 家长登录都要用）。"""
    password = str(body.get("password") or "")
    if len(password) < 6:
        raise HTTPException(400, "家长密码至少 6 位")
    family_code = str(secrets.randbelow(10 ** 8)).zfill(8)
    salt = secrets.token_hex(16)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO family(id, pw_salt, pw_hash, created_at) VALUES(?,?,?,?)",
            (family_code, salt, hash_pw(password, salt), int(time.time())),
        )
    return {"family_code": family_code}


@app.post("/api/parent/login")
def parent_login(body: dict):
    code = str(body.get("family_code") or "")
    password = str(body.get("password") or "")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM family WHERE id=?", (code,)).fetchone()
        if not row or not hmac.compare_digest(row["pw_hash"], hash_pw(password, row["pw_salt"])):
            raise HTTPException(401, "家庭码或密码错误")
        token = secrets.token_hex(32)
        conn.execute(
            "UPDATE family SET parent_token=?, parent_token_exp=? WHERE id=?",
            (sha256(token), int(time.time()) + 30 * 86400, code),
        )
    return {"parent_token": token, "family_code": code}


@app.get("/api/parent/tasks")
def parent_list_tasks(authorization: Optional[str] = Header(None)):
    """家长端查询本家庭下发的所有任务（不过滤 active 状态，模板/历史都能看）。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        rows = conn.execute(
            "SELECT * FROM task WHERE family_id=? ORDER BY updated_at DESC", (fid,)
        ).fetchall()
        return {
            "revision": get_revision(conn, fid),
            "tasks": [_task_json(r) for r in rows],
        }


@app.post("/api/parent/tasks")
async def parent_push_tasks(body: dict, authorization: Optional[str] = Header(None)):
    """家长下发任务单（替换式：把传入的 tasks 数组落库，标记 active）。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > 200:
            raise HTTPException(400, "tasks 必须为 1~200 的数组")
        now = int(time.time() * 1000)
        # 先停掉所有旧的 active
        conn.execute("UPDATE task SET active=0 WHERE family_id=?", (fid,))
        last_task_id = None
        for t in tasks:
            task_id = str(t.get("task_id") or _new_task_id())
            conn.execute(
                """INSERT OR REPLACE INTO task(family_id, task_id, title, due_time, days_mask,
                    priority, mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
                    require_evidence, active, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (fid, task_id,
                 str(t.get("title",""))[:64],
                 str(t.get("due_time","20:00")),
                 int(t.get("days_mask", 127)),
                 str(t.get("priority","NORMAL"))[:16],
                 1 if t.get("mandatory") else 0,
                 int(t.get("merit_reward",0)),
                 int(t.get("merit_penalty",0)),
                 int(t.get("points_reward",0)),
                 int(t.get("points_penalty",0)),
                 1 if t.get("require_evidence") else 0,
                 now),
            )
            last_task_id = task_id
        rev = bump_revision(conn, fid)
    # 通过 WS 通知在线设备有任务更新
    await ws_push(fid, device_sockets, {
        "type": "tasks_updated", "revision": rev, "count": len(tasks)
    })
    return {"ok": True, "revision": rev, "task_id": last_task_id}


TASK_FIELDS = ("task_id", "title", "due_time", "days_mask", "priority", "mandatory",
               "merit_reward", "merit_penalty", "points_reward", "points_penalty",
               "require_evidence", "active", "updated_at")


def _task_json(row, deleted: bool = False) -> dict:
    return {
        "task_id": row["task_id"], "title": row["title"], "due_time": row["due_time"],
        "days_mask": row["days_mask"], "priority": row["priority"],
        "mandatory": bool(row["mandatory"]),
        "merit_reward": row["merit_reward"], "merit_penalty": row["merit_penalty"],
        "points_reward": row["points_reward"], "points_penalty": row["points_penalty"],
        "require_evidence": bool(row["require_evidence"]),
        "active": False if deleted else bool(row["active"]),
        "updated_at": row["updated_at"],
    }


@app.post("/api/tasks")
async def push_tasks(body: dict, authorization: Optional[str] = Header(None)):
    """家长整单下发（全量覆盖语义：不在名单中的旧任务自动停用）。
    下发成功后向在线设备推送 tasks_changed（秒级到达）。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > 200:
            raise HTTPException(400, "tasks 必须为 1~200 条的数组")
        now = int(time.time() * 1000)
        keep = set()
        for t in tasks:
            tid = str(t.get("task_id") or "").strip()
            title = str(t.get("title") or "").strip()
            due = str(t.get("due_time") or "").strip()
            if not tid or not title:
                raise HTTPException(400, "任务缺少 task_id/title")
            try:
                hh, mm = due.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
            except Exception:
                raise HTTPException(400, f"任务「{title}」到点时间格式应为 HH:mm")
            mask = int(t.get("days_mask", 127))
            if not (1 <= mask <= 127):
                raise HTTPException(400, f"任务「{title}」days_mask 非法")
            keep.add(tid)
            conn.execute(
                """INSERT INTO task(family_id, task_id, title, due_time, days_mask, priority,
                    mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
                    require_evidence, active, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(family_id, task_id) DO UPDATE SET
                    title=excluded.title, due_time=excluded.due_time, days_mask=excluded.days_mask,
                    priority=excluded.priority, mandatory=excluded.mandatory,
                    merit_reward=excluded.merit_reward, merit_penalty=excluded.merit_penalty,
                    points_reward=excluded.points_reward, points_penalty=excluded.points_penalty,
                    require_evidence=excluded.require_evidence, active=excluded.active,
                    updated_at=excluded.updated_at""",
                (fid, tid, title, due, mask, str(t.get("priority", "MED")),
                 1 if t.get("mandatory") else 0,
                 int(t.get("merit_reward", 0)), int(t.get("merit_penalty", 0)),
                 int(t.get("points_reward", 0)), int(t.get("points_penalty", 0)),
                 1 if t.get("require_evidence") else 0,
                 1 if t.get("active", True) else 0, now),
            )
        # 全量覆盖语义：不在本次名单中的任务 → 停用（孩子端会同步停用）
        for row in conn.execute("SELECT task_id FROM task WHERE family_id=? AND active=1", (fid,)).fetchall():
            if row["task_id"] not in keep:
                conn.execute("UPDATE task SET active=0, updated_at=? WHERE family_id=? AND task_id=?",
                             (now, fid, row["task_id"]))
        rev = bump_revision(conn, fid)
    # 提交后：向在线设备推送"任务单已变更"，设备端 WS 收到即触发对账
    await ws_push(fid, device_sockets, {"type": "tasks_changed"})
    return {"ok": True, "revision": rev}


@app.get("/api/events")
def list_events(since: int = 0, limit: int = 100, authorization: Optional[str] = Header(None)):
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        rows = conn.execute(
            "SELECT * FROM event WHERE family_id=? AND _id>? ORDER BY _id ASC LIMIT ?",
            (fam["id"], since, min(limit, 500)),
        ).fetchall()
        return {"events": [dict(r) for r in rows]}


def _seen_text(ts: int) -> str:
    """last_seen 的相对时间文案（家长端展示用）"""
    diff = int(time.time()) - ts
    if ts <= 0:
        return "从未上线"
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    return f"{diff // 86400} 天前"


@app.get("/api/status")
def status(authorization: Optional[str] = Header(None)):
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        devices = conn.execute(
            "SELECT name, last_seen FROM device WHERE family_id=?", (fam["id"],)
        ).fetchall()
        now = int(time.time())
        pending_rev = conn.execute(
            "SELECT rev FROM task_revision WHERE family_id=?", (fam["id"],)
        ).fetchone()
        tasks = conn.execute(
            "SELECT * FROM task WHERE family_id=? AND active=1 ORDER BY due_time", (fam["id"],)
        ).fetchall()
        pending_appeals = conn.execute(
            "SELECT COUNT(*) FROM appeal WHERE family_id=? AND result='PENDING'",
            (fam["id"],),
        ).fetchone()[0]
        return {
            "devices": [
                {"name": d["name"], "online": (now - d["last_seen"]) < ONLINE_WINDOW_S,
                 "last_seen": d["last_seen"], "last_seen_text": _seen_text(d["last_seen"])}
                for d in devices
            ],
            "revision": pending_rev["rev"] if pending_rev else 0,
            "active_tasks": len(tasks),
            "pending_appeals": pending_appeals,
        }

# ── 任务模板 API（M2.6）────────────────────────────────────────


def _template_json(row: sqlite3.Row) -> dict:
    return {
        "template_id": row["template_id"],
        "name": row["name"],
        "title": row["title"],
        "due_time": row["due_time"],
        "days_mask": row["days_mask"],
        "priority": row["priority"],
        "mandatory": bool(row["mandatory"]),
        "merit_reward": row["merit_reward"],
        "merit_penalty": row["merit_penalty"],
        "points_reward": row["points_reward"],
        "points_penalty": row["points_penalty"],
        "require_evidence": bool(row["require_evidence"]),
        "created_at": row["created_at"],
    }


@app.get("/api/templates")
def list_templates(authorization: Optional[str] = Header(None)):
    """列出本家庭所有任务模板。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        rows = conn.execute(
            "SELECT * FROM template WHERE family_id=? ORDER BY created_at DESC",
            (fam["id"],),
        ).fetchall()
        return {"templates": [_template_json(r) for r in rows]}


@app.post("/api/templates")
async def save_template(body: dict, authorization: Optional[str] = Header(None)):
    """保存（新建或更新）一个任务模板。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        template_id = str(body.get("template_id") or "").strip()
        name = str(body.get("name") or "").strip()
        title = str(body.get("title") or "").strip()
        due_time = str(body.get("due_time") or "19:00").strip()
        days_mask = int(body.get("days_mask", 127))
        priority = str(body.get("priority", "MED"))
        mandatory = 1 if body.get("mandatory") else 0
        merit_reward = int(body.get("merit_reward", 0))
        merit_penalty = int(body.get("merit_penalty", 0))
        points_reward = int(body.get("points_reward", 0))
        points_penalty = int(body.get("points_penalty", 0))
        require_evidence = 1 if body.get("require_evidence") else 0
        now = int(time.time() * 1000)
        if not template_id:
            template_id = secrets.token_hex(8)
        if not name or not title:
            raise HTTPException(400, "模板名称和任务标题不能为空")
        conn.execute(
            """INSERT INTO template(family_id, template_id, name, title, due_time, days_mask,
                priority, mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
                require_evidence, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(family_id, template_id) DO UPDATE SET
                name=excluded.name, title=excluded.title, due_time=excluded.due_time,
                days_mask=excluded.days_mask, priority=excluded.priority,
                mandatory=excluded.mandatory, merit_reward=excluded.merit_reward,
                merit_penalty=excluded.merit_penalty, points_reward=excluded.points_reward,
                points_penalty=excluded.points_penalty, require_evidence=excluded.require_evidence""",
            (fid, template_id, name, title, due_time, days_mask, priority,
             mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
             require_evidence, now),
        )
        row = conn.execute(
            "SELECT * FROM template WHERE family_id=? AND template_id=?",
            (fid, template_id),
        ).fetchone()
        return {"ok": True, "template": _template_json(row)}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, authorization: Optional[str] = Header(None)):
    """删除指定模板。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        conn.execute(
            "DELETE FROM template WHERE family_id=? AND template_id=?",
            (fam["id"], template_id),
        )
        return {"ok": True}


@app.post("/api/templates/{template_id}/dispatch")
async def dispatch_template(template_id: str, authorization: Optional[str] = Header(None)):
    """将模板作为单次任务下发到设备（生成 task_id 并推送）。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        tmpl = conn.execute(
            "SELECT * FROM template WHERE family_id=? AND template_id=?",
            (fid, template_id),
        ).fetchone()
        if not tmpl:
            raise HTTPException(404, "模板不存在")
        task_id = secrets.token_hex(8)
        now = int(time.time() * 1000)
        conn.execute(
            """INSERT INTO task(family_id, task_id, title, due_time, days_mask, priority,
                mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
                require_evidence, active, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, task_id, tmpl["title"], tmpl["due_time"], tmpl["days_mask"],
             tmpl["priority"], tmpl["mandatory"], tmpl["merit_reward"], tmpl["merit_penalty"],
             tmpl["points_reward"], tmpl["points_penalty"], tmpl["require_evidence"], 1, now),
        )
        rev = bump_revision(conn, fid)
    await ws_push(fid, device_sockets, {"type": "tasks_changed"})
    return {"ok": True, "revision": rev, "task_id": task_id}


# ── 证据文件 API（M2.7）────────────────────────────────────────


@app.get("/api/evidence/list")
def list_evidence(task_id: str = "", appeal_session_id: str = "", limit: int = 50, authorization: Optional[str] = Header(None)):
    """列出本家庭的取证照片（家长端"打卡照片"面板用）。
    task_id 筛任务打卡照；appeal_session_id 筛申诉凭证。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        if appeal_session_id:
            rows = conn.execute(
                "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
                "FROM evidence_file WHERE family_id=? AND appeal_session_id=? ORDER BY _id DESC LIMIT ?",
                (fam["id"], appeal_session_id, min(limit, 200)),
            ).fetchall()
        elif task_id:
            rows = conn.execute(
                "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
                "FROM evidence_file WHERE family_id=? AND task_id=? ORDER BY _id DESC LIMIT ?",
                (fam["id"], task_id, min(limit, 200)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
                "FROM evidence_file WHERE family_id=? ORDER BY _id DESC LIMIT ?",
                (fam["id"], min(limit, 200)),
            ).fetchall()
        return {
            "evidence": [
                {
                    "id": r["_id"],
                    "task_id": r["task_id"],
                    "task_title": r["task_title"],
                    "device_name": r["device_name"],
                    "size_bytes": r["size_bytes"],
                    "created_at": r["created_at"],
                    "appeal_session_id": r["appeal_session_id"] or "",
                }
                for r in rows
            ]
        }


@app.get("/api/evidence/{ev_id}")
def get_evidence(ev_id: int, authorization: Optional[str] = Header(None)):
    """取回一张取证照片（base64 格式 + mime）。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        row = conn.execute(
            "SELECT * FROM evidence_file WHERE _id=? AND family_id=?",
            (ev_id, fam["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "证据不存在")
        return {
            "id": row["_id"],
            "task_id": row["task_id"],
            "task_title": row["task_title"],
            "device_name": row["device_name"],
            "mime": row["mime"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "data_b64": row["data_b64"],
        }


# ── 申诉审核 API（M2.6）─────────────────────────────────────────


@app.get("/api/appeals")
def list_appeals(status: str = "", authorization: Optional[str] = Header(None)):
    """列出本家庭的申诉记录。status 空字符串表示全部，否则筛选 PENDING/APPROVED/REJECTED。"""
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        if status:
            rows = conn.execute(
                "SELECT * FROM appeal WHERE family_id=? AND result=? ORDER BY submitted_at DESC",
                (fam["id"], status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appeal WHERE family_id=? ORDER BY submitted_at DESC",
                (fam["id"],),
            ).fetchall()
        # 附加每条申诉的证据 id 列表（家长端点击可取图）
        out = []
        for r in rows:
            ev_rows = conn.execute(
                "SELECT _id, size_bytes FROM evidence_file "
                "WHERE family_id=? AND appeal_session_id=? ORDER BY _id ASC",
                (fam["id"], r["session_id"]),
            ).fetchall()
            out.append({
                "session_id": r["session_id"],
                "device_name": r["device_name"],
                "reason": r["reason"],
                "verdict": r["verdict"],
                "submitted_at": r["submitted_at"],
                "reviewed_at": r["reviewed_at"],
                "result": r["result"],
                "reviewed_by": r["reviewed_by"],
                "has_evidence": len(ev_rows) > 0,
                "evidence_ids": [e["_id"] for e in ev_rows],
            })
        return {"appeals": out}


@app.post("/api/appeals/{session_id}/review")
async def review_appeal(
    session_id: str,
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """家长审核申诉：APPROVED（通过）或 REJECTED（驳回）。结果通过 WS 推送给设备。"""
    result = str(body.get("result") or "").strip().upper()
    if result not in ("APPROVED", "REJECTED"):
        raise HTTPException(400, "result 必须为 APPROVED 或 REJECTED")
    with get_db() as conn:
        fam = auth_parent(conn, authorization)
        fid = fam["id"]
        now = int(time.time() * 1000)
        conn.execute(
            "UPDATE appeal SET result=?, reviewed_at=?, reviewed_by=? "
            "WHERE family_id=? AND session_id=?",
            (result, now, fam["id"], fid, session_id),
        )
        # 通过 WS 即时通知设备审核结果
        await ws_push(fid, device_sockets, {
            "type": "appeal_reviewed",
            "session_id": session_id,
            "result": result,
            "reviewed_at": now,
        })
        return {"ok": True}


# ── 孩子设备 API ────────────────────────────────────────────────


@app.post("/api/device/pair")
def device_pair(body: dict):
    """设备配对：用家庭码 + 家长密码换取 device_token（持久保存在 app 端）。"""
    code = str(body.get("family_code") or "").strip()
    password = str(body.get("parent_password") or "")
    name = str(body.get("device_name") or "child-device").strip()[:32] or "child-device"
    if len(password) < 6:
        raise HTTPException(400, "家长密码至少 6 位")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM family WHERE id=?", (code,)).fetchone()
        if not row or not hmac.compare_digest(row["pw_hash"], hash_pw(password, row["pw_salt"])):
            raise HTTPException(401, "家庭码或家长密码错误")
        token = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO device(token_hash, family_id, name, last_seen, created_at) VALUES(?,?,?,?,?)",
            (sha256(token), code, name, int(time.time()), int(time.time())),
        )
    return {"device_token": token, "device_name": name, "family_code": code}


@app.get("/api/tasks")
def device_pull(authorization: Optional[str] = Header(None)):
    """设备拉取任务单。revision 用于设备端判断是否有更新（可带 ?revision= 短路）。"""
    with get_db() as conn:
        dev = auth_device(conn, authorization)
        fid = dev["family_id"]
        tasks = conn.execute(
            "SELECT * FROM task WHERE family_id=? ORDER BY due_time ASC", (fid,)
        ).fetchall()
        rev_row = conn.execute("SELECT rev FROM task_revision WHERE family_id=?", (fid,)).fetchone()
        return {
            "revision": rev_row["rev"] if rev_row else 0,
            "tasks": [_task_json(r) for r in tasks],
        }


@app.post("/api/events")
async def device_push_events(body: dict, authorization: Optional[str] = Header(None)):
    """设备批量上报事件：task_completion / mission_result / appeal_submitted 等。
    入库后实时推送给在线的家长端（WS）。"""
    stored = []
    with get_db() as conn:
        dev = auth_device(conn, authorization)
        fid = dev["family_id"]
        events = body.get("events")
        if not isinstance(events, list) or len(events) > 100:
            raise HTTPException(400, "events 必须为 1~100 条的数组")
        now = int(time.time())
        for e in events:
            kind = str(e.get("kind") or "").strip()
            if not kind:
                raise HTTPException(400, "事件缺少 kind")
            payload = e.get("data")
            created = int(e.get("created_at") or now * 1000)
            conn.execute(
                "INSERT INTO event(family_id, device_name, kind, payload, created_at) VALUES(?,?,?,?,?)",
                (fid, dev["name"], kind, json.dumps(payload, ensure_ascii=False) if payload is not None else "{}",
                 created),
            )
            ev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 【M2.7 存证】通用逻辑：event 入库后立刻取 ev_id，再写 evidence_file
            ev_b64 = payload.get("evidence_b64") if payload else None
            ev_appeal_sid = str(payload.get("session_id") if payload else "") if kind == "appeal_submitted" else ""
            ev_task_id = str(payload.get("task_id") if payload else "") if kind == "task_completion" else ""
            ev_task_title = str(payload.get("title") if payload else "") if kind == "task_completion" else (
                f"申诉凭证 session={ev_appeal_sid}" if kind == "appeal_submitted" else "")
            if ev_b64 and isinstance(ev_b64, str) and len(ev_b64) > 100:
                size_bytes = len(ev_b64) * 3 // 4
                conn.execute(
                    """INSERT INTO evidence_file(family_id, event_id, task_id, task_title,
                        device_name, mime, data_b64, size_bytes, created_at, appeal_session_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (fid, ev_id, ev_task_id, ev_task_title, dev["name"],
                     "image/jpeg", ev_b64, size_bytes, created, ev_appeal_sid),
                )
            # 【M2.6】申诉提交：同时写入 appeals 表供家长审核
            if kind == "appeal_submitted":
                conn.execute(
                    """INSERT OR IGNORE INTO appeal(family_id, device_name, session_id, reason,
                        evidence_photo, verdict, submitted_at, result)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (fid, dev["name"],
                     str(payload.get("session_id") if payload else ""),
                     str(payload.get("reason") if payload else ""),
                     str(payload.get("evidence_photo") if payload else ""),
                     str(payload.get("verdict") if payload else "CAUGHT"),
                     created, "PENDING"),
                )
            stored.append({"kind": kind, "data": payload or {}, "device_name": dev["name"],
                           "created_at": created})
    # 提交后逐条推给在线家长（实时战况流）
    for ev in stored:
        await ws_push(fid, parent_sockets, {"type": "event", "event": ev})
    return {"ok": True}

# ── WebSocket 实时通道 ─────────────────────────────────────────


@app.websocket("/ws/device")
async def ws_device(ws: WebSocket, token: str = ""):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM device WHERE token_hash=?", (sha256(token),)).fetchone()
    if not row:
        await ws.close(code=4401)
        return
    fid = row["family_id"]
    await ws.accept()
    device_sockets.setdefault(fid, []).append(ws)
    conn2 = sqlite3.connect(DB_PATH)
    conn2.execute("UPDATE device SET last_seen=? WHERE token_hash=?", (int(time.time()), sha256(token)))
    conn2.commit()
    conn2.close()
    await ws_push(fid, parent_sockets, {
        "type": "device_status",
        "name": row["name"],
        "online": True,
        "last_seen_text": _seen_text(int(time.time())),
        "seen_text": _seen_text(int(time.time())),
    })
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                with get_db() as conn3:
                    conn3.execute("UPDATE device SET last_seen=? WHERE token_hash=?",
                                  (int(time.time()), sha256(token)))
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            device_sockets.get(fid, []).remove(ws)
        except ValueError:
            pass
        await ws_push(fid, parent_sockets, {
            "type": "device_status",
            "name": row["name"],
            "online": False,
            "last_seen_text": _seen_text(row["last_seen"]),
            "seen_text": _seen_text(row["last_seen"]),
        })


@app.websocket("/ws/parent")
async def ws_parent(ws: WebSocket, token: str = ""):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM family WHERE parent_token=? AND parent_token_exp>?",
            (sha256(token), int(time.time())),
        ).fetchone()
    if not row:
        await ws.close(code=4401)
        return
    fid = row["id"]
    await ws.accept()
    parent_sockets.setdefault(fid, []).append(ws)
    # 连接即补发最近 30 条事件（刷新页面/重连后战况不丢）
    try:
        with get_db() as conn2:
            rows = conn2.execute(
                "SELECT kind, payload, device_name, created_at FROM event "
                "WHERE family_id=? ORDER BY id DESC LIMIT 30", (fid,)
            ).fetchall()
        backlog = [{"kind": r["kind"], "data": json.loads(r["payload"]),
                    "device_name": r["device_name"], "created_at": r["created_at"]}
                   for r in reversed(rows)]
        await ws.send_json({"type": "events_backlog", "events": backlog})
    except Exception:
        pass
    try:
        while True:
            await ws.receive_json()  # 心跳占位
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            parent_sockets.get(fid, []).remove(ws)
        except ValueError:
            pass

# ── 任务下发后的实时通知（HTTP 层内触发）───────────────────────


@app.on_event("startup")
def _startup():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 挂载家长网页（必须放在 API 路由之后，避免遮蔽 /api/*）
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="parent")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
