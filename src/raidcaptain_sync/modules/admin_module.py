"""
Admin Dashboard API 模块 - RaidCaptain Sync Server v3.3
=======================================================
提供剧情 Bundle、商品、动画的全局管理界面。

端点前缀: /api/admin

权限模型:
    admin  — 最高权限，可管理所有资源
    editor — 可增删改，不能删除 admin 账号
    viewer — 只读

使用方式:
    from raidcaptain_sync.modules.admin_module import create_admin_module
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any

import sqlite3
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from raidcaptain_sync.deps import get_db
from raidcaptain_sync.services.admin_auth import AdminAuthService
from raidcaptain_sync.services.economy import CurrencyKind


# ── 依赖 ────────────────────────────────────────────────────────

def auth_admin(
    authorization: Annotated[str, Header(...)],
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """验证并返回 admin 信息。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Authorization header")
    token = authorization[7:]
    info = AdminAuthService.verify_token(token)
    if not info:
        raise HTTPException(401, "Token 无效或已过期")
    # 补充角色信息
    row = db.execute(
        "SELECT admin_id, username, role FROM admin WHERE admin_id=?",
        (info["admin_id"],),
    ).fetchone()
    if not row:
        raise HTTPException(401, "管理员不存在")
    return dict(zip(["admin_id", "username", "role"], row))


AdminAuth = Annotated[dict, Depends(auth_admin)]


