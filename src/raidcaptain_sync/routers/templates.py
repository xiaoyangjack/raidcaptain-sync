"""
任务模板路由 - RAID Captain Sync
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
from raidcaptain_sync.services.auth import make_token as _new_template_id

router = APIRouter()


def _template_json(row) -> dict:
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


@router.get("/api/templates")
def list_templates(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    fam = auth_parent(db, authorization)
    rows = db.execute(
        "SELECT * FROM template WHERE family_id=? ORDER BY created_at DESC",
        (fam["id"],),
    ).fetchall()
    return {"templates": [_template_json(r) for r in rows]}


@router.post("/api/templates")
def save_template(
    body: dict,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """保存/更新模板。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    template_id = str(body.get("template_id") or "").strip()
    name = str(body.get("name") or "").strip()
    title = str(body.get("title") or "").strip()
    if not name or not title:
        raise HTTPException(400, "模板名称和任务标题不能为空")
    if not template_id:
        template_id = _new_template_id()
    db.execute(
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
        (fid, template_id, name, title,
         str(body.get("due_time") or "19:00"),
         int(body.get("days_mask", 127)),
         str(body.get("priority", "MED")),
         1 if body.get("mandatory") else 0,
         int(body.get("merit_reward", 0)),
         int(body.get("merit_penalty", 0)),
         int(body.get("points_reward", 0)),
         int(body.get("points_penalty", 0)),
         1 if body.get("require_evidence") else 0,
         int(time.time() * 1000)),
    )
    row = db.execute(
        "SELECT * FROM template WHERE family_id=? AND template_id=?",
        (fid, template_id),
    ).fetchone()
    return {"ok": True, "template": _template_json(row)}


@router.delete("/api/templates/{template_id}")
def delete_template(
    template_id: str,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    fam = auth_parent(db, authorization)
    db.execute(
        "DELETE FROM template WHERE family_id=? AND template_id=?",
        (fam["id"], template_id),
    )
    return {"ok": True}


@router.post("/api/templates/{template_id}/dispatch")
async def dispatch_template(
    template_id: str,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """将模板作为单次任务下发。"""
    import secrets
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    tmpl = db.execute(
        "SELECT * FROM template WHERE family_id=? AND template_id=?",
        (fid, template_id),
    ).fetchone()
    if not tmpl:
        raise HTTPException(404, "模板不存在")
    task_id = secrets.token_hex(8)
    now = int(time.time() * 1000)
    db.execute(
        """INSERT INTO task(family_id, task_id, title, due_time, days_mask, priority,
            mandatory, merit_reward, merit_penalty, points_reward, points_penalty,
            require_evidence, active, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, task_id, tmpl["title"], tmpl["due_time"], tmpl["days_mask"],
         tmpl["priority"], tmpl["mandatory"], tmpl["merit_reward"], tmpl["merit_penalty"],
         tmpl["points_reward"], tmpl["points_penalty"], tmpl["require_evidence"], 1, now),
    )
    rev = bump_revision(db, fid)
    await ws_push(fid, device_sockets, {"type": "tasks_changed"})
    return {"ok": True, "revision": rev, "task_id": task_id}