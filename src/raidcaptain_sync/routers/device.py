"""
设备事件上报 + 证据处理路由 - RAID Captain Sync
核心变更：照片数据通过 OSS 存储，DB 只存 OSS key。
"""
import base64
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException

from raidcaptain_sync.deps import (
    auth_device,
    device_sockets,
    get_db,
    parent_sockets,
    ws_push,
)
from raidcaptain_sync.services.oss_storage import oss_storage

router = APIRouter()


@router.post("/api/events")
async def device_push_events(
    body: dict,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """设备批量上报事件（含 base64 证据 → 自动上传 OSS）。"""
    dev = auth_device(db, authorization)
    fid = dev["family_id"]
    events = body.get("events")
    if not isinstance(events, list) or len(events) > 100:
        raise HTTPException(400, "events 必须为 1~100 条的数组")

    now = int(time.time())
    stored = []

    for e in events:
        kind = str(e.get("kind") or "").strip()
        if not kind:
            raise HTTPException(400, "事件缺少 kind")
        payload = e.get("data")
        created = int(e.get("created_at") or now * 1000)

        db.execute(
            "INSERT INTO event(family_id, device_name, kind, payload, created_at) "
            "VALUES(?,?,?,?,?)",
            (fid, dev["name"], kind,
             json.dumps(payload, ensure_ascii=False) if payload is not None else "{}",
             created),
        )
        ev_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # ── 证据处理：OSS 上传 + DB 记录 ─────────────────────
        ev_b64 = (payload or {}).get("evidence_b64") if payload else None
        if ev_b64 and isinstance(ev_b64, str) and len(ev_b64) > 100:
            try:
                oss_key, size_bytes = oss_storage.upload_evidence(
                    fid, dev["name"], ev_b64
                )
            except ValueError as ve:
                raise HTTPException(400, str(ve))
            ev_task_id = str((payload or {}).get("task_id", "")) if kind == "task_completion" else ""
            ev_task_title = str((payload or {}).get("title", "")) if kind == "task_completion" else (
                f"申诉凭证 session={(payload or {}).get('session_id', '')}" if kind == "appeal_submitted" else ""
            )
            db.execute(
                """INSERT INTO evidence_file(family_id, event_id, task_id, task_title,
                    device_name, mime, data_b64, oss_key, size_bytes, created_at, appeal_session_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, ev_id, ev_task_id, ev_task_title, dev["name"],
                 "image/jpeg", ev_b64, oss_key, size_bytes, created, ev_appeal_sid),
            )

        # ── 申诉提交 ────────────────────────────────────────
        if kind == "appeal_submitted":
            db.execute(
                """INSERT OR IGNORE INTO appeal(family_id, device_name, session_id,
                    reason, evidence_photo, verdict, submitted_at, result)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (fid, dev["name"],
                 str((payload or {}).get("session_id", "")),
                 str((payload or {}).get("reason", "")),
                 str((payload or {}).get("evidence_photo", "")),
                 str((payload or {}).get("verdict", "CAUGHT")),
                 created, "PENDING"),
            )

        # ── 学习时段记录 ────────────────────────────────────
        if kind == "mission_result" and payload:
            db.execute(
                """INSERT INTO patrol_session(family_id, device_name, started_at, ended_at,
                    valid_minutes, points_delta, merit_delta, sessions, task_name, outcome)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fid, dev["name"],
                 int(payload.get("start_ms", created * 1000) / 1000),
                 int(payload.get("end_ms", created * 1000) / 1000),
                 int(payload.get("valid_minutes", 0)),
                 int(payload.get("points_delta", 0)),
                 int(payload.get("merit_delta", 0)),
                 int(payload.get("sessions", 0)),
                 str(payload.get("task", payload.get("task_id", ""))),
                 str(payload.get("outcome", ""))),
            )

        stored.append({
            "kind": kind,
            "data": payload or {},
            "device_name": dev["name"],
            "created_at": created,
        })

    # 实时推送给在线家长
    for ev in stored:
        await ws_push(fid, parent_sockets, {"type": "event", "event": ev})

    return {"ok": True}
