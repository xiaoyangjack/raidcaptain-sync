"""
奖励商店模块 - RaidCaptain Sync Server v3.2
孩子发起兑换请求 → 家长审核 → 扣款 → 发放物品 → 触发过场动画

兑换流程：
1. 孩子 POST /api/device/rewards/exchange {item_id}
   → 创建 EXCHANGE_REQUESTED 记录（status=PENDING）
   → WS 推送给家长端
2. 家长 GET /api/parent/rewards/pending → 列表
   → POST /api/parent/rewards/exchanges/{id}/approve
   → POST /api/parent/rewards/exchanges/{id}/reject
3. 通过 → 扣减积分/货币 → 库存 +1 → 播放过场动画
4. 触发 EXCHANGE_COMPLETED 事件 → RankModule 可联动
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from raidcaptain_sync.services.economy import EconomyService, CurrencyKind
from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


# ── Schemas ────────────────────────────────────────────────────────


class ItemCreate(BaseModel):
    item_id: str
    name: str
    description: str = ""
    icon_key: str = ""
    animation_key: str = Field("", description="兑换过场动画 OSS key")
    price_currency: str = "merit"   # ✅ Phase 6 修复：默认从 points 改为 merit（Android 端 prize.points_cost 实际消耗 merit_balance）
    price_amount: int = Field(ge=0)
    stock: Optional[int] = None
    requires_approval: bool = True
    display_order: int = 0
    tags: list[str] = Field(default_factory=list)


class ExchangeRequest(BaseModel):
    item_id: str
    quantity: int = 1


class ApprovalDecision(BaseModel):
    note: str = ""


# ── Module ──────────────────────────────────────────────────────


class RewardModule(BaseModule):
    """奖励商店 + 兑换审核。"""

    id = "rewards"
    display_name = "奖励商店"
    version = "1.0.0"
    description = "物品上架 + 兑换请求 + 家长审核 + 过场动画"

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
        """每次独立打开数据库连接（绕开 deps.get_db generator 单次性）."""
        import sqlite3
        from raidcaptain_sync.config import settings
        db = sqlite3.connect(str(settings.db_path), timeout=30.0, check_same_thread=False)
        db.row_factory = sqlite3.Row  # auth_parent 依赖 Row 索引
        # WAL 模式允许多读单写并发
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return db

    def _ensure_schema(self, db) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS store_item(
                item_id TEXT PRIMARY KEY,
                family_id TEXT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                icon_key TEXT NOT NULL DEFAULT '',
                animation_key TEXT NOT NULL DEFAULT '',
                price_currency TEXT NOT NULL DEFAULT 'merit',
                price_amount INTEGER NOT NULL DEFAULT 0,
                stock INTEGER,
                requires_approval INTEGER NOT NULL DEFAULT 1,
                display_order INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS exchange_request(
                request_id TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                device_name TEXT NOT NULL DEFAULT '',
                device_token_hash TEXT NOT NULL DEFAULT '',
                item_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                price_currency TEXT NOT NULL DEFAULT 'merit',
                price_total INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING',
                requested_at INTEGER NOT NULL,
                reviewed_at INTEGER,
                reviewed_by TEXT,
                note TEXT NOT NULL DEFAULT '',
                animation_key TEXT NOT NULL DEFAULT ''
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS family_inventory(
                family_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                acquired_at INTEGER NOT NULL,
                PRIMARY KEY(family_id, item_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_exchange_family_status ON exchange_request(family_id, status, requested_at DESC)")

    def _get_item(self, db, family_id: str, item_id: str) -> Optional[dict]:
        row = db.execute(
            "SELECT * FROM store_item WHERE item_id=? AND (family_id IS NULL OR family_id=?) AND active=1",
            (item_id, family_id),
        ).fetchone()
        if not row:
            return None
        keys = ["item_id", "family_id", "name", "description", "icon_key",
                "animation_key", "price_currency", "price_amount", "stock",
                "requires_approval", "display_order", "tags", "active", "created_at"]
        d = dict(zip(keys, row))
        d["tags"] = json.loads(d["tags"] or "[]")
        d["requires_approval"] = bool(d["requires_approval"])
        d["active"] = bool(d["active"])
        return d

    def _build_routers(self) -> None:
        from raidcaptain_sync.deps import get_db as _gdb
        parent_router = APIRouter(prefix="/api/parent/rewards", tags=["rewards"])
        device_router = APIRouter(prefix="/api/device/rewards", tags=["rewards"])

        # ── 家长端 ────────────────────────────────────────────────

        @parent_router.post("/items", summary="上架商品")
        def create_item(body: ItemCreate, db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            now = int(time.time() * 1000)
            db.execute(
                """INSERT INTO store_item(item_id, family_id, name, description,
                    icon_key, animation_key, price_currency, price_amount, stock,
                    requires_approval, display_order, tags, active, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(item_id) DO UPDATE SET
                    family_id=excluded.family_id, name=excluded.name,
                    description=excluded.description, icon_key=excluded.icon_key,
                    animation_key=excluded.animation_key,
                    price_currency=excluded.price_currency, price_amount=excluded.price_amount,
                    stock=excluded.stock, requires_approval=excluded.requires_approval,
                    display_order=excluded.display_order, tags=excluded.tags,
                    active=1""",
                (body.item_id, fam["id"], body.name, body.description,
                 body.icon_key, body.animation_key, body.price_currency,
                 body.price_amount, body.stock,
                 1 if body.requires_approval else 0,
                 body.display_order, json.dumps(body.tags), now),
            )
            db.commit()
            self._bump_revision(db, "rewards", fam["id"])
            return {"item_id": body.item_id}

        @parent_router.get("/items", summary="商品列表")
        def list_items(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            rows = db.execute(
                """SELECT * FROM store_item
                   WHERE family_id IS NULL OR family_id=?
                   ORDER BY display_order, created_at DESC""",
                (fam["id"],),
            ).fetchall()
            return {
                "items": [
                    {**self._row_to_item_dict(r), "stock_remaining": r["stock"]}
                    for r in rows
                ]
            }

        @parent_router.delete("/items/{item_id}", summary="下架商品")
        def delete_item(item_id: str, db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            fam = self._auth_parent(db, authorization)
            db.execute(
                "UPDATE store_item SET active=0 WHERE item_id=? AND (family_id IS NULL OR family_id=?)",
                (item_id, fam["id"]),
            )
            db.commit()
            self._bump_revision(db, "rewards", fam["id"])
            return {"ok": True}

        @parent_router.get("/pending", summary="待审核列表")
        def pending_list(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            rows = db.execute(
                """SELECT * FROM exchange_request
                   WHERE family_id=? AND status='PENDING'
                   ORDER BY requested_at ASC""",
                (fam["id"],),
            ).fetchall()
            return {
                "requests": [
                    {
                        "request_id": r["request_id"],
                        "device_name": r["device_name"],
                        "item_id": r["item_id"],
                        "item_name": self._get_item(db, fam["id"], r["item_id"])["name"]
                                    if self._get_item(db, fam["id"], r["item_id"]) else r["item_id"],
                        "quantity": r["quantity"],
                        "price_total": r["price_total"],
                        "price_currency": r["price_currency"],
                        "requested_at": r["requested_at"],
                        "animation_key": r["animation_key"],
                    }
                    for r in rows
                ]
            }

        @parent_router.post("/exchanges/{request_id}/approve", summary="通过")
        async def approve(request_id: str, db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)

            req = db.execute(
                "SELECT * FROM exchange_request WHERE request_id=? AND family_id=?",
                (request_id, fam["id"]),
            ).fetchone()
            if not req:
                raise HTTPException(404, "兑换请求不存在")
            req = dict(zip(["request_id", "family_id", "device_name", "device_token_hash",
                            "item_id", "quantity", "price_currency", "price_total",
                            "status", "requested_at", "reviewed_at", "reviewed_by",
                            "note", "animation_key"], req))
            if req["status"] != "PENDING":
                raise HTTPException(400, f"状态为 {req['status']}, 不可重复处理")

            # 扣款 + 发物品
            eco = EconomyService(db)
            ok, _, err = eco.transfer(
                fam["id"], CurrencyKind(req["price_currency"]),
                -req["price_total"], "exchange_approved", request_id,
            )
            if not ok:
                raise HTTPException(400, f"扣款失败: {err}")

            # 库存 +1
            db.execute(
                """INSERT INTO family_inventory(family_id, item_id, quantity, acquired_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(family_id, item_id) DO UPDATE SET
                    quantity=quantity+excluded.quantity, acquired_at=excluded.acquired_at""",
                (fam["id"], req["item_id"], req["quantity"], int(time.time() * 1000)),
            )

            # 更新请求状态
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE exchange_request SET status='APPROVED', reviewed_at=?, reviewed_by=? WHERE request_id=?",
                (now, fam["id"], request_id),
            )
            db.commit()
            self._bump_revision(db, "rewards", fam["id"])

            # 事件 + WS
            await event_bus.publish(EventContext(
                family_id=fam["id"], kind=EventKind.EXCHANGE_COMPLETED,
                data={
                    "request_id": request_id,
                    "item_id": req["item_id"],
                    "quantity": req["quantity"],
                    "animation_key": req["animation_key"],
                },
            ))

            await self._ws_push(fam["id"], self._sockets, {
                "type": "exchange_completed",
                "request_id": request_id,
                "item_id": req["item_id"],
                "animation_key": req["animation_key"],
            })

            return {"ok": True, "request_id": request_id, "status": "APPROVED"}

        @parent_router.post("/exchanges/{request_id}/reject", summary="驳回")
        async def reject(
            request_id: str, body: ApprovalDecision,
            authorization: str = Header(...),
        ):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            req = db.execute(
                "SELECT status FROM exchange_request WHERE request_id=? AND family_id=?",
                (request_id, fam["id"]),
            ).fetchone()
            if not req:
                raise HTTPException(404, "兑换请求不存在")
            if req["status"] != "PENDING":
                raise HTTPException(400, "状态已变更")
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE exchange_request SET status='REJECTED', reviewed_at=?, reviewed_by=?, note=? WHERE request_id=?",
                (now, fam["id"], body.note, request_id),
            )
            db.commit()
            self._bump_revision(db, "rewards", fam["id"])

            await event_bus.publish(EventContext(
                family_id=fam["id"], kind=EventKind.EXCHANGE_REJECTED,
                data={"request_id": request_id, "note": body.note},
            ))
            await self._ws_push(fam["id"], self._sockets, {
                "type": "exchange_rejected",
                "request_id": request_id,
                "note": body.note,
            })
            return {"ok": True, "status": "REJECTED"}

        @parent_router.get("/inventory", summary="家庭库存")
        def inventory(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            rows = db.execute(
                """SELECT i.item_id, i.quantity, i.acquired_at,
                          s.name, s.icon_key, s.animation_key
                   FROM family_inventory i
                   LEFT JOIN store_item s ON s.item_id = i.item_id
                   WHERE i.family_id=?""",
                (fam["id"],),
            ).fetchall()
            return {
                "items": [
                    {
                        "item_id": r["item_id"],
                        "name": r["name"] or r["item_id"],
                        "icon_key": r["icon_key"],
                        "animation_key": r["animation_key"],
                        "quantity": r["quantity"],
                        "acquired_at": r["acquired_at"],
                    }
                    for r in rows
                ]
            }

        @parent_router.get("/exchanges", summary="兑换历史")
        def exchange_history(
            status: str = "", limit: int = 50,
            authorization: str = Header(...),
        ):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            sql = "SELECT * FROM exchange_request WHERE family_id=?"
            args = [fam["id"]]
            if status:
                sql += " AND status=?"
                args.append(status)
            sql += " ORDER BY requested_at DESC LIMIT ?"
            args.append(min(limit, 200))
            rows = db.execute(sql, args).fetchall()
            return {
                "requests": [
                    {
                        "request_id": r["request_id"],
                        "device_name": r["device_name"],
                        "item_id": r["item_id"],
                        "quantity": r["quantity"],
                        "price_total": r["price_total"],
                        "price_currency": r["price_currency"],
                        "status": r["status"],
                        "requested_at": r["requested_at"],
                        "reviewed_at": r["reviewed_at"],
                        "reviewed_by": r["reviewed_by"],
                        "note": r["note"],
                        "animation_key": r["animation_key"],
                    }
                    for r in rows
                ]
            }

        # ── 设备端 ────────────────────────────────────────────────

        @device_router.get("/items", summary="可兑换商品")
        def device_items(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            rows = db.execute(
                """SELECT * FROM store_item
                   WHERE (family_id IS NULL OR family_id=?) AND active=1
                   ORDER BY display_order, created_at DESC""",
                (dev["family_id"],),
            ).fetchall()
            # 附加余额信息
            eco = EconomyService(db)
            balances = eco.get_all_balances(dev["family_id"])
            return {
                "items": [
                    {**self._row_to_item_dict(r), "stock_remaining": r["stock"]}
                    for r in rows
                ],
                "balances": balances,
            }

        @device_router.post("/exchange", summary="发起兑换")
        async def create_exchange(body: ExchangeRequest, db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            item = self._get_item(db, dev["family_id"], body.item_id)
            if not item:
                raise HTTPException(404, "商品不存在")
            if item["stock"] is not None and item["stock"] < body.quantity:
                raise HTTPException(400, "库存不足")

            price_total = item["price_amount"] * body.quantity
            request_id = uuid.uuid4().hex
            now = int(time.time() * 1000)

            db.execute(
                """INSERT INTO exchange_request(request_id, family_id, device_name,
                    device_token_hash, item_id, quantity, price_currency, price_total,
                    status, requested_at, animation_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, dev["family_id"], dev["name"], dev["token_hash"],
                 body.item_id, body.quantity, item["price_currency"], price_total,
                 "PENDING", now, item["animation_key"]),
            )
            db.commit()

            # 事件 + 推送
            await event_bus.publish(EventContext(
                family_id=dev["family_id"], kind=EventKind.EXCHANGE_REQUESTED,
                data={"request_id": request_id, "item_id": body.item_id,
                      "quantity": body.quantity, "price_total": price_total},
            ))

            await self._ws_push(dev["family_id"], self._sockets, {
                "type": "exchange_requested",
                "request_id": request_id,
                "item_id": body.item_id,
                "device_name": dev["name"],
                "quantity": body.quantity,
                "price_total": price_total,
                "requires_approval": item["requires_approval"],
            })

            return {
                "ok": True,
                "request_id": request_id,
                "status": "PENDING",
                "requires_approval": item["requires_approval"],
                "price_total": price_total,
                "animation_key": item["animation_key"],
            }

        @device_router.get("/inventory", summary="我的库存")
        def device_inventory(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            rows = db.execute(
                """SELECT i.item_id, i.quantity, i.acquired_at,
                          s.name, s.icon_key, s.animation_key
                   FROM family_inventory i
                   LEFT JOIN store_item s ON s.item_id = i.item_id
                   WHERE i.family_id=?""",
                (dev["family_id"],),
            ).fetchall()
            return {
                "items": [
                    {
                        "item_id": r["item_id"],
                        "name": r["name"] or r["item_id"],
                        "icon_key": r["icon_key"],
                        "quantity": r["quantity"],
                    }
                    for r in rows
                ]
            }

        @device_router.get("/exchanges", summary="我的兑换")
        def device_exchanges(db: sqlite3.Connection = Depends(_gdb), authorization: str = Header(...)):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            rows = db.execute(
                """SELECT * FROM exchange_request
                   WHERE device_token_hash=? ORDER BY requested_at DESC LIMIT 100""",
                (dev["token_hash"],),
            ).fetchall()
            return {
                "requests": [
                    {
                        "request_id": r["request_id"],
                        "item_id": r["item_id"],
                        "quantity": r["quantity"],
                        "price_total": r["price_total"],
                        "status": r["status"],
                        "requested_at": r["requested_at"],
                        "reviewed_at": r["reviewed_at"],
                        "note": r["note"],
                        "animation_key": r["animation_key"],
                    }
                    for r in rows
                ]
            }

        self._routers = [parent_router, device_router]

    def _row_to_item_dict(self, r) -> dict:
        keys = ["item_id", "family_id", "name", "description", "icon_key",
                "animation_key", "price_currency", "price_amount", "stock",
                "requires_approval", "display_order", "tags", "active", "created_at"]
        d = dict(zip(keys, r))
        d["tags"] = json.loads(d["tags"] or "[]")
        d["requires_approval"] = bool(d["requires_approval"])
        d["active"] = bool(d["active"])
        return d


def create_reward_module(
    get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision,
):
    return RewardModule(get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_revision)