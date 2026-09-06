"""
管理端路由 - RAID Captain Sync
数据清理/重置/汇总。
"""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from raidcaptain_sync.deps import auth_parent, get_db
from raidcaptain_sync.services.oss_storage import oss_storage

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/admin/summary")
def admin_summary(
    authorization: str | None = Header(None), db=Depends(get_db)
):
    """数据概况。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    task_count = db.execute(
        "SELECT COUNT(*) FROM task WHERE family_id=?", (fid,)
    ).fetchone()[0]
    event_count = db.execute(
        "SELECT COUNT(*) FROM event WHERE family_id=?", (fid,)
    ).fetchone()[0]
    photo_count = db.execute(
        "SELECT COUNT(*) FROM evidence_file WHERE family_id=? AND appeal_session_id=''",
        (fid,),
    ).fetchone()[0]
    appeal_count = db.execute(
        "SELECT COUNT(*) FROM appeal WHERE family_id=?", (fid,)
    ).fetchone()[0]
    template_count = db.execute(
        "SELECT COUNT(*) FROM template WHERE family_id=?", (fid,)
    ).fetchone()[0]
    size_row = db.execute(
        "SELECT SUM(size_bytes) FROM evidence_file WHERE family_id=?", (fid,)
    ).fetchone()[0]
    photo_size = size_row[0] if size_row and size_row[0] else 0
    return {
        "task_count": task_count,
        "event_count": event_count,
        "photo_count": photo_count,
        "appeal_count": appeal_count,
        "template_count": template_count,
        "photo_size_mb": round(photo_size / 1024 / 1024, 2) if photo_size else 0,
    }


@router.delete("/api/admin/tasks/{task_id}")
def admin_delete_task(
    task_id: str,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """删除单条任务。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    db.execute("DELETE FROM task WHERE family_id=? AND task_id=?", (fid, task_id))
    db.execute("DELETE FROM template WHERE family_id=? AND template_id=?", (fid, task_id))
    return {"ok": True, "deleted": task_id}


@router.post("/api/admin/clear")
def admin_clear(
    body: dict,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """按类型清空数据。需 confirm_code = 家庭码。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    target = str(body.get("type") or "").strip().lower()
    confirm_code = str(body.get("confirm_code") or "").strip()
    if not confirm_code:
        raise HTTPException(400, "需要输入家庭码确认身份")
    if confirm_code != fid:
        raise HTTPException(403, "家庭码不匹配，无法执行清理")
    if target not in ("events", "photos", "appeals", "templates", "tasks", "all"):
        raise HTTPException(400, f"未知类型：{target}")

    counts = {}
    if target in ("events", "all"):
        n = db.execute("SELECT COUNT(*) FROM event WHERE family_id=?", (fid,)).fetchone()[0]
        db.execute("DELETE FROM event WHERE family_id=?", (fid,))
        counts["events"] = n
    if target in ("photos", "all"):
        n = db.execute("SELECT COUNT(*) FROM evidence_file WHERE family_id=?", (fid,)).fetchone()[0]
        db.execute("DELETE FROM evidence_file WHERE family_id=?", (fid,))
        # 同步删除 OSS 对象
        try:
            oss_storage.delete_family(fid)
        except Exception as e:
            logger.warning("OSS delete failed for family %s: %s", fid, e)
        counts["photos"] = n
    if target in ("appeals", "all"):
        n = db.execute("SELECT COUNT(*) FROM appeal WHERE family_id=?", (fid,)).fetchone()[0]
        db.execute("DELETE FROM appeal WHERE family_id=?", (fid,))
        counts["appeals"] = n
    if target in ("templates", "all"):
        n = db.execute("SELECT COUNT(*) FROM template WHERE family_id=?", (fid,)).fetchone()[0]
        db.execute("DELETE FROM template WHERE family_id=?", (fid,))
        counts["templates"] = n
    if target == "tasks":
        n = db.execute("SELECT COUNT(*) FROM task WHERE family_id=?", (fid,)).fetchone()[0]
        db.execute("DELETE FROM task WHERE family_id=?", (fid,))
        db.execute("UPDATE task_revision SET rev=rev+1 WHERE family_id=?", (fid,))
        counts["tasks"] = n
    if target == "all":
        db.execute("UPDATE task_revision SET rev=rev+1 WHERE family_id=?", (fid,))
        counts["tasks"] = db.execute(
            "SELECT COUNT(*) FROM task WHERE family_id=?", (fid,)
        ).fetchone()[0] or 0

    return {"ok": True, "cleared": counts, "target": target}


@router.post("/api/admin/reset")
def admin_reset(
    body: dict,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """彻底重置账号。"""
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    confirm_code = str(body.get("confirm_code") or "").strip()
    confirm_word = str(body.get("confirm_word") or "").strip().upper()
    if confirm_code != fid:
        raise HTTPException(403, "家庭码不匹配")
    if confirm_word != "RESET":
        raise HTTPException(400, "确认词错误，请输入大写 RESET")

    db.execute("DELETE FROM event WHERE family_id=?", (fid,))
    db.execute("DELETE FROM evidence_file WHERE family_id=?", (fid,))
    db.execute("DELETE FROM appeal WHERE family_id=?", (fid,))
    db.execute("DELETE FROM template WHERE family_id=?", (fid,))
    db.execute("DELETE FROM task WHERE family_id=?", (fid,))
    db.execute("DELETE FROM device WHERE family_id=?", (fid,))
    db.execute("UPDATE task_revision SET rev=1 WHERE family_id=?", (fid,))

    try:
        oss_storage.delete_family(fid)
    except Exception as e:
        logger.warning("OSS delete_family failed for %s: %s", fid, e)

    return {"ok": True, "message": "账号已重置，所有数据已清空"}