"""
军衔模块 - RaidCaptain Sync Server v3.2
阶梯图 → 进度累积 → 手动/自动升级 → 奖励发放 + 升级动画触发。

API（家长端）：
  POST /api/parent/ranks       创建/更新军衔定义
  GET  /api/parent/ranks       列出所有军衔
  POST /api/parent/ranks/upgrade 手动升级
  GET  /api/parent/ranks/progress 当前进度
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from raidcaptain_sync.services.economy import CurrencyKind, EconomyService
from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule


# ── Pydantic schemas ──────────────────────────────────────────────


class RankCreate(BaseModel):
    rank_id: str
    name: str
    description: str = ""
    tier: int = Field(ge=0)
    icon_key: str = ""
    unlock_animation_key: str = Field("")
    required_currency: str = "merit"
    required_amount: int = Field(ge=0)
    reward_merit: int = 0
    reward_points: int = 0
    reward_items: list = Field(default_factory=list)
    display_order: int = 0


class RankUpgradeRequest(BaseModel):
    rank_id: str


# ── Module ──────────────────────────────────────────────────────


class RankModule(BaseModule):
    """军衔系统模块。"""

    id = "ranks"
    display_name = "军衔系统"
    version = "1.0.0"
    description = "阶梯图 + 进度累积 + 升级动画 + 专属奖励"

    def __init__(self, get_db, ws_push, parent_sockets, bump_revision):
        self._get_db = get_db
        self._ws = ws_push
        self._sockets = parent_sockets
        self._bump_rev_fn = bump_revision
        self._routers: list = []
        self._build_routers()

    async def on_register(self, app) -> None:
        # Phase 6：启动时主动建表
        try:
            with self._get_db() as db:
                self._ensure_schema(db)
        except Exception:
            pass

    def _open_db(self):
        """Phase 6：module_registry.init_all 自动建表所需的 helper."""
        return self._get_db()

    def _ensure_schema(self, db) -> None:
        """建表（rank_def + family_rank）。"""
        db.execute("""
            CREATE TABLE IF NOT EXISTS rank_def(
                rank_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tier INTEGER NOT NULL DEFAULT 0,
                icon_key TEXT NOT NULL DEFAULT '',
                unlock_animation_key TEXT NOT NULL DEFAULT '',
                required_currency TEXT NOT NULL DEFAULT 'merit',
                required_amount INTEGER NOT NULL DEFAULT 0,
                reward_merit INTEGER NOT NULL DEFAULT 0,
                reward_points INTEGER NOT NULL DEFAULT 0,
                reward_items TEXT NOT NULL DEFAULT '[]',
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS family_rank(
                family_id TEXT PRIMARY KEY,
                rank_id TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                upgraded_at INTEGER
            )
        """)

    def _get_all_ranks(self, db):
        rows = db.execute(
            "SELECT rank_id, name, description, tier, icon_key, unlock_animation_key,"
            " required_currency, required_amount, reward_merit, reward_points,"
            " reward_items, display_order"
            " FROM rank_def ORDER BY tier, display_order"
        ).fetchall()
        return [
            {
                "rank_id": r[0], "name": r[1], "description": r[2],
                "tier": r[3], "icon_key": r[4], "unlock_animation_key": r[5],
                "required_currency": r[6], "required_amount": r[7],
                "reward_merit": r[8], "reward_points": r[9],
                "reward_items": json.loads(r[10] or "[]"),
                "display_order": r[11],
            } for r in rows
        ]

    def _get_current_rank(self, db, family_id: str):
        row = db.execute(
            "SELECT rank_id, progress, total_earned FROM family_rank WHERE family_id=?",
            (family_id,),
        ).fetchone()
        if not row:
            return None
        return {"rank_id": row[0], "progress": row[1], "total_earned": row[2]}

    def _set_rank(self, db, family_id, rank_id, progress, total):
        now = int(time.time() * 1000)
        db.execute(
            """INSERT INTO family_rank(family_id, rank_id, progress, total_earned, upgraded_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(family_id) DO UPDATE SET
                rank_id=excluded.rank_id, progress=excluded.progress,
                total_earned=excluded.total_earned, upgraded_at=excluded.upgraded_at""",
            (family_id, rank_id, progress, total, now),
        )

    def _build_routers(self) -> None:
        from raidcaptain_sync.deps import auth_parent as _ap, get_db as _gdb
        router = APIRouter(prefix="/api", tags=["ranks"])

        @router.post("/parent/ranks", summary="创建/更新军衔")
        def create_rank(
            body: RankCreate,
            db=Depends(_gdb),
            authorization: str | None = Header(None),
        ):
            fam = _ap(db, authorization)
            self._ensure_schema(db)
            now = int(time.time() * 1000)
            db.execute(
                """INSERT INTO rank_def(rank_id, name, description, tier,
                    icon_key, unlock_animation_key, required_currency, required_amount,
                    reward_merit, reward_points, reward_items, display_order, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(rank_id) DO UPDATE SET
                    name=excluded.name, description=excluded.description,
                    tier=excluded.tier, icon_key=excluded.icon_key,
                    unlock_animation_key=excluded.unlock_animation_key,
                    required_currency=excluded.required_currency,
                    required_amount=excluded.required_amount,
                    reward_merit=excluded.reward_merit,
                    reward_points=excluded.reward_points,
                    reward_items=excluded.reward_items,
                    display_order=excluded.display_order""",
                (body.rank_id, body.name, body.description, body.tier,
                 body.icon_key, body.unlock_animation_key,
                 body.required_currency, body.required_amount,
                 body.reward_merit, body.reward_points,
                 json.dumps(body.reward_items), body.display_order, now),
            )
            db.commit()
            self._bump_rev_fn(db, fam["id"])
            return {"rank_id": body.rank_id}

        @router.get("/parent/ranks", summary="列出所有军衔")
        def list_ranks(
            db=Depends(_gdb),
            authorization: str | None = Header(None),
        ):
            fam = _ap(db, authorization)
            self._ensure_schema(db)
            ranks = self._get_all_ranks(db)
            current = self._get_current_rank(db, fam["id"])
            cur_tier = 0
            if current:
                for r in ranks:
                    if r["rank_id"] == current["rank_id"]:
                        cur_tier = r["tier"]
                        break
            return {
                "ranks": [
                    {
                        **r,
                        "current": bool(current and current["rank_id"] == r["rank_id"]),
                        "unlocked": r["tier"] <= cur_tier,
                    }
                    for r in ranks
                ],
                "current_rank": current,
            }

        @router.post("/parent/ranks/upgrade", summary="手动升级军衔")
        def manual_upgrade(
            body: RankUpgradeRequest,
            db=Depends(_gdb),
            authorization: str | None = Header(None),
        ):
            fam = _ap(db, authorization)
            self._ensure_schema(db)

            row = db.execute(
                "SELECT rank_id, name, tier, unlock_animation_key, required_currency,"
                " required_amount, reward_merit, reward_items FROM rank_def WHERE rank_id=?",
                (body.rank_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "军衔不存在")
            target = {
                "rank_id": row[0], "name": row[1], "tier": row[2],
                "animation_key": row[3], "required_currency": row[4],
                "required_amount": row[5], "reward_merit": row[6],
                "reward_points": 0, "reward_items": json.loads(row[7] or "[]"),
            }

            current = self._get_current_rank(db, fam["id"])
            if current and current["rank_id"] == body.rank_id:
                return {"upgraded": False, "reason": "已是该军衔"}

            # Phase 6：校验所需货币（军衔晋升只能用 discipline 纲纪指数）
            required_currency = "discipline"
            cur_balance = 0
            eco = EconomyService(db)
            cur_balance = eco.get_balance(fam["id"], CurrencyKind.DISCIPLINE)
            target_amount = target["required_amount"]
            if cur_balance < target_amount:
                return {"upgraded": False,
                        "reason": f"纲纪指数不足（需要 {target_amount} discipline，当前 {cur_balance}）"}

            # 扣款（纲纪指数）
            ok, _, err = eco.transfer(
                fam["id"], CurrencyKind.DISCIPLINE,
                -target_amount, "rank_upgrade", body.rank_id,
            )
            if not ok:
                raise HTTPException(400, err)

            # 升级
            self._set_rank(db, fam["id"], body.rank_id, 0,
                          (current["total_earned"] if current else 0))
            db.commit()
            self._bump_rev_fn(db, fam["id"])

            # 奖励（merit）
            rewards = []
            if target["reward_merit"]:
                rewards.append((CurrencyKind.MERIT, target["reward_merit"]))
            if rewards:
                eco.batch_transfer(fam["id"], rewards, "rank_reward", body.rank_id)

            # 事件 + WS 推送
            event_bus.publish(EventContext(
                family_id=fam["id"], kind=EventKind.RANK_PROMOTED,
                data={
                    "from_rank_id": current["rank_id"] if current else "",
                    "to_rank_id": body.rank_id,
                    "animation_key": target["animation_key"],
                },
            ))
            self._ws(fam["id"], self._sockets, {
                "type": "rank_promoted",
                "from_rank_id": current["rank_id"] if current else "",
                "to_rank_id": body.rank_id,
                "animation_key": target["animation_key"],
            })

            return {"upgraded": True, "new_rank_id": body.rank_id,
                    "new_rank_name": target["name"]}

        @router.get("/parent/ranks/progress", summary="军衔进度")
        def get_progress(
            db=Depends(_gdb),
            authorization: str | None = Header(None),
        ):
            fam = _ap(db, authorization)
            self._ensure_schema(db)
            current = self._get_current_rank(db, fam["id"])
            ranks = self._get_all_ranks(db)
            if not ranks:
                return {"current_rank_id": "", "current_rank_name": "无",
                        "current_tier": -1, "next_rank_id": None,
                        "next_rank_name": None, "next_required": 0,
                        "current_progress": 0, "progress_percent": 0.0}

            if not current:
                first = ranks[0]
                return {"current_rank_id": "", "current_rank_name": "无",
                        "current_tier": -1, "next_rank_id": first["rank_id"],
                        "next_rank_name": first["name"],
                        "next_required": first["required_amount"],
                        "current_progress": 0, "progress_percent": 0.0}

            cur_rank = next((r for r in ranks if r["rank_id"] == current["rank_id"]), None)
            cur_tier = cur_rank["tier"] if cur_rank else -1
            next_ranks = [r for r in ranks if r["tier"] == cur_tier + 1]
            next_rank = next_ranks[0] if next_ranks else None

            prog = current["total_earned"]
            target_amount = next_rank["required_amount"] if next_rank else 0
            pct = min(100.0, prog / target_amount * 100) if target_amount else 100.0
            return {
                "current_rank_id": current["rank_id"],
                "current_rank_name": cur_rank["name"] if cur_rank else "未知",
                "current_tier": cur_tier,
                "next_rank_id": next_rank["rank_id"] if next_rank else None,
                "next_rank_name": next_rank["name"] if next_rank else None,
                "next_required": target_amount,
                "current_progress": prog,
                "progress_percent": pct,
            }

        self._routers = [router]


def create_rank_module(get_db, ws_push, parent_sockets, bump_revision):
    return RankModule(get_db, ws_push, parent_sockets, bump_revision)
