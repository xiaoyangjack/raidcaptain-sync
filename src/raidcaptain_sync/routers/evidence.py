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
