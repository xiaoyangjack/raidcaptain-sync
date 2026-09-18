"""
WebSocket 实时通道 - RAID Captain Sync
保持与原 API 完全兼容的 /ws/device 和 /ws/parent 端点。
"""
import json
import time

from contextlib import contextmanager

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from raidcaptain_sync.deps import (
    device_sockets,
    get_db,
    parent_sockets,
    ws_push,
)
from raidcaptain_sync.services.auth import sha256_hex

router = APIRouter()


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


def _db():
    """从生成器获取 SQLite 连接，由调用方负责最终关闭。"""
    gen = get_db()
    return next(gen), gen


@router.websocket("/ws/device")
async def ws_device(ws: WebSocket, token: str = ""):
    """设备端 WebSocket：心跳 + 实时状态通知。"""
    conn, gen = _db()
    try:
        row = conn.execute(
            "SELECT * FROM device WHERE token_hash=?",
            (sha256_hex(token),),
        ).fetchone()
    finally:
        gen.close()
    if not row:
        await ws.close(code=4401)
        return
    fid = row["family_id"]
    await ws.accept()
    device_sockets.setdefault(fid, []).append(ws)

    # 更新 last_seen
    conn2, gen2 = _db()
    try:
        conn2.execute(
            "UPDATE device SET last_seen=? WHERE token_hash=?",
            (int(time.time()), sha256_hex(token)),
        )
    finally:
        gen2.close()

    # 上线通知家长
    await ws_push(fid, parent_sockets, {
        "type": "device_status",
        "name": row["name"],
        "online": True,
        "last_seen_text": _seen_text(int(time.time())),
        "seen_text": _seen_text(int(time.time())),
    })

    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                conn3, gen3 = _db()
                try:
                    conn3.execute(
                        "UPDATE device SET last_seen=? WHERE token_hash=?",
                        (int(time.time()), sha256_hex(token)),
                    )
                finally:
                    gen3.close()
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 下线通知家长
        try:
            device_sockets.get(fid, []).remove(ws)
        except ValueError:
            pass
        await ws_push(fid, parent_sockets, {
            "type": "device_status",
            "name": row["name"],
            "online": False,
            "last_seen_text": _seen_text(row["last_seen"]),
            "seen_text": _seen_text(row["last_seen"]),
        })


@router.websocket("/ws/parent")
async def ws_parent(ws: WebSocket, token: str = ""):
    """家长端 WebSocket：实时战况推送。"""
    conn, gen = _db()
    try:
        row = conn.execute(
            "SELECT id FROM family WHERE parent_token=? AND parent_token_exp>?",
            (sha256_hex(token), int(time.time())),
        ).fetchone()
    finally:
        gen.close()
    if not row:
        await ws.close(code=4401)
        return
    fid = row["id"]
    await ws.accept()
    parent_sockets.setdefault(fid, []).append(ws)

    # 连接即发送最近 30 条事件（防止刷新/重连丢事件）
    conn2, gen2 = _db()
    try:
        rows = conn2.execute(
            "SELECT kind, payload, device_name, created_at FROM event "
            "WHERE family_id=? ORDER BY _id DESC LIMIT 30",
            (fid,),
        ).fetchall()
        backlog = [
            {
                "kind": r["kind"],
                "data": json.loads(r["payload"]),
                "device_name": r["device_name"],
                "created_at": r["created_at"],
            }
            for r in reversed(rows)
        ]
        await ws.send_json({"type": "events_backlog", "events": backlog})
    except Exception:
        pass
    finally:
        gen2.close()

    try:
        while True:
            await ws.receive_json()  # 心跳占位
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            parent_sockets.get(fid, []).remove(ws)
        except ValueError:
            pass
