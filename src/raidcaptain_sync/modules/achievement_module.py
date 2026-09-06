"""
成就模块 - RaidCaptain Sync Server v3.1
事件驱动的成就系统：事件总线触发 → 条件匹配 → 自动解锁 → 奖励发放。

触发器类型:
  - task_streak: 连续N天完成任务
  - total_completed: 累计完成N次
  - bundle_complete: 完成指定 Bundle
  - custom: 自定义条件

奖励类型:
  - merit: 积分奖励
  - points: 点数奖励
  - items: 道具列表 (JSON)
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from raidcaptain_sync.services.event_bus import EventKind, EventContext, event_bus
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


# ── Pydantic schemas ──────────────────────────────────────────────


class TriggerConfig(BaseModel):
    type: str = Field(..., description="task_streak | total_completed | bundle_complete | custom")
    params: dict = Field(default_factory=dict)
    once: bool = Field(True, description="是否只能触发一次")


class AchievementCreate(BaseModel):
    achievement_id: str
    title: str
    description: str = ""
    icon_key: str = ""
    category: str = "general"  # general | task | storyline | social | hidden
    rarity: str = "common"  # common | rare | epic | legendary
    trigger: TriggerConfig
    reward_merit: int = 0
    reward_points: int = 0
    reward_items: list = Field(default_factory=list)
    display_order: int = 0


# ── 触发器引擎 ────────────────────────────────────────────────────


class TriggerEngine:
    """成就触发器引擎：扫描事件，判断是否解锁。"""

    def __init__(self, db, family_id: str):
        self.db = db
        self.family_id = family_id

    def evaluate(self, achievement_row: dict, ctx: EventContext) -> tuple[bool, int]:
        """评估成就是否应解锁。
        返回 (should_unlock, progress)。
        progress = 当前进度（用于显示）。
        """
        import json as _json
        trigger: dict = {}
        try:
            trigger = _json.loads(achievement_row["trigger_config"] or "{}")
        except Exception:
            pass

        trigger_type = trigger.get("type", "custom")
        params = trigger.get("params", {})
        target = params.get("count", params.get("days", params.get("total", 1)))

        if trigger_type == "task_streak":
            return self._eval_task_streak(params, target, ctx)
        elif trigger_type == "total_completed":
            return self._eval_total_completed(params, target, ctx)
        elif trigger_type == "bundle_complete":
            return self._eval_bundle_complete(params, target, ctx)
        else:
            return False, 0

    def _eval_task_streak(self, params: dict, target: int, ctx: EventContext) -> tuple[bool, int]:
        """连续 N 天完成任务。"""
        task_id = params.get("task_id")
        rows = self.db.execute(
            """SELECT COUNT(DISTINCT date(created_at/1000,'unixepoch','localtime')) as days,
                      COUNT(*) as total
               FROM event
               WHERE family_id=? AND kind='task_completion'
               AND payload LIKE ? AND created_at > ?
               ORDER BY created_at DESC""",
            (self.family_id,
             f'%"{task_id}"%' if task_id else '%%',
             int((time.time() - 90 * 86400) * 1000)),
        ).fetchall()
        row = rows[0] if rows else {"days": 0, "total": 0}
        days = row["days"]
        return (days >= target, days)

    def _eval_total_completed(self, params: dict, target: int, ctx: EventContext) -> tuple[bool, int]:
        """累计完成 N 次。"""
        task_id = params.get("task_id")
        if task_id:
            count = self.db.execute(
                """SELECT COUNT(*) FROM event
                   WHERE family_id=? AND kind='task_completion'
                   AND payload LIKE ?""",
                (self.family_id, f'%"{task_id}"%'),
            ).fetchone()["COUNT(*)"]
        else:
            count = self.db.execute(
                "SELECT COUNT(*) FROM event WHERE family_id=? AND kind='task_completion'",
                (self.family_id,),
            ).fetchone()["COUNT(*)"]
        return (count >= target, count)

    def _eval_bundle_complete(self, params: dict, target: int, ctx: EventContext) -> tuple[bool, int]:
        """完成指定 Bundle 全部章节。"""
        bundle_id = params.get("bundle_id")
        count = self.db.execute(
            """SELECT COUNT(DISTINCT chapter_id) as chapters
               FROM storyline_progress
               WHERE family_id=? AND bundle_id=?
               AND completed_episodes IS NOT NULL""",
            (self.family_id, bundle_id),
        ).fetchone()["chapters"]
        return (count >= target, count)


# ── Module ────────────────────────────────────────────────────────


class AchievementModule(BaseModule):
    id = StandardModules.ACHIEVEMENTS
    display_name = "成就系统"
    version = "1.0.0"
    description = "成就定义/解锁/奖励"

    def __init__(self, get_db, bump_revision, ws_push, parent_sockets):
        self._get_db = get_db
        self._bump_revision = bump_revision
        self._ws_push = ws_push
        self._parent_sockets = parent_sockets
        self._routers: list = []
        self._build_routers()

    def _build_routers(self) -> None:
        r = APIRouter(prefix="/api", tags=["achievements"])

        # ── 家长端 ────────────────────────────────────────────────

        @r.get("/parent/achievements")
        def list_achievements(
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            rows = db.execute(
                "SELECT * FROM achievement WHERE active=1 ORDER BY display_order",
            ).fetchall()
            return {"achievements": [self._achievement_json(r) for r in rows]}

        @r.get("/parent/achievements/unlocked")
        def list_unlocked(
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            rows = db.execute(
                """SELECT fa.*, a.title, a.description, a.icon_key, a.category,
                          a.rarity, a.reward_merit, a.reward_points, a.reward_items
                   FROM family_achievement fa
                   JOIN achievement a ON a.achievement_id = fa.achievement_id
                   WHERE fa.family_id=?
                   ORDER BY fa.unlocked_at DESC""",
                (fam["id"],),
            ).fetchall()
            return {
                "achievements": [
                    {
                        **self._achievement_json({"achievement_id": r["achievement_id"],
                                                  "title": r["title"],
                                                  "description": r["description"],
                                                  "icon_key": r["icon_key"],
                                                  "category": r["category"],
                                                  "rarity": r["rarity"],
                                                  "trigger_config": "{}",
                                                  "reward_merit": r["reward_merit"],
                                                  "reward_points": r["reward_points"],
                                                  "reward_items": r["reward_items"],
                                                  "display_order": 0}),
                        "progress": r["progress"],
                        "target": r["target"],
                        "unlocked_at": r["unlocked_at"],
                        "claimed": bool(r["claimed"]),
                        "claimed_at": r["claimed_at"],
                    }
                    for r in rows
                ]
            }

        @r.post("/parent/achievements")
        def create_achievement(
            body: AchievementCreate,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            now = int(time.time() * 1000)
            trigger_raw = json.dumps(body.trigger.model_dump(), ensure_ascii=False)
            db.execute(
                """INSERT OR REPLACE INTO achievement(
                    achievement_id, module_id, title, description, icon_key,
                    category, rarity, trigger_config, reward_merit, reward_points,
                    reward_items, display_order, active, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (body.achievement_id, StandardModules.ACHIEVEMENTS, body.title,
                 body.description, body.icon_key, body.category, body.rarity,
                 trigger_raw, body.reward_merit, body.reward_points,
                 json.dumps(body.reward_items), body.display_order, now),
            )
            self._bump_revision(db, fam["id"])
            return {"ok": True, "achievement_id": body.achievement_id}

        @r.post("/parent/achievements/{aid}/claim")
        async def claim_reward(
            aid: str,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_parent
            fam = auth_parent(db, authorization)
            row = db.execute(
                "SELECT * FROM family_achievement WHERE family_id=? AND achievement_id=?",
                (fam["id"], aid),
            ).fetchone()
            if not row:
                raise HTTPException(404, "achievement not unlocked")
            if row["claimed"]:
                raise HTTPException(400, "already claimed")
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE family_achievement SET claimed=1, claimed_at=? "
                "WHERE family_id=? AND achievement_id=?",
                (now, fam["id"], aid),
            )
            await event_bus.publish(EventContext(
                family_id=fam["id"], kind=EventKind.ACHIEVEMENT_CLAIMED,
                data={"achievement_id": aid},
                db=db,
            ))
            return {"ok": True, "claimed_at": now}

        # ── 设备端进度查询 ────────────────────────────────────────

        @r.get("/achievements/progress")
        def device_progress(
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            from raidcaptain_sync.deps import auth_device
            dev = auth_device(db, authorization)
            rows = db.execute(
                """SELECT fa.*, a.title, a.category, a.trigger_config
                   FROM family_achievement fa
                   JOIN achievement a ON a.achievement_id = fa.achievement_id
                   WHERE fa.family_id=?""",
                (dev["family_id"],),
            ).fetchall()
            return {
                "progress": [
                    {
                        "achievement_id": r["achievement_id"],
                        "title": r["title"],
                        "progress": r["progress"],
                        "target": r["target"],
                        "category": r["category"],
                        "unlocked": r["unlocked_at"] is not None,
                    }
                    for r in rows
                ]
            }

        self._routers = [r]

    async def on_register(self, app) -> None:
        """注册成就事件处理器：每当有相关事件时扫描所有成就。"""
        event_bus.subscribe(EventKind.TASK_COMPLETION, self._on_task_event)
        event_bus.subscribe(EventKind.EPISODE_COMPLETED, self._on_storyline_event)

    async def _on_task_event(self, ctx: EventContext) -> None:
        self._check_achievements(ctx, EventKind.TASK_COMPLETION)

    async def _on_storyline_event(self, ctx: EventContext) -> None:
        self._check_achievements(ctx, EventKind.EPISODE_COMPLETED)

    def _check_achievements(self, ctx: EventContext, trigger_event: EventKind) -> None:
        """扫描所有成就，判断是否解锁。"""
        if ctx.db is None:
            return
        try:
            db = ctx.db
            engine = TriggerEngine(db, ctx.family_id)

            rows = db.execute(
                """SELECT * FROM achievement
                   WHERE active=1
                   AND trigger_config LIKE ?
                   AND achievement_id NOT IN (
                       SELECT achievement_id FROM family_achievement
                       WHERE family_id=? AND unlocked_at IS NOT NULL
                   )""",
                (f"%{trigger_event.value}%", ctx.family_id),
            ).fetchall()

            for row in rows:
                should_unlock, progress = engine.evaluate(row, ctx)
                if should_unlock:
                    self._unlock_achievement(db, ctx, row, progress)
        except Exception as e:
            import logging
            logging.exception("Achievement check failed: %s", e)

    def _unlock_achievement(self, db, ctx: EventContext, row: dict, progress: int) -> None:
        import logging
        now = int(time.time() * 1000)
        try:
            trigger: dict = json.loads(row["trigger_config"] or "{}")
        except Exception:
            trigger = {}
        target = trigger.get("params", {}).get("count", 1)

        db.execute(
            """INSERT INTO family_achievement(
                family_id, achievement_id, progress, target, unlocked_at, claimed)
               VALUES(?,?,?,?,?,0)
               ON CONFLICT(family_id, achievement_id) DO UPDATE SET
                progress=excluded.progress, unlocked_at=excluded.unlocked_at""",
            (ctx.family_id, row["achievement_id"], progress, target, now),
        )
        logging.info("Achievement unlocked: %s for family %s", row["achievement_id"], ctx.family_id)

        # 事件
        event_bus.publish(EventContext(
            family_id=ctx.family_id, kind=EventKind.ACHIEVEMENT_UNLOCKED,
            device_name=ctx.device_name,
            data={
                "achievement_id": row["achievement_id"],
                "title": row["title"],
                "rarity": row["rarity"],
                "reward_merit": row["reward_merit"],
                "reward_points": row["reward_points"],
            },
            db=db,
        ))

        # WS 推送
        self._ws_push(ctx.family_id, self._parent_sockets, {
            "type": "achievement_unlocked",
            "achievement_id": row["achievement_id"],
            "title": row["title"],
            "rarity": row["rarity"],
        })

    def _achievement_json(self, row: dict) -> dict:
        return {
            "achievement_id": row["achievement_id"],
            "title": row["title"],
            "description": row.get("description", ""),
            "icon_key": row.get("icon_key", ""),
            "category": row.get("category", "general"),
            "rarity": row.get("rarity", "common"),
            "trigger_config": row.get("trigger_config", "{}"),
            "reward_merit": row.get("reward_merit", 0),
            "reward_points": row.get("reward_points", 0),
            "reward_items": json.loads(row.get("reward_items", "[]")),
            "display_order": row.get("display_order", 0),
        }


def create_achievement_module(get_db, bump_revision, ws_push,
                              parent_sockets) -> AchievementModule:
    return AchievementModule(get_db, bump_revision, ws_push, parent_sockets)