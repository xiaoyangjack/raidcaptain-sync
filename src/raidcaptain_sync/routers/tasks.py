"""
任务路由 - RAID Captain Sync
"""
import time

from fastapi import APIRouter, Depends, Header, HTTPException

from raidcaptain_sync.deps import (
    auth_parent,
    bump_revision,
    device_sockets,
    get_db,
    ws_push,
)
from raidcaptain_sync.services.auth import make_token as _new_task_id

router = APIRouter()

TASK_FIELDS = (
    "task_id", "title", "due_time", "days_mask", "priority", "mandatory",
    "merit_reward", "merit_penalty", "points_reward", "points_penalty",
    "require_evidence", "active", "updated_at",
)


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


@router.get("/api/parent/tasks")
def parent_list_tasks(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    """家长查询本家庭下发的所有任务。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    rows = db.execute(
        "SELECT * FROM task WHERE family_id=? ORDER BY updated_at DESC", (fid,)
    ).fetchall()
    return {
        "revision": _get_revision(db, fid),
        "tasks": [_task_json(r) for r in rows],
    }


@router.post("/api/parent/tasks")
async def parent_push_tasks(
    body: dict, authorization: str | None = Header(None), db=Depends(get_db)
):
    """家长下发任务单（替换式）。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    tasks = body.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > 200:
        raise HTTPException(400, "tasks 必须为 1~200 的数组")
    now = int(time.time() * 1000)
    db.execute("UPDATE task SET active=0 WHERE family_id=?", (fid,))
    last_task_id = None
    for t in tasks:
        task_id = str(t.get("task_id") or _new_task_id())
        db.execute(
            """INSERT OR REPLACE INTO task(family_id, task_id, title, due_time, days_mask,
                priority, mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
                require_evidence, active, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (fid, task_id,
             str(t.get("title", ""))[:64],
             str(t.get("due_time", "20:00")),
             int(t.get("days_mask", 127)),
             str(t.get("priority", "NORMAL"))[:16],
             1 if t.get("mandatory") else 0,
             int(t.get("merit_reward", 0)),
             int(t.get("merit_penalty", 0)),
             int(t.get("points_reward", 0)),
             int(t.get("points_penalty", 0)),
             1 if t.get("require_evidence") else 0,
             now),
        )
        last_task_id = task_id
    rev = bump_revision(db, fid)
    await ws_push(fid, device_sockets, {
        "type": "tasks_updated", "revision": rev, "count": len(tasks)
    })
    return {"ok": True, "revision": rev, "task_id": last_task_id}


@router.post("/api/tasks")
async def push_tasks(
    body: dict, authorization: str | None = Header(None), db=Depends(get_db)
):
    """家长整单下发（全量覆盖语义）。"""
    fam = auth_parent(db, authorization)
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
        db.execute(
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
    for row in db.execute(
        "SELECT task_id FROM task WHERE family_id=? AND active=1", (fid,)
    ).fetchall():
        if row["task_id"] not in keep:
            db.execute(
                "UPDATE task SET active=0, updated_at=? WHERE family_id=? AND task_id=?",
                (now, fid, row["task_id"]),
            )
    rev = bump_revision(db, fid)
    await ws_push(fid, device_sockets, {"type": "tasks_changed"})
    return {"ok": True, "revision": rev}


@router.get("/api/tasks")
def device_pull(authorization: str | None = Header(None), db=Depends(get_db)):
    """设备拉取任务单。"""
    from raidcaptain_sync.deps import auth_device
    dev = auth_device(db, authorization)
    fid = dev["family_id"]
    tasks = db.execute(
        "SELECT * FROM task WHERE family_id=? ORDER BY due_time ASC", (fid,)
    ).fetchall()
    rev_row = db.execute(
        "SELECT rev FROM task_revision WHERE family_id=?", (fid,)
    ).fetchone()
    return {
        "revision": rev_row["rev"] if rev_row else 0,
        "tasks": [_task_json(r) for r in tasks],
    }


def _get_revision(db, family_id: str) -> int:
    from raidcaptain_sync.deps import get_revision
    return get_revision(db, family_id)