"""
申诉审核路由 - RAID Captain Sync
"""
from fastapi import APIRouter, Depends, Header, HTTPException

from raidcaptain_sync.deps import auth_parent, device_sockets, get_db, ws_push

router = APIRouter()


@router.get("/api/appeals")
def list_appeals(
    status: str = "",
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """列出本家庭的申诉记录。"""
    fam = auth_parent(db, authorization)
    if status:
        rows = db.execute(
            "SELECT * FROM appeal WHERE family_id=? AND result=? ORDER BY submitted_at DESC",
            (fam["id"], status),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM appeal WHERE family_id=? ORDER BY submitted_at DESC",
            (fam["id"],),
        ).fetchall()
    out = []
    for r in rows:
        ev_rows = db.execute(
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


@router.post("/api/appeals/{session_id}/review")
async def review_appeal(
    session_id: str,
    body: dict,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """家长审核申诉：APPROVED 或 REJECTED。"""
    import time
    result = str(body.get("result") or "").strip().upper()
    if result not in ("APPROVED", "REJECTED"):
        raise HTTPException(400, "result 必须为 APPROVED 或 REJECTED")
    fam = auth_parent(db, authorization)
    fid = fam["id"]
    now = int(time.time() * 1000)
    db.execute(
        "UPDATE appeal SET result=?, reviewed_at=?, reviewed_by=? "
        "WHERE family_id=? AND session_id=?",
        # ↑ 5 个占位符，5 个参数：正确！（main.py 原版的 bug 已修复）
        (result, now, fid, fid, session_id),
    )
    await ws_push(fid, device_sockets, {
        "type": "appeal_reviewed",
        "session_id": session_id,
        "result": result,
        "reviewed_at": now,
    })
    return {"ok": True}
