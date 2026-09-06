"""
事件总线 - RaidCaptain Sync Server v3.1
所有事件经过统一分发：类型安全、自动持久化、模块订阅。

设计灵感来自游戏引擎的 Event Dispatcher:
- EventKind 枚举替代自由字符串
- EventContext 携带完整上下文
- 处理器可订阅感兴趣的事件
- 自动持久化 + WS 推送
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

import sqlite3

logger = logging.getLogger(__name__)


class EventKind(str, Enum):
    """所有事件类型的强类型枚举。
    新增事件：只需在此处加一行，所有订阅者自动收到。"""

    # ── 任务模块 ────────────────────────────────────────────────
    TASK_COMPLETION = "task_completion"
    TASK_OVERDUE = "task_overdue"
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_DELETED = "task_deleted"

    # ── 巡逻 / 学习时段 ────────────────────────────────────────
    PATROL_SESSION = "patrol_session"
    PATROL_OUTCOME = "patrol_outcome"
    MISSION_RESULT = "mission_result"

    # ── 申诉 ───────────────────────────────────────────────────
    APPEAL_SUBMITTED = "appeal_submitted"
    APPEAL_REVIEWED = "appeal_reviewed"

    # ── 故事线 Bundle ─────────────────────────────────────────
    BUNDLE_PUBLISHED = "bundle_published"
    BUNDLE_DOWNLOADED = "bundle_downloaded"
    EPISODE_STARTED = "episode_started"
    EPISODE_COMPLETED = "episode_completed"
    CHAPTER_UNLOCKED = "chapter_unlocked"
    STORYLINE_COMPLETED = "storyline_completed"

    # ── 军衔 ───────────────────────────────────────────────────
    RANK_PROMOTED = "rank_promoted"          # 军衔升级（可触发升级动画）
    RANK_PROGRESS_UPDATED = "rank_progress_updated"

    # ── 成就 ───────────────────────────────────────────────────
    ACHIEVEMENT_UNLOCKED = "achievement_unlocked"
    ACHIEVEMENT_CLAIMED = "achievement_claimed"
    MILESTONE_REACHED = "milestone_reached"

    # ── 奖励商店 / 兑换 ───────────────────────────────────────
    EXCHANGE_REQUESTED = "exchange_requested"  # 孩子发起兑换（家长WS通知）
    EXCHANGE_COMPLETED = "exchange_completed"  # 兑换通过（播放动画）
    EXCHANGE_REJECTED = "exchange_rejected"    # 兑换驳回

    # ── 货币 / 经济 ────────────────────────────────────────────
    CURRENCY_CHANGED = "currency_changed"    # 余额变动
    CURRENCY_INSUFFICIENT = "currency_insufficient"  # 余额不足提示

    # ── 动画 ───────────────────────────────────────────────────
    ANIMATION_PLAYED = "animation_played"    # 动画播放记录（用于统计/解锁）

    # ── 设备生命周期 ──────────────────────────────────────────
    DEVICE_PAIRED = "device_paired"
    DEVICE_ONLINE = "device_online"
    DEVICE_OFFLINE = "device_offline"

    # ── 系统 ───────────────────────────────────────────────────
    ANNOUNCEMENT_NEW = "announcement_new"
    MILESTONE_ANY = "milestone_any"          # 任意里程碑（跨模块联动钩子）


@dataclass
class EventContext:
    """事件发布时的完整上下文。"""
    family_id: str
    kind: EventKind
    device_name: str = ""
    device_token_hash: str = ""
    data: dict = field(default_factory=dict)
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    db: Optional[sqlite3.Connection] = None
    persist: bool = True


EventHandler = Callable[[EventContext], Awaitable[None] | None]


class EventBus:
    """事件总线 - 替代散落的 event.kind 字符串。

    用法:
        # 注册处理器
        event_bus.subscribe(EventKind.TASK_COMPLETION, achievement_handler)

        # 发布事件 (自动持久化 + WS 推送)
        await event_bus.publish(EventContext(family_id=..., kind=TASK_COMPLETION, ...))
    """

    def __init__(self):
        self._handlers: dict[EventKind, list[EventHandler]] = defaultdict(list)
        self._ws_pusher: Optional[Callable] = None

    def set_ws_pusher(self, pusher: Callable) -> None:
        self._ws_pusher = pusher

    def subscribe(self, kind: EventKind, handler: EventHandler) -> None:
        self._handlers[kind].append(handler)
        logger.debug("Subscribed %s to %s", handler.__qualname__, kind.value)

    def unsubscribe(self, kind: EventKind, handler: EventHandler) -> None:
        if handler in self._handlers.get(kind, []):
            self._handlers[kind].remove(handler)

    async def publish(self, ctx: EventContext) -> int:
        """发布事件：持久化 -> 触发处理器 -> WS 推送 -> 返回 event_id。"""
        if ctx.persist and ctx.db is not None:
            cur = ctx.db.execute(
                "INSERT INTO event(family_id, device_name, kind, payload, created_at) "
                "VALUES(?,?,?,?,?)",
                (ctx.family_id, ctx.device_name, ctx.kind.value,
                 json.dumps(ctx.data, ensure_ascii=False), ctx.timestamp),
            )
            ev_id = cur.lastrowid
            ctx.data.setdefault("_event_id", ev_id)
        else:
            ev_id = 0

        for handler in list(self._handlers.get(ctx.kind, [])):
            try:
                result = handler(ctx)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.exception(
                    "Event handler %s failed for %s: %s",
                    handler.__qualname__, ctx.kind.value, e,
                )

        if self._ws_pusher is not None:
            try:
                await self._ws_pusher(
                    ctx.family_id, ctx.kind.value,
                    ctx.device_name, ctx.data, ctx.timestamp,
                )
            except Exception as e:
                logger.warning("WS push failed: %s", e)

        return ev_id

    def list_subscriptions(self) -> dict:
        return {k.value: [h.__qualname__ for h in v] for k, v in self._handlers.items()}


# 全局单例
event_bus = EventBus()


async def _default_ws_pusher(family_id: str, kind: str, device_name: str,
                              data: dict, ts: int) -> None:
    """默认 WS 推送：动态导入避免循环引用。"""
    from raidcaptain_sync.deps import parent_sockets, ws_push
    payload = {
        "type": "event",
        "event": {"kind": kind, "data": data, "device_name": device_name, "created_at": ts},
    }
    await ws_push(family_id, parent_sockets, payload)


event_bus.set_ws_pusher(_default_ws_pusher)