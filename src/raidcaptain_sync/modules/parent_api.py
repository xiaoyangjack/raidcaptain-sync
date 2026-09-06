"""
Parent 端专用 API 集中路由器
Phase 6 - 命名规范 / 空间预估 / 余额查询

端点：
  GET  /api/parent/balance          → 锐察功绩 + 纲纪指数余额 + 近期流水
  GET  /api/parent/transactions     → 流水记录（按 currency 筛选）
  POST /api/parent/balance/adjust  → 手动增减余额（家长调分）
  GET  /api/admin/space-usage      → 各表数据量 + 磁盘占用
  POST /api/admin/seed-rewards     → 播撒军需处 4 大分类默认数据
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from raidcaptain_sync.deps import auth_parent, get_db
from raidcaptain_sync.services.economy import CurrencyKind, EconomyService
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.config import settings


# ── Schemas ────────────────────────────────────────────────────────

class BalanceResponse(BaseModel):
    merit: int
    discipline: int
    last_transactions: list[dict]


class TransactionEntry(BaseModel):
    tx_id: int
    currency: str
    amount: int
    balance_after: int
    reason: str
    ref_id: str
    created_at: int


class TransactionsResponse(BaseModel):
    transactions: list[dict]
    total: int


class AdjustBalanceRequest(BaseModel):
    currency: str  # "merit" | "discipline"
    amount: int
    reason: str = "manual_adjust"
    note: str = ""


class AdjustBalanceResponse(BaseModel):
    ok: bool
    new_balance: int
    error: str = ""


class SpaceUsageEntry(BaseModel):
    table: str
    row_count: int
    estimated_kb: float


class SpaceUsageResponse(BaseModel):
    db_path: str
    db_size_kb: float
    db_size_mb: float
    tables: list[SpaceUsageEntry]
    total_rows: int


# ── 4 大分类 seed 数据 ──────────────────────────────────────────────

SEED_REWARDS = [
    # game_time
    {"item_id": "game_15min",   "name": "游戏时间 15 分钟",   "description": "解锁游戏设备 15 分钟",           "icon_key": "🎮", "price_currency": "merit", "price_amount": 100, "stock": 999, "display_order": 1, "tags": ["game_time"]},
    {"item_id": "game_30min",   "name": "游戏时间 30 分钟",   "description": "解锁游戏设备 30 分钟",           "icon_key": "🎮", "price_currency": "merit", "price_amount": 180, "stock": 999, "display_order": 2, "tags": ["game_time"]},
    {"item_id": "game_45min",   "name": "游戏时间 45 分钟",   "description": "解锁游戏设备 45 分钟",           "icon_key": "🎮", "price_currency": "merit", "price_amount": 260, "stock": 999, "display_order": 3, "tags": ["game_time"]},
    {"item_id": "game_60min",   "name": "游戏时间 60 分钟",   "description": "解锁游戏设备 60 分钟",           "icon_key": "🎮", "price_currency": "merit", "price_amount": 320, "stock": 999, "display_order": 4, "tags": ["game_time"]},
    # entertainment
    {"item_id": "ent_music",     "name": "听歌特权",           "description": "自主播放音乐 30 分钟",          "icon_key": "🎵", "price_currency": "merit", "price_amount": 80,  "stock": 999, "display_order": 10, "tags": ["entertainment"]},
    {"item_id": "ent_animation", "name": "看动画特权",         "description": "观看动画片 30 分钟",            "icon_key": "📺", "price_currency": "merit", "price_amount": 120, "stock": 999, "display_order": 11, "tags": ["entertainment"]},
    {"item_id": "ent_movie",     "name": "看电影特权",         "description": "观看电影 1 部（≤2 小时）",        "icon_key": "🎬", "price_currency": "merit", "price_amount": 400, "stock": 999, "display_order": 12, "tags": ["entertainment"]},
    {"item_id": "ent_online",    "name": "联机游戏特权",       "description": "与朋友联机游戏 30 分钟",         "icon_key": "🌐", "price_currency": "merit", "price_amount": 200, "stock": 999, "display_order": 13, "tags": ["entertainment"]},
    # physical
    {"item_id": "phys_snack",    "name": "零食兑换券",         "description": "兑换指定零食一份",               "icon_key": "🍪", "price_currency": "merit", "price_amount": 150, "stock": 999, "display_order": 20, "tags": ["physical"]},
    {"item_id": "phys_stationery","name": "文具兑换券",        "description": "兑换文具一份（笔/本/橡皮）",      "icon_key": "📒", "price_currency": "merit", "price_amount": 250, "stock": 999, "display_order": 21, "tags": ["physical"]},
    {"item_id": "phys_book",     "name": "新书兑换券",        "description": "自选图书一本",                   "icon_key": "📚", "price_currency": "merit", "price_amount": 400, "stock": 999, "display_order": 22, "tags": ["physical"]},
    {"item_id": "phys_model",    "name": "模型兑换券",        "description": "乐高/拼图模型一个",              "icon_key": "🏎️", "price_currency": "merit", "price_amount": 800, "stock": 999, "display_order": 23, "tags": ["physical"]},
    {"item_id": "phys_bigtoy",   "name": "大玩具兑换券",      "description": "价值 200 元以内玩具一件",         "icon_key": "🎁", "price_currency": "merit", "price_amount": 2000, "stock": 999, "display_order": 24, "tags": ["physical"]},
    # privilege
    {"item_id": "priv_late",    "name": "晚睡特权（1次）",   "description": "当天晚睡 30 分钟",             "icon_key": "⭐", "price_currency": "merit", "price_amount": 100, "stock": 999, "display_order": 30, "tags": ["privilege"]},
    {"item_id": "priv_outing",  "name": "外出游玩特权（1次）","description": "周末外出游玩半天",               "icon_key": "⭐", "price_currency": "merit", "price_amount": 500, "stock": 999, "display_order": 31, "tags": ["privilege"]},
    {"item_id": "priv_treat",   "name": "请客特权（1次）",   "description": "外出就餐点菜一次",               "icon_key": "⭐", "price_currency": "merit", "price_amount": 600, "stock": 999, "display_order": 32, "tags": ["privilege"]},
]

SEED_RANKS = [
    {"rank_id": "private",        "name": "列兵",      "tier": 0,  "required_currency": "discipline", "required_amount": 0,    "display_order": 0},
    {"rank_id": "pvt_first",     "name": "上等兵",    "tier": 1,  "required_currency": "discipline", "required_amount": 50,   "display_order": 1},
    {"rank_id": "corporal",      "name": "下士",      "tier": 2,  "required_currency": "discipline", "required_amount": 150,  "display_order": 2},
    {"rank_id": "sergeant",      "name": "中士",      "tier": 3,  "required_currency": "discipline", "required_amount": 300,  "display_order": 3},
    {"rank_id": "sgt_first",     "name": "上士",      "tier": 4,  "required_currency": "discipline", "required_amount": 500,  "display_order": 4},
    {"rank_id": "sgt_third",     "name": "三级军士长","tier": 5,  "required_currency": "discipline", "required_amount": 750,  "display_order": 5},
    {"rank_id": "sgt_second",    "name": "二级军士长", "tier": 6,  "required_currency": "discipline", "required_amount": 1000, "display_order": 6},
    {"rank_id": "sgt_first_cls", "name": "一级军士长", "tier": 7,  "required_currency": "discipline", "required_amount": 1500, "display_order": 7},
    {"rank_id": "specialist",    "name": "特级",      "tier": 8,  "required_currency": "discipline", "required_amount": 2200, "display_order": 8},
    {"rank_id": "second_lieut",  "name": "少尉",      "tier": 9,  "required_currency": "discipline", "required_amount": 3000, "display_order": 9},
    {"rank_id": "lieutenant",    "name": "中尉",      "tier": 10, "required_currency": "discipline", "required_amount": 4200, "display_order": 10},
    {"rank_id": "captain",       "name": "上尉",      "tier": 11, "required_currency": "discipline", "required_amount": 6000, "display_order": 11},
    {"rank_id": "major",         "name": "少校",      "tier": 12, "required_currency": "discipline", "required_amount": 8500, "display_order": 12},
    {"rank_id": "lieut_col",     "name": "中校",      "tier": 13, "required_currency": "discipline", "required_amount": 12000,"display_order": 13},
    {"rank_id": "colonel",       "name": "上校",      "tier": 14, "required_currency": "discipline", "required_amount": 17000,"display_order": 14},
    {"rank_id": "senior_col",    "name": "大校",      "tier": 15, "required_currency": "discipline", "required_amount": 23000,"display_order": 15},
    {"rank_id": "major_gen",     "name": "少将",      "tier": 16, "required_currency": "discipline", "required_amount": 30000,"display_order": 16},
    {"rank_id": "lieut_gen",     "name": "中将",      "tier": 17, "required_currency": "discipline", "required_amount": 40000,"display_order": 17},
    {"rank_id": "general",        "name": "上将",      "tier": 18, "required_currency": "discipline", "required_amount": 55000,"display_order": 18},
]


# ── Router ────────────────────────────────────────────────────────

def create_parent_api_module(
    get_db, auth_parent, ws_push, parent_sockets, bump_revision,
) -> BaseModule:
    """Parent 端专用 API 模块：余额 / 流水 / 空间 / 种子数据."""

    parent_router = APIRouter(prefix="/api", tags=["parent_api"])
    admin_router = APIRouter(prefix="/api/admin", tags=["admin"])

    # ── 家长端：余额查询 ───────────────────────────────────────────

    @parent_router.get("/parent/balance", summary="查询锐察功绩 + 纲纪指数余额")
    def get_balance(db=Depends(get_db), authorization: str = Header(...)):
        fam = auth_parent(db, authorization)
        eco = EconomyService(db)

        merit = eco.get_balance(fam["id"], CurrencyKind.MERIT)
        discipline = eco.get_balance(fam["id"], CurrencyKind.DISCIPLINE)

        # 近期 10 条流水
        recent = eco.get_transactions(fam["id"], limit=10)
        tx_list = [
            {
                "tx_id": t.tx_id,
                "currency": t.currency.value,
                "amount": t.amount,
                "balance_after": t.balance_after,
                "reason": t.reason,
                "ref_id": t.ref_id,
                "created_at": t.created_at,
            }
            for t in recent
        ]

        return {
            "merit": merit,
            "discipline": discipline,
            "last_transactions": tx_list,
        }

    @parent_router.get("/parent/transactions", summary="流水记录")
    def get_transactions(
        currency: str = Query("", description="merit / discipline / 空=全部"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db=Depends(get_db),
        authorization: str = Header(...),
    ):
        fam = auth_parent(db, authorization)
        eco = EconomyService(db)

        cur = CurrencyKind(currency) if currency else None
        txs = eco.get_transactions(fam["id"], currency=cur, limit=limit, offset=offset)

        return {
            "transactions": [
                {
                    "tx_id": t.tx_id,
                    "currency": t.currency.value,
                    "amount": t.amount,
                    "balance_after": t.balance_after,
                    "reason": t.reason,
                    "ref_id": t.ref_id,
                    "created_at": t.created_at,
                }
                for t in txs
            ],
            "total": len(txs),
        }

    @parent_router.post("/parent/balance/adjust", summary="手动增减余额（家长调分）")
    def adjust_balance(
        body: AdjustBalanceRequest,
        db=Depends(get_db),
        authorization: str = Header(...),
    ):
        fam = auth_parent(db, authorization)
        eco = EconomyService(db)

        try:
            cur = CurrencyKind(body.currency)
        except ValueError:
            raise HTTPException(400, f"未知货币类型: {body.currency}，有效值: merit, discipline")

        ok, new_balance, err = eco.transfer(
            family_id=fam["id"],
            currency=cur,
            amount=body.amount,
            reason=body.reason or "manual_adjust",
            ref_id=body.note or "",
        )

        return {
            "ok": ok,
            "new_balance": new_balance,
            "error": err,
        }

    # ── Admin：空间占用 ────────────────────────────────────────────

    @admin_router.get("/space-usage", summary="各表数据量 + 磁盘占用")
    def get_space_usage(db=Depends(get_db), authorization: str = Header(...)):
        fam = auth_parent(db, authorization)

        db_path = str(settings.db_path)
        db_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        db_size_kb = db_size_bytes / 1024
        db_size_mb = db_size_kb / 1024

        # 各表行数（仅查当前 family 相关）
        table_counts = {}
        try:
            rows = db.execute("""
                SELECT 'event' as tbl, COUNT(*) FROM event WHERE family_id=?
                UNION ALL SELECT 'task', COUNT(*) FROM task WHERE family_id=?
                UNION ALL SELECT 'task_revision', COUNT(*) FROM task_revision WHERE family_id=?
                UNION ALL SELECT 'patrol_session', COUNT(*) FROM patrol_session WHERE family_id=?
                UNION ALL SELECT 'appeal', COUNT(*) FROM appeal WHERE family_id=?
                UNION ALL SELECT 'evidence_file', COUNT(*) FROM evidence_file WHERE family_id=?
                UNION ALL SELECT 'currency_transaction', COUNT(*) FROM currency_transaction WHERE family_id=?
                UNION ALL SELECT 'currency_balance', COUNT(*) FROM currency_balance WHERE family_id=?
                UNION ALL SELECT 'exchange_request', COUNT(*) FROM exchange_request WHERE family_id=?
                UNION ALL SELECT 'family_inventory', COUNT(*) FROM family_inventory WHERE family_id=?
                UNION ALL SELECT 'rank_def', COUNT(*) FROM rank_def
                UNION ALL SELECT 'family_rank', COUNT(*) FROM family_rank WHERE family_id=?
            """, (fam["id"],)*12).fetchall()
            for tbl, cnt in rows:
                table_counts[tbl] = cnt
        except Exception:
            pass

        tables = [
            {"table": tbl, "row_count": cnt, "estimated_kb": round(cnt * 0.3 / 1024, 2)}
            for tbl, cnt in table_counts.items()
        ]
        tables.sort(key=lambda x: x["row_count"], reverse=True)

        return {
            "db_path": db_path,
            "db_size_kb": round(db_size_kb, 2),
            "db_size_mb": round(db_size_mb, 4),
            "tables": tables,
            "total_rows": sum(t["row_count"] for t in tables),
        }

    @admin_router.post("/seed-rewards", summary="播撒军需处 4 大分类默认数据")
    def seed_rewards(db=Depends(get_db), authorization: str = Header(...)):
        """一次性播撒 4 大分类 + 19 级军衔数据（幂等，不会重复覆盖）."""
        fam = auth_parent(db, authorization)
        now = int(time.time() * 1000)

        seeded_items = 0
        for item in SEED_REWARDS:
            db.execute(
                """INSERT INTO store_item(item_id, family_id, name, description,
                    icon_key, animation_key, price_currency, price_amount, stock,
                    requires_approval, display_order, tags, active, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,?,?,1,?)
                   ON CONFLICT(item_id) DO UPDATE SET
                    name=excluded.name, description=excluded.description,
                    icon_key=excluded.icon_key, price_currency=excluded.price_currency,
                    price_amount=excluded.price_amount, stock=excluded.stock,
                    display_order=excluded.display_order, tags=excluded.tags""",
                (item["item_id"], fam["id"], item["name"], item["description"],
                 item["icon_key"], "", item["price_currency"], item["price_amount"],
                 item["stock"], item["display_order"], json.dumps(item["tags"]), now),
            )
            seeded_items += 1

        seeded_ranks = 0
        for rank in SEED_RANKS:
            db.execute(
                """INSERT INTO rank_def(rank_id, name, description, tier, icon_key,
                    unlock_animation_key, required_currency, required_amount,
                    reward_merit, reward_points, reward_items, display_order, created_at)
                   VALUES (?,?,'',?,'','',?,?,0,0,'[]',?,?)
                   ON CONFLICT(rank_id) DO UPDATE SET
                    name=excluded.name, tier=excluded.tier,
                    required_currency=excluded.required_currency,
                    required_amount=excluded.required_amount,
                    display_order=excluded.display_order""",
                (rank["rank_id"], rank["name"], rank["tier"],
                 rank["required_currency"], rank["required_amount"],
                 rank["display_order"], now),
            )
            seeded_ranks += 1

        db.commit()

        return {
            "ok": True,
            "seeded_items": seeded_items,
            "seeded_ranks": seeded_ranks,
            "categories": ["game_time", "entertainment", "physical", "privilege"],
            "total_ranks": len(SEED_RANKS),
        }

    # ── 注册 ──────────────────────────────────────────────────────

    class ParentAPIModule(BaseModule):
        id = "parent_api"
        display_name = "Parent API"
        version = "1.0.0"
        description = "余额 / 流水 / 空间 / 种子数据"

        def __init__(self):
            super().__init__()
            self._routers = [parent_router, admin_router]

    return ParentAPIModule()