def require_role(*roles: str):
    """权限检查工厂（FastAPI 依赖注入）."""
    from raidcaptain_sync.services.admin_auth import AdminAuthService

    def dep(
        authorization: Annotated[str, Header(...)],
        db: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        admin = auth_admin(authorization, db)
        if admin["role"] not in roles:
            raise HTTPException(403, f"需要角色: {roles}")
        return admin
    return Depends(dep)


# ── 请求/响应模型 ──────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class AdminMe(BaseModel):
    admin_id: str
    username: str
    role: str


class BundleUploadRequest(BaseModel):
    bundle: dict
    target_families: list[str] | None = None  # None = 全部分发


class BundleItemResponse(BaseModel):
    bundle_id: str
    version: str
    title: str
    family_id: str | None
    chapter_count: int
    size_bytes: int
    schema_version: int
    is_active: bool
    created_at: int


class RewardItemRequest(BaseModel):
    item_id: str
    name: str
    description: str = ""
    icon_key: str = ""
    animation_key: str = ""
    price_currency: str = "points"
    price_amount: int
    stock: int | None = None
    requires_approval: bool = True
    display_order: int = 0
    tags: list[str] = Field(default_factory=list)
    active: bool = True


class RewardItemResponse(BaseModel):
    item_id: str
    name: str
    description: str
    icon_key: str
    animation_key: str
    price_currency: str
    price_amount: int
    stock: int | None
    requires_approval: bool
    display_order: int
    tags: list[str]
    active: bool
    created_at: int


class AnimationRegisterRequest(BaseModel):
    animation_id: str
    title: str
    description: str = ""
    duration_ms: int = 0
    oss_key: str = ""
    oss_checksum: str = ""
    oss_size_bytes: int = 0
    category: str = "general"
    thumbnail_key: str = ""
    tags: list[str] = Field(default_factory=list)


class AnimationItemResponse(BaseModel):
    animation_id: str
    title: str
    description: str
    duration_ms: int
    oss_key: str
    oss_checksum: str
    oss_size_bytes: int
    category: str
    thumbnail_key: str
    tags: list[str]
    created_at: int


class AuditLogResponse(BaseModel):
    _id: int
    admin_id: str
    admin_username: str
    action: str
    resource_type: str
    resource_id: str
    family_id: str | None
    details: dict
    created_at: int


class AuditLogRequest(BaseModel):
    resource_type: str | None = None
    admin_id: str | None = None
    limit: int = Field(default=50, le=200)
    offset: int = 0


# ── Module ─────────────────────────────────────────────────────

def create_admin_module(
    get_db, auth_parent, auth_device,
    ws_push, parent_sockets, bump_revision,
) -> "AdminModule":
    return AdminModule(get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision)


class AdminModule:
    id = "admin"
    display_name = "管理后台"
    version = "1.0.0"

    def __init__(self, get_db, auth_parent, auth_device,
                 ws_push, parent_sockets, bump_revision):
        self._get_db = get_db
        self._auth_parent = auth_parent
        self._auth_device = auth_device
        self._ws_push = ws_push
        self._parent_sockets = parent_sockets
        self._bump_revision = bump_revision
        # _routers must be set BEFORE _build_routers assigns to it
        self._routers: list = []
        self._build_routers()

    async def on_register(self, app):
        pass

    async def on_unregister(self):
        pass

    def get_routers(self) -> list:
        return self._routers

    def _build_routers(self) -> list:
        from raidcaptain_sync.deps import get_db as _gdb
        from raidcaptain_sync.services.admin_auth import AdminAuthService

        router = APIRouter(prefix="/api/admin", tags=["admin"])

        # ── 认证 ─────────────────────────────────────────────────

        @router.post("/login", summary="管理员登录")
        def login(body: LoginRequest, db: sqlite3.Connection = Depends(_gdb)):
            auth = AdminAuthService(db)
            token, info, expires = auth.login(body.username, body.password)
            return {"admin_token": token, "expires_at": expires, **info}

        @router.get("/me", summary="当前管理员信息", response_model=AdminMe)
        def me(admin: AdminAuth):
            return AdminMe(admin_id=admin["admin_id"],
                           username=admin["username"], role=admin["role"])

        # ── 剧情 Bundle 全局管理 ──────────────────────────────────

        @router.get("/storyline/bundles", summary="列出所有 Bundle")
        def list_bundles(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            bundle_id: str | None = None,
            family_id: str | None = None,
            limit: int = Query(50, le=200),
            offset: int = 0,
        ):
            auth = AdminAuthService(db)
            sql = """SELECT b.bundle_id, b.version, b.title, b.family_id,
                            b.chapter_count, b.size_bytes, b.schema_version,
                            b.is_active, b.created_at
                       FROM storyline_bundle b WHERE 1=1"""
            args = []
            if bundle_id:
                sql += " AND b.bundle_id=?"
                args.append(bundle_id)
            if family_id:
                sql += " AND b.family_id=?"
                args.append(family_id)
            sql += " ORDER BY b.created_at DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = db.execute(sql, args).fetchall()
            return {
                "bundles": [
                    dict(zip(
                        ["bundle_id", "version", "title", "family_id",
                         "chapter_count", "size_bytes", "schema_version",
                         "is_active", "created_at"], r))
                    for r in rows
                ]
            }

        @router.post("/storyline/bundles", summary="上传/创建 Bundle")
        def create_bundle(
            body: BundleUploadRequest,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            bundle = body.bundle
            bundle_id = bundle.get("bundle_id") or uuid.uuid4().hex
            version = bundle.get("version", "1.0.0")
            # admin 上传的 bundle 默认全局（family_id = "__global__"）
            family_id = bundle.get("family_id") or "__global__"
            now = int(time.time() * 1000)

            nodes = bundle.get("story_graph", {}).get("nodes", [])
            size_bytes = len(json.dumps(bundle, ensure_ascii=False).encode())

            db.execute("""
                INSERT INTO storyline_bundle(
                    bundle_id, version, title, family_id,
                    story_graph, reward_graph, migration,
                    chapter_count, size_bytes, schema_version,
                    bundle_json, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(bundle_id, version, family_id) DO UPDATE SET
                    title=excluded.title, story_graph=excluded.story_graph,
                    reward_graph=excluded.reward_graph,
                    migration=excluded.migration,
                    chapter_count=excluded.chapter_count,
                    size_bytes=excluded.size_bytes
            """, (
                bundle_id, version, bundle.get("title", ""),
                family_id,
                json.dumps(bundle.get("story_graph", {}), ensure_ascii=False),
                json.dumps(bundle.get("reward_graph", {}), ensure_ascii=False),
                json.dumps(bundle.get("migration", {}), ensure_ascii=False),
                len(nodes),
                size_bytes,
                bundle.get("schema_version", 2),
                json.dumps(bundle, ensure_ascii=False),
                now,
            ))
            db.commit()

            # 分发到指定家庭（或全部）
            families = body.target_families
            if not families:
                rows = db.execute(
                    "SELECT id FROM family LIMIT 1000"
                ).fetchall()
                families = [r[0] for r in rows]
            for fam in families:
                db.execute("""
                    INSERT OR IGNORE INTO storyline_subscription
                        (family_id, bundle_id, version, auto_download, distributed_at)
                    VALUES (?, ?, ?, 1, ?)
                """, (fam, bundle_id, version, now))
            db.commit()

            auth.audit_log(admin["admin_id"], "create_bundle",
                           "storyline", bundle_id,
                           details={"version": version, "families": families})
            return {"bundle_id": bundle_id, "version": version,
                    "distributed_to": len(families)}

        @router.post("/storyline/bundles/{bundle_id}/activate",
                     summary="激活 Bundle")
        def activate_bundle(
            bundle_id: str,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            db.execute(
                "UPDATE storyline_bundle SET is_active=1 WHERE bundle_id=?",
                (bundle_id,))
            db.commit()
            auth.audit_log(admin["admin_id"], "activate_bundle",
                           "storyline", bundle_id)
            return {"ok": True}

        @router.post("/storyline/bundles/{bundle_id}/deactivate",
                     summary="停用 Bundle")
        def deactivate_bundle(
            bundle_id: str,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            db.execute(
                "UPDATE storyline_bundle SET is_active=0 WHERE bundle_id=?",
                (bundle_id,))
            db.commit()
            auth.audit_log(admin["admin_id"], "deactivate_bundle",
                           "storyline", bundle_id)
            return {"ok": True}

        @router.delete("/storyline/bundles/{bundle_id}", summary="删除 Bundle")
        def delete_bundle(
            bundle_id: str,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            if admin["role"] == "viewer":
                raise HTTPException(403, "viewer 角色无权删除")
            auth = AdminAuthService(db)
            db.execute(
                "DELETE FROM storyline_bundle WHERE bundle_id=?",
                (bundle_id,))
            db.commit()
            auth.audit_log(admin["admin_id"], "delete_bundle",
                           "storyline", bundle_id)
            return {"ok": True}

        # ── 商品管理 ────────────────────────────────────────────────

        @router.get("/rewards/items", summary="列出所有商品")
        def list_items(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            active: bool | None = None,
            limit: int = Query(50, le=200),
            offset: int = 0,
        ):
            sql = "SELECT * FROM store_item WHERE 1=1"
            args = []
            if active is not None:
                sql += " AND active=?"
                args.append(1 if active else 0)
            sql += " ORDER BY display_order, created_at DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = db.execute(sql, args).fetchall()
            keys = ["item_id", "family_id", "name", "description",
                    "icon_key", "animation_key", "price_currency",
                    "price_amount", "stock", "requires_approval",
                    "display_order", "tags", "active", "created_at"]
            return {
                "items": [
                    {**dict(zip(keys, r)),
                     "tags": json.loads(r[11] or "[]"),
                     "active": bool(r[12])}
                    for r in rows
                ]
            }

        @router.post("/rewards/items", summary="创建商品")
        def create_item(
            body: RewardItemRequest,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            now = int(time.time() * 1000)
            db.execute("""
                INSERT INTO store_item(
                    item_id, family_id, name, description,
                    icon_key, animation_key, price_currency, price_amount,
                    stock, requires_approval, display_order, tags, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name=excluded.name, description=excluded.description,
                    icon_key=excluded.icon_key, animation_key=excluded.animation_key,
                    price_currency=excluded.price_currency, price_amount=excluded.price_amount,
                    stock=excluded.stock, requires_approval=excluded.requires_approval,
                    display_order=excluded.display_order, tags=excluded.tags,
                    active=1
            """, (
                body.item_id, None, body.name, body.description,
                body.icon_key, body.animation_key, body.price_currency,
                body.price_amount, body.stock,
                1 if body.requires_approval else 0,
                body.display_order, json.dumps(body.tags), now,
            ))
            db.commit()
            auth.audit_log(admin["admin_id"], "create_item",
                           "reward", body.item_id)
            return {"item_id": body.item_id}

        @router.patch("/rewards/items/{item_id}", summary="更新商品")
        def update_item(
            item_id: str,
            body: RewardItemRequest,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            db.execute("""
                UPDATE store_item SET
                    name=?, description=?, icon_key=?, animation_key=?,
                    price_currency=?, price_amount=?, stock=?,
                    requires_approval=?, display_order=?, tags=?, active=?
                WHERE item_id=?
            """, (
                body.name, body.description, body.icon_key,
                body.animation_key, body.price_currency, body.price_amount,
                body.stock, 1 if body.requires_approval else 0,
                body.display_order, json.dumps(body.tags),
                1 if body.active else 0, item_id,
            ))
            db.commit()
            auth.audit_log(admin["admin_id"], "update_item",
                           "reward", item_id)
            return {"ok": True}

        @router.delete("/rewards/items/{item_id}", summary="删除商品")
        def delete_item(
            item_id: str,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            if admin["role"] == "viewer":
                raise HTTPException(403, "viewer 角色无权删除")
            auth = AdminAuthService(db)
            db.execute(
                "UPDATE store_item SET active=0 WHERE item_id=?",
                (item_id,))
            db.commit()
            auth.audit_log(admin["admin_id"], "delete_item",
                           "reward", item_id)
            return {"ok": True}

        @router.post("/rewards/items/batch-active",
                     summary="批量上下架")
        def batch_active(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            item_ids: list[str] = Body(...),
            active: bool = Body(...),
        ):
            if not item_ids:
                return {"ok": True}
            placeholders = ",".join(["?"] * len(item_ids))
            db.execute(
                f"UPDATE store_item SET active=? WHERE item_id IN ({placeholders})",
                [(1 if active else 0)] + item_ids)
            db.commit()
            return {"ok": True, "updated": len(item_ids)}

        # ── 动画资源管理 ───────────────────────────────────────────

        @router.get("/animations/manifest", summary="全局动画清单")
        def list_animations(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            limit: int = Query(50, le=200),
            offset: int = 0,
        ):
            rows = db.execute("""
                SELECT * FROM animation_manifest
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """, [limit, offset]).fetchall()
            keys = ["animation_id", "title", "description", "duration_ms",
                    "oss_key", "oss_checksum", "oss_size_bytes", "category",
                    "thumbnail_key", "tags", "created_at"]
            return {
                "animations": [
                    {**dict(zip(keys, r)),
                     "tags": json.loads(r[9] or "[]")}
                    for r in rows
                ]
            }

        @router.post("/animations", summary="注册动画资源")
        def register_animation(
            body: AnimationRegisterRequest,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            auth = AdminAuthService(db)
            now = int(time.time() * 1000)
            db.execute("""
                INSERT INTO animation_manifest(
                    animation_id, title, description, duration_ms,
                    oss_key, oss_checksum, oss_size_bytes,
                    category, thumbnail_key, tags, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(animation_id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    duration_ms=excluded.duration_ms, oss_key=excluded.oss_key,
                    oss_checksum=excluded.oss_checksum,
                    oss_size_bytes=excluded.oss_size_bytes,
                    category=excluded.category,
                    thumbnail_key=excluded.thumbnail_key, tags=excluded.tags
            """, (
                body.animation_id, body.title, body.description,
                body.duration_ms, body.oss_key, body.oss_checksum,
                body.oss_size_bytes, body.category, body.thumbnail_key,
                json.dumps(body.tags), now, now,
            ))
            db.commit()
            auth.audit_log(admin["admin_id"], "register_animation",
                           "animation", body.animation_id,
                           details={"oss_key": body.oss_key})
            return {"animation_id": body.animation_id}

        @router.delete("/animations/{animation_id}", summary="删除动画")
        def delete_animation(
            animation_id: str,
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
        ):
            if admin["role"] == "viewer":
                raise HTTPException(403, "viewer 角色无权删除")
            auth = AdminAuthService(db)
            db.execute(
                "DELETE FROM animation_manifest WHERE animation_id=?",
                (animation_id,))
            db.commit()
            auth.audit_log(admin["admin_id"], "delete_animation",
                           "animation", animation_id)
            return {"ok": True}

        # ── 审计日志 ───────────────────────────────────────────────

        @router.get("/audit-log", summary="操作审计日志")
        def get_audit_log(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            resource_type: str | None = None,
            action: str | None = None,
            limit: int = Query(50, le=200),
            offset: int = 0,
        ):
            sql = """
                SELECT a._id, a.admin_id, a.action, a.resource_type,
                       a.resource_id, a.family_id, a.details, a.created_at,
                       ad.username as admin_username
                FROM admin_audit_log a
                JOIN admin ad ON ad.admin_id = a.admin_id
                WHERE 1=1
            """
            args = []
            if resource_type:
                sql += " AND a.resource_type=?"
                args.append(resource_type)
            if action:
                sql += " AND a.action=?"
                args.append(action)
            sql += " ORDER BY a.created_at DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = db.execute(sql, args).fetchall()
            return {
                "logs": [
                    {
                        "_id": r[0], "admin_id": r[1], "action": r[2],
                        "resource_type": r[3], "resource_id": r[4],
                        "family_id": r[5], "details": json.loads(r[6] or "{}"),
                        "created_at": r[7], "admin_username": r[8],
                    }
                    for r in rows
                ]
            }

        # ── 家庭管理（只读）────────────────────────────────────────

        @router.get("/families", summary="家庭列表（只读）")
        def list_families(
            admin: AdminAuth,
            db: sqlite3.Connection = Depends(_gdb),
            limit: int = Query(50, le=200),
            offset: int = 0,
        ):
            rows = db.execute("""
                SELECT id as family_id, id as family_code, created_at FROM family
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """, [limit, offset]).fetchall()
            return {
                "families": [
                    dict(zip(["family_id", "family_code", "created_at"], r))
                    for r in rows
                ]
            }

        self._routers = [router]


def create_admin_module(get_db, auth_parent, auth_device,
                        ws_push, parent_sockets, bump_revision):
    return AdminModule(get_db, auth_parent, auth_device,
                       ws_push, parent_sockets, bump_revision)
