"""
证据文件路由 - RAID Captain Sync
支持 OSS URL 访问 + base64 降级。
"""
import base64

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from raidcaptain_sync.deps import auth_parent, get_db
from raidcaptain_sync.services.oss_storage import oss_storage

router = APIRouter()


@router.get("/api/evidence/list")
def list_evidence(
    task_id: str = "",
    appeal_session_id: str = "",
    limit: int = 50,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """列出本家庭的取证照片。"""
    fam = auth_parent(db, authorization)
    limit = min(limit, 200)
    if appeal_session_id:
        rows = db.execute(
            "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
            "FROM evidence_file WHERE family_id=? AND appeal_session_id=? ORDER BY _id DESC LIMIT ?",
            (fam["id"], appeal_session_id, limit),
        ).fetchall()
    elif task_id:
        rows = db.execute(
            "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
            "FROM evidence_file WHERE family_id=? AND task_id=? ORDER BY _id DESC LIMIT ?",
            (fam["id"], task_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT _id, task_id, task_title, device_name, size_bytes, created_at, appeal_session_id "
            "FROM evidence_file WHERE family_id=? ORDER BY _id DESC LIMIT ?",
            (fam["id"], limit),
        ).fetchall()
    return {
        "evidence": [
            {
                "id": r["_id"], "task_id": r["task_id"], "task_title": r["task_title"],
                "device_name": r["device_name"], "size_bytes": r["size_bytes"],
                "created_at": r["created_at"], "appeal_session_id": r["appeal_session_id"] or "",
            }
            for r in rows
        ]
    }


@router.get("/api/evidence/{ev_id}")
def get_evidence(
    ev_id: int,
    authorization: str | None = Header(None),
    token: str | None = None,
    db=Depends(get_db),
):
    """
    取回一张取证照片。
    优先返回 OSS 签名 URL（供 <img> 使用）；OSS 未启用时降级返回 base64。
    支持 Authorization: Bearer xxx 或 ?token=xxx（<img> 兼容）。
    """
    auth = authorization
    if not auth and token:
        auth = f"Bearer {token}"
    fam = auth_parent(db, auth)
    row = db.execute(
        "SELECT * FROM evidence_file WHERE _id=? AND family_id=?",
        (ev_id, fam["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, "证据不存在")

    # OSS 优先：生成签名 URL
    oss_key = row["oss_key"] or ""
    if oss_key and oss_storage._enabled:
        url = oss_storage.get_url(oss_key, expires_seconds=7200)
        return {"url": url, "via": "oss"}

    # 降级：返回 base64
    data_b64 = row["data_b64"] or ""
    if data_b64:
        try:
            data = base64.b64decode(data_b64)
            return Response(content=data, media_type=row["mime"] or "image/jpeg")
        except Exception:
            pass

    raise HTTPException(404, "证据文件未找到")


@router.post("/api/evidence/{ev_id}/approve")
def approve_evidence(
    ev_id: int,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """家长确认证据有效，触发加分。"""
    import json
    
    fam = auth_parent(db, authorization)
    ev = db.execute(
        "SELECT * FROM evidence_file WHERE _id=? AND family_id=?",
        (ev_id, fam["id"]),
    ).fetchone()
    if not ev:
        raise HTTPException(404, "证据不存在")
    
    # 检查是否已确认
    if ev["review_status"] == "approved":
        raise HTTPException(400, "该证据已被确认过")
    
    # 获取任务信息
    task = db.execute(
        "SELECT * FROM task_definition WHERE _id=?", (ev["task_id"],)
    ).fetchone()
    if not task:
        raise HTTPException(404, "任务定义不存在")
    
    # 更新证据状态
    db.execute(
        "UPDATE evidence_file SET review_status='approved', reviewed_at=datetime('now') WHERE _id=?",
        (ev_id,),
    )
    
    # 计算分数
    score_awarded = task.get("point_value", 10)
    multiplier = 1.0
    
    # 如果是正计时任务，按实际时长加分
    if task["timing_mode"] == "countup" and ev.get("actual_duration"):
        actual_minutes = ev["actual_duration"] / 60.0
        expected_minutes = task.get("expected_duration_min", 30)
        multiplier = actual_minutes / expected_minutes
        score_awarded = int(score_awarded * multiplier)
    
    # 如果是倒计时任务且未超时，按剩余时间加分
    elif task["timing_mode"] == "countdown" and ev.get("time_remaining"):
        remaining = ev["time_remaining"]
        expected_minutes = task.get("expected_duration_min", 30)
        multiplier = remaining / (expected_minutes * 60.0)
        score_awarded = int(score_awarded * multiplier)
    
    # 如果是拍照打卡（无计时），直接加基础分
    elif task["timing_mode"] == "manual":
        score_awarded = task.get("point_value", 10)
    
    # 更新游戏得分
    db.execute(
        """INSERT INTO game_session (family_id, task_id, duration_sec, score_delta, metadata)
           VALUES (?, ?, ?, ?, ?)""",
        (
            fam["id"],
            ev["task_id"],
            ev.get("actual_duration", 0),
            score_awarded,
            json.dumps({
                "evidence_id": ev_id,
                "review_status": "approved",
                "multiplier": multiplier,
            }),
        ),
    )
    
    # 如果有总分表，也更新
    total_row = db.execute(
        "SELECT * FROM total_score WHERE family_id=?", (fam["id"],)
    ).fetchone()
    if total_row:
        current_total = total_row["total_score"] or 0
        db.execute(
            "UPDATE total_score SET total_score=?, updated_at=datetime('now') WHERE family_id=?",
            (current_total + score_awarded, fam["id"]),
        )
    
    return {
        "ok": True,
        "evidence_id": ev_id,
        "score_awarded": score_awarded,
        "multiplier": multiplier,
    }


@router.delete("/api/evidence/{ev_id}")
def delete_evidence(
    ev_id: int,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """删除单条证据记录及 OSS 对象。"""
    fam = auth_parent(db, authorization)
    row = db.execute(
        "SELECT * FROM evidence_file WHERE _id=? AND family_id=?",
        (ev_id, fam["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, "证据不存在")
    oss_key = row["oss_key"] or ""
    db.execute("DELETE FROM evidence_file WHERE _id=?", (ev_id,))
    if oss_key:
        oss_storage.delete(oss_key)
    return {"ok": True, "deleted": ev_id}
