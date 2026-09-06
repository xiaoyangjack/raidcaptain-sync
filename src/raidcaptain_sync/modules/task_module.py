"""
任务模块 - RaidCaptain Sync Server v3.1
继承 BaseModule，完全向后兼容原有 API。

新增 API:
  - GET  /api/tasks/sync?revisions={"tasks":5}  (按模块 revision 精准拉取)
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


TASK_FIELDS = (
    "task_id", "title", "due_time", "days_mask", "priority", "mandatory",
    "merit_reward", "merit_penalty", "points_reward", "points_penalty",
    "require_evidence", "active", "updated_at",
)


def _task_json(row: dict, deleted: bool = False) -> dict:
    return {
        "task_id": row["task_id"],
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
        "active": False if deleted else bool(row["active"]),
        "updated_at": row["updated_at"],
    }


class TaskModule(BaseModule):
    id = StandardModules.TASKS
    display_name = "任务系统"
    version = "1.0.0"
    description = "家庭任务下发、完成追踪、证据提交"

    def __init__(self, get_db, auth_parent, auth_device, ws_push, device_sockets,
                 bump_revision, get_revision, make_task_id):
        self._get_db = get_db
        self._auth_parent = auth_parent
        self._auth_device = auth_device
        self._ws_push = ws_push
        self._device_sockets = device_sockets
        self._bump_revision = bump_revision
        self._get_revision = get_revision
        self._make_task_id = make_task_id
        self._routers: list = []
        self._build_routers()

    def _build_routers(self) -> None:
        r = APIRouter(prefix="/api", tags=["tasks"])

        @r.get("/parent/tasks")
        def list_tasks(authorization: str | None = Header(None), db=Depends(self._get_db)):
            fam = self._auth_parent(db, authorization)
            rows = db.execute(
                "SELECT * FROM task WHERE family_id=? ORDER BY updated_at DESC",
                (fam["id"],)
            ).fetchall()
            return {
                "revision": self._get_revision(db, fam["id"]),
                "tasks": [_task_json(r) for r in rows],
            }

        @r.post("/parent/tasks")
        async def push_tasks(
            body: dict, authorization: str | None = Header(None),
            db=Depends(self._get_db)
        ):
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            tasks = body.get("tasks")
            if not isinstance(tasks, list) or len(tasks) > 200:
                raise HTTPException(400, "tasks must be array of 1-200 items")
            now = int(time.time() * 1000)
            db.execute("UPDATE task SET active=0 WHERE family_id=?", (fid,))
            last_id = None
            for t in tasks:
                tid = str(t.get("task_id") or self._make_task_id())
                db.execute(
                    """INSERT OR REPLACE INTO task(family_id, task_id, title, due_time,
                        days_mask, priority, mandatory, merit_reward, merit_penalty,
                        points_reward, points_penalty, require_evidence, active, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (fid, tid, str(t.get("title", ""))[:64],
                     str(t.get("due_time", "20:00")),
                     int(t.get("days_mask", 127)),
                     str(t.get("priority", "MED"))[:16],
                     1 if t.get("mandatory") else 0,
                     int(t.get("merit_reward", 0)),
                     int(t.get("merit_penalty", 0)),
                     int(t.get("points_reward", 0)),
                     int(t.get("points_penalty", 0)),
                     1 if t.get("require_evidence") else 0, now),
                )
                last_id = tid
            rev = self._bump_revision(db, fid)
            await self._ws_push(
                fid, self._device_sockets,
                {"type": "tasks_updated", "revision": rev, "count": len(tasks)}
            )
            return {"ok": True, "revision": rev, "task_id": last_id}

        @r.post("/tasks")
        async def push_tasks_v2(
            body: dict, authorization: str | None = Header(None),
            db=Depends(self._get_db)
        ):
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            tasks = body.get("tasks")
            if not isinstance(tasks, list) or len(tasks) > 200:
                raise HTTPException(400, "tasks must be 1-200")
            now = int(time.time() * 1000)
            keep = set()
            for t in tasks:
                tid = str(t.get("task_id") or "").strip()
                title = str(t.get("title") or "").strip()
                due = str(t.get("due_time") or "").strip()
                if not tid or not title:
                    raise HTTPException(400, "task missing task_id or title")
                try:
                    hh, mm = due.split(":")
                    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                        raise ValueError
                except Exception:
                    raise HTTPException(400, f"due_time format for '{title}' must be HH:mm")
                mask = int(t.get("days_mask", 127))
                if not (1 <= mask <= 127):
                    raise HTTPException(400, f"days_mask invalid for '{title}'")
                keep.add(tid)
                db.execute(
                    """INSERT INTO task(family_id, task_id, title, due_time, days_mask,
                        priority, mandatory, merit_reward, merit_penalty, points_reward,
                        points_penalty, require_evidence, active, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(family_id, task_id) DO UPDATE SET
                        title=excluded.title, due_time=excluded.due_time,
                        days_mask=excluded.days_mask, priority=excluded.priority,
                        mandatory=excluded.mandatory, merit_reward=excluded.merit_reward,
                        merit_penalty=excluded.merit_penalty, points_reward=excluded.points_reward,
                        points_penalty=excluded.points_penalty,
                        require_evidence=excluded.require_evidence,
                        active=excluded.active, updated_at=excluded.updated_at""",
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
                        "UPDATE task SET active=0, updated_at=? "
                        "WHERE family_id=? AND task_id=?",
                        (now, fid, row["task_id"]),
                    )
            rev = self._bump_revision(db, fid)
            await self._ws_push(fid, self._device_sockets, {"type": "tasks_changed"})
            return {"ok": True, "revision": rev}

        @r.get("/tasks")
        def pull_tasks(authorization: str | None = Header(None),
                       db=Depends(self._get_db)):
            dev = self._auth_device(db, authorization)
            fid = dev["family_id"]
            tasks = db.execute(
                "SELECT * FROM task WHERE family_id=? ORDER BY due_time ASC",
                (fid,)
            ).fetchall()
            rev_row = db.execute(
                "SELECT rev FROM task_revision WHERE family_id=?", (fid,)
            ).fetchone()
            return {
                "revision": rev_row["rev"] if rev_row else 0,
                "tasks": [_task_json(r) for r in tasks],
            }

        # --- v3.1 新增: 按模块 revision 精准拉取 ---
        @r.get("/tasks/sync")
        def sync_tasks(
            revisions: str = "",
            authorization: str | None = Header(None),
            db=Depends(self._get_db)
        ):
            """设备端精准同步：只返回有变化的模块数据。"""
            dev = self._auth_device(db, authorization)
            fid = dev["family_id"]

            import json
            client_revs: dict[str, int] = {}
            if revisions:
                try:
                    client_revs = json.loads(revisions)
                except Exception:
                    pass

            current_rev = self._get_revision(db, fid)
            client_rev = client_revs.get(StandardModules.TASKS, 0)

            if current_rev == client_rev:
                return {
                    "modules": {StandardModules.TASKS: {
                        "rev": current_rev, "changed": False, "data": None
                    }}
                }

            tasks = db.execute(
                "SELECT * FROM task WHERE family_id=? ORDER BY due_time ASC",
                (fid,)
            ).fetchall()
            return {
                "modules": {StandardModules.TASKS: {
                    "rev": current_rev,
                    "changed": True,
                    "data": {"tasks": [_task_json(r) for r in tasks]},
                }}
            }

        self._routers = [r]


def create_task_module(get_db, auth_parent, auth_device, ws_push, device_sockets,
                       bump_revision, get_revision, make_task_id) -> TaskModule:
    return TaskModule(
        get_db, auth_parent, auth_device, ws_push, device_sockets,
        bump_revision, get_revision, make_task_id,
    )