"""
动画分发模块 - RaidCaptain Sync Server v3.2
MP4 动画资源：清单 + 下载链接 + 缓存追踪。

工作流：
1. 制作新动画 MP4 → 上传 OSS → 在服务器注册到 animation_manifest
2. 设备端首包：启动时拉取 GET /api/animations/manifest
   → 遍历，按 checksum 比对本地缓存
   → 不存在/不匹配 → 调用 GET /api/animations/{id}/download-url 下载
   → 调用 POST /api/animations/{id}/track-cached 标记已缓存
3. 家长端：查询动画下载清单 + 跟踪家庭缓存
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.oss_storage import oss_storage


# ── Schemas ────────────────────────────────────────────────────────


class AnimationRegister(BaseModel):
    animation_id: str
    title: str
    version: str = "1.0.0"
    duration_ms: int = 0
    mime: str = "video/mp4"
    oss_key: str = Field(..., description="OSS 对象 key")
    tags: list[str] = Field(default_factory=list)
    related_tier_id: str = ""
    related_item_id: str = ""
    required_for: list[str] = Field(default_factory=list)  # ["rank_promotion", "exchange_animation"]


# ── Module ──────────────────────────────────────────────────────


class AnimationModule(BaseModule):
    """动画资源分发：清单 / 下载 / 缓存跟踪。"""

    id = "animations"
    display_name = "动画分发"
    version = "1.0.0"
    description = "MP4 动画资源清单 + OSS 下载 + 首包集成支持"

    def __init__(self, get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision):
        self._get_db = get_db
        self._auth_parent = auth_parent
        self._auth_device = auth_device
        self._ws_push = ws_push
        self._sockets = parent_sockets
        self._bump_revision = bump_revision
        self._routers: list = []
        self._build_routers()

    async def on_register(self, app) -> None:
        try:
            with self._get_db() as db:
                self._ensure_schema(db)
        except Exception:
            pass

    def _open_db(self):
        """每次独立打开数据库连接."""
        import sqlite3
        from raidcaptain_sync.config import settings
        db = sqlite3.connect(str(settings.db_path), timeout=30.0, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return db

    def _ensure_schema(self, db) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS animation_manifest(
                animation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '1.0.0',
                description TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                mime TEXT NOT NULL DEFAULT 'video/mp4',
                oss_key TEXT NOT NULL,
                oss_checksum TEXT NOT NULL DEFAULT '',
                oss_size_bytes INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                checksum TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL DEFAULT 'general',
                thumbnail_key TEXT NOT NULL DEFAULT '',
                related_tier_id TEXT NOT NULL DEFAULT '',
                related_item_id TEXT NOT NULL DEFAULT '',
                required_for TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS family_animation_cache(
                family_id TEXT NOT NULL,
                animation_id TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '',
                cached_at INTEGER NOT NULL,
                checksum_verified TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(family_id, animation_id)
            )
        """)

    def _row_to_dict(self, r) -> dict:
        keys = ["animation_id", "title", "version", "duration_ms", "mime",
                "oss_key", "size_bytes", "checksum", "tags", "related_tier_id",
                "related_item_id", "required_for", "created_at", "updated_at"]
        d = dict(zip(keys, r))
        d["tags"] = json.loads(d["tags"] or "[]")
        d["required_for"] = json.loads(d["required_for"] or "[]")
        return d

    def _build_routers(self) -> None:
        from raidcaptain_sync.deps import get_db as _gdb
        parent_router = APIRouter(prefix="/api/parent/animations", tags=["animations"])
        device_router = APIRouter(prefix="/api/device/animations", tags=["animations"])
        public_router = APIRouter(prefix="/api/animations", tags=["animations"])

        @parent_router.post("/register", summary="注册动画资源")
        async def register_animation(
            body: AnimationRegister,
            db: sqlite3.Connection = Depends(_gdb),
            authorization: str = Header(...),
        ):
            self._ensure_schema(db)
            self._auth_parent(db, authorization)
            now = int(time.time() * 1000)

            # 计算 OSS 文件大小
            size_bytes = 0
            try:
                meta = oss_storage._bucket.get_object_meta(body.oss_key)
                size_bytes = meta.content_length
            except Exception:
                pass

            db.execute(
                """INSERT INTO animation_manifest(animation_id, title, version,
                    duration_ms, mime, oss_key, size_bytes, checksum,
                    tags, related_tier_id, related_item_id, required_for,
                    created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(animation_id) DO UPDATE SET
                    title=excluded.title, version=excluded.version,
                    duration_ms=excluded.duration_ms, mime=excluded.mime,
                    oss_key=excluded.oss_key, size_bytes=excluded.size_bytes,
                    tags=excluded.tags, related_tier_id=excluded.related_tier_id,
                    related_item_id=excluded.related_item_id,
                    required_for=excluded.required_for,
                    updated_at=excluded.updated_at""",
                (body.animation_id, body.title, body.version, body.duration_ms,
                 body.mime, body.oss_key, size_bytes, "",
                 json.dumps(body.tags), body.related_tier_id,
                 body.related_item_id, json.dumps(body.required_for),
                 now, now),
            )
            db.commit()
            self._bump_revision(db, "animations", "global")
            return {"animation_id": body.animation_id, "size_bytes": size_bytes}

        @parent_router.get("/manifest", summary="所有动画清单")
        def list_manifest(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            self._auth_parent(db, authorization)
            rows = db.execute(
                "SELECT * FROM animation_manifest ORDER BY created_at DESC"
            ).fetchall()
            return {"animations": [self._row_to_dict(r) for r in rows]}

        @parent_router.get("/checklist/{family_id}", summary="家庭下载清单")
        def checklist(family_id: str, db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            self._auth_parent(db, authorization)
            cached = db.execute(
                "SELECT animation_id, version, checksum_verified FROM family_animation_cache WHERE family_id=?",
                (family_id,),
            ).fetchall()
            cached_map = {r[0]: {"version": r[1], "checksum": r[2]} for r in cached}
            manifest = db.execute(
                "SELECT * FROM animation_manifest ORDER BY created_at DESC"
            ).fetchall()
            checklist = []
            for r in manifest:
                d = self._row_to_dict(r)
                c = cached_map.get(d["animation_id"])
                if not c:
                    checklist.append({**d, "status": "MISSING"})
                elif c["version"] != d["version"]:
                    checklist.append({**d, "status": "OUTDATED"})
                elif c["checksum"] and c["checksum"] != d["checksum"]:
                    checklist.append({**d, "status": "CORRUPTED"})
                else:
                    checklist.append({**d, "status": "OK"})
            return {"checklist": checklist}

        # ── 公共 API（设备端 + 家长端共用）────────────────────

        @public_router.get("/manifest", summary="公开清单（首包集成）")
        def public_manifest(db: sqlite3.Connection = Depends(_gdb)):
            self._ensure_schema(db)
            rows = db.execute(
                "SELECT * FROM animation_manifest ORDER BY created_at DESC"
            ).fetchall()
            return {"animations": [self._row_to_dict(r) for r in rows]}

        @public_router.get("/{animation_id}/download-url", summary="OSS 下载链接")
        def get_download_url(animation_id: str, db: sqlite3.Connection = Depends(_gdb)):
            self._ensure_schema(db)
            row = db.execute(
                "SELECT oss_key FROM animation_manifest WHERE animation_id=?",
                (animation_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "动画不存在")
            url = oss_storage.get_url(row[0], expires_seconds=7200)
            if not url:
                raise HTTPException(500, "OSS 未配置")
            return {"animation_id": animation_id, "url": url, "expires_in": 7200}

        @public_router.get("/{animation_id}/info", summary="动画详情")
        def info(animation_id: str, db: sqlite3.Connection = Depends(_gdb)):
            self._ensure_schema(db)
            row = db.execute(
                "SELECT * FROM animation_manifest WHERE animation_id=?",
                (animation_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "动画不存在")
            return self._row_to_dict(row)

        # ── 设备端 ────────────────────────────────────────────────

        @device_router.get("/checklist", summary="设备缓存检查")
        def device_checklist(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            cached = db.execute(
                "SELECT animation_id, version, checksum_verified FROM family_animation_cache WHERE family_id=?",
                (dev["family_id"],),
            ).fetchall()
            cached_map = {r[0]: {"version": r[1], "checksum": r[2]} for r in cached}
            manifest = db.execute(
                "SELECT * FROM animation_manifest ORDER BY created_at DESC"
            ).fetchall()
            items = []
            for r in manifest:
                d = self._row_to_dict(r)
                c = cached_map.get(d["animation_id"])
                if not c:
                    items.append({**d, "status": "MISSING"})
                elif c["version"] != d["version"]:
                    items.append({**d, "status": "OUTDATED"})
                elif c["checksum"] and c["checksum"] != d["checksum"]:
                    items.append({**d, "status": "CORRUPTED"})
            return {"checklist": items}

        @device_router.post("/{animation_id}/track-cached", summary="标记已缓存")
        async def track_cached(
            animation_id: str, authorization: str = Header(...),
        ):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            row = db.execute(
                "SELECT version FROM animation_manifest WHERE animation_id=?",
                (animation_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "动画不存在")
            now = int(time.time() * 1000)
            db.execute(
                """INSERT INTO family_animation_cache(family_id, animation_id,
                    version, cached_at, checksum_verified)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(family_id, animation_id) DO UPDATE SET
                    version=excluded.version, cached_at=excluded.cached_at""",
                (dev["family_id"], animation_id, row[0], now, ""),
            )
            db.commit()

            await event_bus.publish(EventContext(
                family_id=dev["family_id"], kind=EventKind.ANIMATION_PLAYED,
                data={"animation_id": animation_id, "event": "cached"},
            ))
            return {"ok": True, "animation_id": animation_id}

        @device_router.post("/{animation_id}/played", summary="标记已播放")
        async def track_played(
            animation_id: str, authorization: str = Header(...),
        ):
            dev = self._auth_device(db, authorization)
            await event_bus.publish(EventContext(
                family_id=dev["family_id"], kind=EventKind.ANIMATION_PLAYED,
                data={"animation_id": animation_id, "event": "played",
                      "device_name": dev["name"]},
            ))
            await self._ws_push(dev["family_id"], self._sockets, {
                "type": "animation_played",
                "animation_id": animation_id,
                "device_name": dev["name"],
            })
            return {"ok": True}

        self._routers = [parent_router, device_router, public_router]


def create_animation_module(
    get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision,
):
    return AnimationModule(get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision)