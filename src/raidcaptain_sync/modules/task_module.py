"""
任务模块 - RaidCaptain Sync Server v3.4
继承 BaseModule，完全向后兼容原有 API。

新增:
  - 计时模式 (timing_mode: countdown/countup)、场景模板 (mode_id)、时长 (duration_min)
  - 任务下发留痕 (task_audit)
  - GET /api/parent/task-audit 查看下发/修改历史
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


TASK_FIELDS = (
    "task_id", "title", "due_time", "days_mask", "priority", "mandatory",
    "merit_reward", "merit_penalty", "points_reward", "points_penalty",
    "require_evidence", "active", "updated_at",
    # v3.4: 计时模式下发
    "mode_id", "duration_min", "timing_mode",
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
        # v3.4
        "mode_id": row["mode_id"] if "mode_id" in row else "builtin:writing",
        "duration_min": row["duration_min"] if "duration_min" in row else 30,
        "timing_mode": row["timing_mode"] if "timing_mode" in row else "countdown",
    }


def _validate_timing_mode(v: str) -> str:
    if v not in ("countdown", "countup", "photo"):
        raise HTTPException(400, f"timing_mode must be countdown/countup/photo, got '{v}'")
    return v


def _validate_duration_min(v: int) -> int:
    if not (1 <= v <= 240):
        raise HTTPException(400, f"duration_min must be 1..240, got {v}")
    return v


def _task_snapshot(row: dict) -> dict:
    """返回任务公开字段快照，用于 audit before/after。"""
    if not row:
        return {}
    return {
        "task_id": row.get("task_id", ""),
        "title": row.get("title", ""),
        "due_time": row.get("due_time", ""),
        "days_mask": row.get("days_mask", 127),
        "priority": row.get("priority", "MED"),
        "mandatory": bool(row.get("mandatory", 0)),
        "merit_reward": row.get("merit_reward", 0),
        "merit_penalty": row.get("merit_penalty", 0),
        "points_reward": row.get("points_reward", 0),
        "points_penalty": row.get("points_penalty", 0),
        "require_evidence": bool(row.get("require_evidence", 0)),
        "active": bool(row.get("active", 1)),
        "mode_id": row.get("mode_id") or "builtin:writing",
        "duration_min": row.get("duration_min") or 30,
        "timing_mode": row.get("timing_mode") or "countdown",
    }


class TaskModule(BaseModule):
    id = StandardModules.TASKS
    display_name = "任务系统"
    version = "1.0.0"
    description = "家庭任务下发、完成追踪、证据提交"

    def __init__(self, get_db, auth_parent, auth_device, ws_push, device_sockets, parent_sockets,
                 bump_revision, get_revision, make_task_id):
        self._get_db = get_db
        self._auth_parent = auth_parent
        self._auth_device = auth_device
        self._ws_push = ws_push
        self._device_sockets = device_sockets
        self._parent_sockets = parent_sockets
        self._bump_revision = bump_revision
        self._get_revision = get_revision
        self._make_task_id = make_task_id
        self._routers: list = []
        self._build_routers()

    async def _notify_tasks_changed(self, family_id: str, revision: int, count: int | None = None) -> None:
        """Notify every supported device client that it must pull the task snapshot.

        ``tasks_changed`` is the canonical message understood by the Android
        client.  ``tasks_updated`` is retained for older web/device clients.
        Sending both makes every task mutation follow the same wire contract.
        """
        message = {"type": "tasks_changed", "revision": revision}
        if count is not None:
            message["count"] = count
        await self._ws_push(family_id, self._device_sockets, message)
        legacy_message = dict(message)
        legacy_message["type"] = "tasks_updated"
        await self._ws_push(family_id, self._device_sockets, legacy_message)

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
            # 先查旧行，用于 audit
            old_rows: dict[str, dict] = {}
            for row in db.execute(
                "SELECT * FROM task WHERE family_id=?", (fid,)
            ).fetchall():
                old_rows[row["task_id"]] = dict(row)
            db.execute("UPDATE task SET active=0 WHERE family_id=?", (fid,))
            last_id = None
            for t in tasks:
                tid = str(t.get("task_id") or self._make_task_id())
                timing_mode = _validate_timing_mode(str(t.get("timing_mode", "countdown")))
                dur = _validate_duration_min(int(t.get("duration_min", 30)))
                db.execute(
                    """INSERT OR REPLACE INTO task(family_id, task_id, title, due_time,
                        days_mask, priority, mandatory, merit_reward, merit_penalty,
                        points_reward, points_penalty, require_evidence, active, updated_at,
                        mode_id, duration_min, timing_mode)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (fid, tid, str(t.get("title", ""))[:64],
                     str(t.get("due_time", "20:00")),
                     int(t.get("days_mask", 127)),
                     str(t.get("priority", "MED"))[:16],
                     1 if t.get("mandatory") else 0,
                     int(t.get("merit_reward", 0)),
                     int(t.get("merit_penalty", 0)),
                     int(t.get("points_reward", 0)),
                     int(t.get("points_penalty", 0)),
                     1 if t.get("require_evidence") else 0, now,
                     str(t.get("mode_id", "builtin:writing"))[:64],
                     dur, timing_mode),
                )
                # v3.4: 写入 audit
                _write_audit(db, fid, tid, "created" if tid not in old_rows else "updated",
                             body.get("_actor", "parent"), old_rows.get(tid), _task_snapshot(t))
                last_id = tid
            # 被整单替换掉的任务 → disabled
            for row in db.execute(
                "SELECT task_id, active FROM task WHERE family_id=? AND active=1", (fid,)
            ).fetchall():
                # 上面 INSERT OR REPLACE 已经确保 keep 的任务 active=1
                # 这里只处理不在 tasks 列表里的旧任务（已被 UPDATE SET active=0 处理）
                pass
            # 对未出现在新列表、且旧状态为 active 的任务写 disabled audit
            kept_ids = {str(t.get("task_id") or "") for t in tasks if t.get("task_id")}
            for tid_old, old_row in old_rows.items():
                if tid_old not in kept_ids and old_row.get("active"):
                    _write_audit(db, fid, tid_old, "disabled",
                                 body.get("_actor", "parent"), _task_snapshot(old_row), {})
            rev = self._bump_revision(db, fid)
            await self._notify_tasks_changed(fid, rev, len(tasks))
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
            # 先查旧行，用于 audit
            old_rows_v2: dict[str, dict] = {}
            for row in db.execute(
                "SELECT * FROM task WHERE family_id=?", (fid,)
            ).fetchall():
                old_rows_v2[row["task_id"]] = dict(row)
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
                timing_mode = _validate_timing_mode(str(t.get("timing_mode", "countdown")))
                dur = _validate_duration_min(int(t.get("duration_min", 30)))
                keep.add(tid)
                mode_id = str(t.get("mode_id", "builtin:writing"))[:64]
                new_active = 1 if t.get("active", True) else 0
                db.execute(
                    """INSERT INTO task(family_id, task_id, title, due_time, days_mask,
                        priority, mandatory, merit_reward, merit_penalty, points_reward,
                        points_penalty, require_evidence, active, updated_at,
                        mode_id, duration_min, timing_mode)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(family_id, task_id) DO UPDATE SET
                        title=excluded.title, due_time=excluded.due_time,
                        days_mask=excluded.days_mask, priority=excluded.priority,
                        mandatory=excluded.mandatory, merit_reward=excluded.merit_reward,
                        merit_penalty=excluded.merit_penalty, points_reward=excluded.points_reward,
                        points_penalty=excluded.points_penalty,
                        require_evidence=excluded.require_evidence,
                        active=excluded.active, updated_at=excluded.updated_at,
                        mode_id=excluded.mode_id, duration_min=excluded.duration_min,
                        timing_mode=excluded.timing_mode""",
                    (fid, tid, title, due, mask, str(t.get("priority", "MED")),
                     1 if t.get("mandatory") else 0,
                     int(t.get("merit_reward", 0)), int(t.get("merit_penalty", 0)),
                     int(t.get("points_reward", 0)), int(t.get("points_penalty", 0)),
                     1 if t.get("require_evidence") else 0,
                     new_active, now, mode_id, dur, timing_mode),
                )
                old = old_rows_v2.get(tid)
                action = "created" if not old else "updated"
                # 如果旧行 active=1 新行 active=0 → 当作 disabled 记录
                if old and old.get("active") and not new_active:
                    action = "disabled"
                _write_audit(db, fid, tid, action,
                             body.get("_actor", "parent"),
                             _task_snapshot(old) if old else {},
                             _task_snapshot({"task_id": tid, **t}) if new_active else {})
            # 被移除的旧任务 → disabled
            for row in db.execute(
                "SELECT task_id, active FROM task WHERE family_id=?", (fid,)
            ).fetchall():
                if row["task_id"] not in keep and row["active"]:
                    db.execute(
                        "UPDATE task SET active=0, updated_at=? "
                        "WHERE family_id=? AND task_id=?",
                        (now, fid, row["task_id"]),
                    )
                    old_snap = old_rows_v2.get(row["task_id"])
                    _write_audit(db, fid, row["task_id"], "disabled",
                                 body.get("_actor", "parent"),
                                 _task_snapshot(old_snap) if old_snap else {},
                                 {})
            rev = self._bump_revision(db, fid)
            await self._notify_tasks_changed(fid, rev, len(tasks))
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

        # --- v3.4 新增: 任务下发留痕 ---
        @r.get("/parent/task-audit")
        def list_task_audit(
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0),
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            rows = db.execute(
                "SELECT * FROM task_audit WHERE family_id=? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (fid, limit, offset),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) AS c FROM task_audit WHERE family_id=?", (fid,)
            ).fetchone()["c"]
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "records": [
                    {
                        "id": r["_id"],
                        "task_id": r["task_id"],
                        "action": r["action"],
                        "actor": r["actor"],
                        "before": json.loads(r["before_json"]) if r["before_json"] else {},
                        "after": json.loads(r["after_json"]) if r["after_json"] else {},
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ],
            }

        # v3.1: 删除任务
        @r.delete("/parent/tasks/{task_id}")
        async def delete_task(task_id: str, authorization: str | None = Header(None),
                               db=Depends(self._get_db)):
            """家长删除某条任务（软禁用）。"""
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            tid = task_id.strip()
            if not tid:
                raise HTTPException(400, "task_id 为空")
            old = db.execute(
                "SELECT * FROM task WHERE family_id=? AND task_id=?", (fid, tid)
            ).fetchone()
            now = int(time.time() * 1000)
            if old:
                db.execute(
                    "UPDATE task SET active=0, updated_at=? WHERE family_id=? AND task_id=?",
                    (now, fid, tid),
                )
                _write_audit(db, fid, tid, "deleted",
                             authorization or "parent",
                             _task_snapshot(dict(old)), {})
                rev = self._bump_revision(db, fid)
                await self._notify_tasks_changed(fid, rev)
                return {"ok": True, "task_id": tid, "revision": rev}
            raise HTTPException(404, f"任务 {tid} 不存在")

        # ── V26: 任务状态回写（对局结束后由设备上报） ──
        @r.post("/device/tasks/state")
        async def set_task_state(
            body: dict,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            """设备上报任务对局结果。
            body: {task_id, state: DONE|OVERDUE, actual_minutes?: int, timing_mode?: str}
            """
            dev = self._auth_device(db, authorization)
            fid = dev["family_id"]
            tid = str(body.get("task_id", "")).strip()
            state = str(body.get("state", "")).strip().upper()
            actual_min = int(body.get("actual_minutes", 0) or 0)
            timing_mode = str(body.get("timing_mode", "countdown")).strip()

            if not tid or state not in ("DONE", "OVERDUE", "ABORTED", "IN_PROGRESS"):
                raise HTTPException(400, "invalid task_id or state")

            # 更新 task_audit（可选：记录设备上报）
            now = int(time.time() * 1000)
            task_row = db.execute(
                "SELECT * FROM task WHERE family_id=? AND task_id=?", (fid, tid)
            ).fetchone()
            if not task_row:
                raise HTTPException(404, "task not found")

            # 写事件留痕
            payload = {
                "task_id": tid,
                "title": task_row["title"],
                "state": state,
                "actual_minutes": actual_min,
                "timing_mode": timing_mode,
                "device": dev["name"],
            }
            db.execute(
                "INSERT INTO event(family_id, device_name, kind, payload, created_at) "
                "VALUES(?,?,?,?,?)",
                (fid, dev["name"], "task_state_update",
                 json.dumps(payload, ensure_ascii=False), now // 1000),
            )

            # 推送实时更新给家长
            await self._ws_push(fid, self._parent_sockets, {
                "type": "task_state_changed",
                "task_id": tid,
                "state": state,
                "actual_minutes": actual_min,
                "timing_mode": timing_mode,
            })

            return {"ok": True, "task_id": tid, "state": state}

        self._routers = [r]


def _write_audit(db, family_id: str, task_id: str, action: str, actor: str,
                 before: dict | None, after: dict | None) -> None:
    db.execute(
        "INSERT INTO task_audit(family_id, task_id, action, actor, before_json, after_json, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (family_id, task_id, action, actor,
         json.dumps(before or {}, ensure_ascii=False),
         json.dumps(after or {}, ensure_ascii=False),
         int(time.time() * 1000)),
    )


def create_task_module(get_db, auth_parent, auth_device, ws_push, device_sockets, parent_sockets,
                       bump_revision, get_revision, make_task_id) -> TaskModule:
    return TaskModule(
        get_db, auth_parent, auth_device, ws_push, device_sockets, parent_sockets,
        bump_revision, get_revision, make_task_id,
    )
