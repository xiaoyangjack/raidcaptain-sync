"""
状态路由 - RAID Captain Sync
设备在线状态 + 健康检查 + 今日任务状态。
"""
import datetime
import time

from fastapi import APIRouter, Depends, Header, HTTPException

from raidcaptain_sync.deps import auth_parent, get_db
from raidcaptain_sync.services.oss_storage import oss_storage

router = APIRouter()

ONLINE_WINDOW_S = 90


def _seen_text(ts: int) -> str:
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


@router.get("/api/status")
def status(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    """设备在线状态 + 任务总数 + 待审申诉数。"""
    fam = auth_parent(db, authorization)
    devices = db.execute(
        "SELECT name, last_seen FROM device WHERE family_id=?", (fam["id"],)
    ).fetchall()
    now = int(time.time())
    rev_row = db.execute(
        "SELECT rev FROM task_revision WHERE family_id=?", (fam["id"],)
    ).fetchone()
    tasks = db.execute(
        "SELECT * FROM task WHERE family_id=? AND active=1 ORDER BY due_time",
        (fam["id"],),
    ).fetchall()
    pending_appeals = db.execute(
        "SELECT COUNT(*) FROM appeal WHERE family_id=? AND result='PENDING'",
        (fam["id"],),
    ).fetchone()[0]
    return {
        "devices": [
            {
                "name": d["name"],
                "online": (now - d["last_seen"]) < ONLINE_WINDOW_S,
                "last_seen": d["last_seen"],
                "last_seen_text": _seen_text(d["last_seen"]),
            }
            for d in devices
        ],
        "revision": rev_row["rev"] if rev_row else 0,
        "active_tasks": len(tasks),
        "pending_appeals": pending_appeals,
    }


@router.get("/api/parent/today-states")
def parent_today_states(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    """今日任务完成状态 + 现场照列表。"""
    import json
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    now = int(time.time() * 1000)
    today = datetime.date.today()
    t_start = int(datetime.datetime(today.year, today.month, today.day).timestamp() * 1000)
    t_end = t_start + 86_400_000

    rows = db.execute("""
        SELECT _id, kind, payload, created_at, device_name FROM event
        WHERE family_id=? AND kind IN ('task_completion','mission_result')
          AND created_at >= ? AND created_at < ?
        ORDER BY _id DESC
    """, (fid, t_start, t_end)).fetchall()

    latest_by_task = {}
    for r in rows:
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            p = {}
        tid = p.get("task_id")
        if r["kind"] == "task_completion" and tid and tid not in latest_by_task:
            latest_by_task[tid] = {
                "state": p.get("state", "DONE"),
                "created_at": r["created_at"],
                "device": r["device_name"],
                "evidence": bool(p.get("evidence_b64")),
                "title": p.get("title", ""),
            }

    ev_rows = db.execute("""
        SELECT _id, task_id, task_title, created_at, size_bytes FROM evidence_file
        WHERE family_id=? AND appeal_session_id='' AND task_id != ''
          AND created_at >= ? AND created_at < ?
        ORDER BY _id DESC
    """, (fid, t_start, t_end)).fetchall()
    evidence_by_task = {}
    for r in ev_rows:
        evidence_by_task.setdefault(r["task_id"], []).append({
            "id": r["_id"], "title": r["task_title"],
            "size": r["size_bytes"], "at": r["created_at"],
        })
    return {"states": latest_by_task, "evidence": evidence_by_task}


@router.get("/health")
def health_check(db=Depends(get_db)):
    """
    健康检查端点 v3.1。
    返回 { status, db, oss, modules } — 用于容器编排探针。
    """
    from raidcaptain_sync.services.module_registry import module_registry
    from raidcaptain_sync.services.oss_storage import oss_storage

    db_ok = False
    try:
        db.execute("SELECT 1 FROM family LIMIT 1").fetchone()
        db_ok = True
    except Exception:
        pass
    oss_ok = oss_storage.health_check()
    overall = "ok" if (db_ok and oss_ok) else "degraded"
    return {
        "status": overall,
        "db": db_ok,
        "oss": oss_ok,
        "modules": module_registry.status(),
        "version": "3.1.0",
    }


@router.get("/api/admin/overview")
def admin_overview(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    """v3.1 新增：管理仪表盘总览。"""
    from raidcaptain_sync.deps import auth_parent
    from raidcaptain_sync.services.revision import RevisionManager

    fam = auth_parent(db, authorization)
    fid = fam["id"]

    # 各模块统计
    stats = {}
    tables = [
        ("task", "active=1"),
        ("template", "1=1"),
        ("patrol_session", "1=1"),
        ("appeal", "result='PENDING'"),
        ("evidence_file", "appeal_session_id=''"),
        ("storyline_bundle", "active=1"),
        ("achievement", "active=1"),
        ("family_achievement", "1=1"),
        ("announcement", "read=0"),
    ]
    for table, where in tables:
        try:
            count = db.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE family_id=? AND {where}",
                (fid,),
            ).fetchone()[0] if "family_id" in [
                c[1] for c in db.execute(f"PRAGMA table_info({table})").fetchall()
            ] else db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}"
            ).fetchone()[0]
            stats[table] = count
        except Exception:
            stats[table] = -1

    # 各模块 revision
    revisions = RevisionManager(db).get_all(fid)

    # 事件订阅关系
    from raidcaptain_sync.services.event_bus import event_bus
    subscriptions = event_bus.list_subscriptions()

    return {
        "stats": stats,
        "revisions": revisions,
        "modules": [
            {
                "id": m.id, "name": m.display_name,
                "version": m.version, "routers": len(m.get_routers()),
            }
            for m in __import__("raidcaptain_sync.services.module_registry", fromlist=["module_registry"]).module_registry.all()
        ],
        "event_subscriptions": subscriptions,
    }


@router.get("/api/health")
def api_health(db=Depends(get_db)):
    """兼容旧版命名的 health 端点。"""
    return health_check(db=db)