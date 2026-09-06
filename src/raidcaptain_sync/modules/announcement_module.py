"""
公告模块 - RaidCaptain Sync Server v3.1
系统公告/通知：可附加跳转链接，按优先级分级。
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from raidcaptain_sync.services.event_bus import EventContext, EventKind, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


class AnnouncementCreate(BaseModel):
    title: str
    body: str
    priority: str = "normal"  # low | normal | high | urgent
    data: dict = Field(default_factory=dict)
    expires_at: Optional[int] = None


class AnnouncementModule(BaseModule):
    id = StandardModules.ANNOUNCEMENTS
    display_name = "公告系统"
    version = "1.0.0"
    description = "系统公告/通知"

    def __init__(self, get_db, bump_revision, ws_push, parent_sockets):
        self._get_db = get_db
        self._bump_revision = bump_revision
        self._ws_push = ws_push
        self._parent_sockets = parent_sockets
        self._routers: list = []
        self._build_routers()

    def _build_routers(self) -> None:
        r = APIRouter(prefix="/api", tags=["announcements"])

        @r.post("/parent/announcements")
        async def create_announcement(
            body: AnnouncementCreate,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            import json as _json
            now = int(time.time() * 1000)
            cur = db.execute(
                """INSERT INTO announcement(family_id, module_id, priority,
                    title, body, data, read, created_at, expires_at)
                   VALUES(?,?,?,?,?,?,0,?,?)""",
                (fam["id"], StandardModules.ANNOUNCEMENTS, body.priority,
                 body.title, body.body, _json.dumps(body.data),
                 now, body.expires_at),
            )
            aid = cur.lastrowid

            self._bump_revision(db, fam["id"])

            await event_bus.publish(EventContext(
                family_id=fam["id"], kind=EventKind.ANNOUNCEMENT_NEW,
                data={
                    "id": aid,
                    "title": body.title,
                    "priority": body.priority,
                    "data": body.data,
                },
                db=db,
            ))
            await self._ws_push(fam["id"], self._parent_sockets, {
                "type": "announcement_new",
                "id": aid,
                "title": body.title,
                "priority": body.priority,
            })

            return {"ok": True, "id": aid}

        @r.get("/parent/announcements")
        def list_announcements(
            unread_only: bool = False,
            limit: int = 50,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            sql = "SELECT * FROM announcement WHERE family_id=?"
            args = [fam["id"]]
            if unread_only:
                sql += " AND read=0"
            sql += " ORDER BY created_at DESC LIMIT ?"
            args.append(min(limit, 200))
            rows = db.execute(sql, args).fetchall()
            return {
                "announcements": [
                    {
                        "id": r["_id"],
                        "module_id": r["module_id"],
                        "priority": r["priority"],
                        "title": r["title"],
                        "body": r["body"],
                        "data": r["data"],
                        "read": bool(r["read"]),
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                    }
                    for r in rows
                ]
            }

        @r.post("/parent/announcements/{aid}/read")
        def mark_read(
            aid: int,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            db.execute(
                "UPDATE announcement SET read=1 WHERE _id=? AND family_id=?",
                (aid, fam["id"]),
            )
            return {"ok": True}

        @r.delete("/parent/announcements/{aid}")
        def delete_announcement(
            aid: int,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            db.execute(
                "DELETE FROM announcement WHERE _id=? AND family_id=?",
                (aid, fam["id"]),
            )
            return {"ok": True}

        self._routers = [r]


def create_announcement_module(get_db, bump_revision, ws_push,
                               parent_sockets) -> AnnouncementModule:
    return AnnouncementModule(get_db, bump_revision, ws_push, parent_sockets)