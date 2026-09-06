"""
跨模块经济系统 - RaidCaptain Sync Server v3.2
所有货币/积分的中央账本：余额、流水、事务性操作。

设计：
- 货币类型由 CurrencyKind 强类型枚举定义，新增货币只需加一行
- 余额表 (currency_balance) 按 family_id + currency 类型联合主键
- 流水表 (currency_transaction) 记录每一笔变动
- 事务性操作：余额增减 + 流水记录 原子完成（防止刷币）
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import sqlite3


class CurrencyKind(str, Enum):
    """所有货币/积分类型。新增货币只需在此处加一行。"""
    MERIT = "merit"          # 锐察功绩（任务奖励/商城兑换）
    POINTS = "points"        # 通用积分（向后兼容）
    DISCIPLINE = "discipline"  # 纲纪指数（军衔晋升依据，Phase 6 引入）
    STARS = "stars"          # 星星（成就奖励）
    GEMS = "gems"            # 宝石（充值/大额奖励）
    TOKENS = "tokens"        # 代币（限时活动货币）


@dataclass
class CurrencyConfig:
    """每种货币的元数据（可在 module_info.config 中覆盖）。"""
    name: str
    plural: str
    icon: str          # emoji 或 OSS key
    color: str         # hex color for UI
    allow_negative: bool = False   # 是否允许负余额（如罚款）
    min_balance: int = 0          # 余额下限
    max_balance: int = 999999     # 余额上限


CURRENCY_META: dict[CurrencyKind, CurrencyConfig] = {
    CurrencyKind.MERIT:      CurrencyConfig("锐察功绩", "锐察功绩", "🎖️", "#E8A33D", allow_negative=False, min_balance=0),
    CurrencyKind.POINTS:     CurrencyConfig("点数",   "点数",   "⭐", "#FFC107"),
    CurrencyKind.DISCIPLINE: CurrencyConfig("纲纪指数", "纲纪指数", "🏛️", "#8BAC0F"),  # Phase 6 引入：军衔晋升依据
    CurrencyKind.STARS:      CurrencyConfig("星星",   "星星",   "🌟", "#E91E63"),
    CurrencyKind.GEMS:       CurrencyConfig("宝石",   "宝石",   "💎", "#9C27B0"),
    CurrencyKind.TOKENS:     CurrencyConfig("代币",   "代币",   "🎫", "#FF5722"),
}


@dataclass
class Transaction:
    """单笔货币流水。"""
    tx_id: int
    family_id: str
    currency: CurrencyKind
    amount: int          # 正=收入，负=支出
    balance_after: int
    reason: str          # 触发原因（如 "task_completion", "rank_upgrade", "exchange"）
    created_at: int
    ref_id: str = ""     # 关联 ID（achievement_id / exchange_id / ...）


class EconomyService:
    """经济系统服务：余额查询/增减/流水。

    用法:
        eco = EconomyService(db)
        # 查余额
        balance = eco.get_balance(family_id, CurrencyKind.MERIT)
        # 增减（原子操作）
        ok, new_balance = eco.transfer(
            family_id=family_id,
            currency=CurrencyKind.MERIT,
            amount=-50,
            reason="exchange",
            ref_id=exchange_id,
        )
        # 流水
        history = eco.get_transactions(family_id, CurrencyKind.POINTS, limit=20)
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS currency_balance(
                family_id TEXT NOT NULL,
                currency TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(family_id, currency)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS currency_transaction(
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ref_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_family_currency
                ON currency_transaction(family_id, currency, created_at DESC)
        """)

    # ── 余额操作 ────────────────────────────────────────────────────

    def get_balance(self, family_id: str, currency: CurrencyKind) -> int:
        """查询当前余额，不存在则返回 0。"""
        row = self.db.execute(
            "SELECT balance FROM currency_balance WHERE family_id=? AND currency=?",
            (family_id, currency.value)
        ).fetchone()
        return row[0] if row else 0

    def get_all_balances(self, family_id: str) -> dict[str, int]:
        """查询所有货币余额。"""
        rows = self.db.execute(
            "SELECT currency, balance FROM currency_balance WHERE family_id=?",
            (family_id,)
        ).fetchall()
        result = {c.value: 0 for c in CurrencyKind}
        for cur, bal in rows:
            result[cur] = bal
        return result

    def transfer(
        self,
        family_id: str,
        currency: CurrencyKind,
        amount: int,
        reason: str,
        ref_id: str = "",
    ) -> tuple[bool, int, str]:
        """原子性余额转移。
        返回 (成功, 新余额, 错误信息)。
        余额不足或超限时自动拒绝。
        """
        meta = CURRENCY_META[currency]
        now = int(time.time() * 1000)

        # 当前余额
        current = self.get_balance(family_id, currency)
        new_balance = current + amount

        # 校验
        if not meta.allow_negative and new_balance < meta.min_balance:
            return False, current, f"{meta.name} 不足（需要 {abs(amount)}，当前 {current}）"
        if new_balance > meta.max_balance:
            return False, current, f"{meta.name} 已达上限（{meta.max_balance}）"

        # 原子更新
        self.db.execute(
            """INSERT INTO currency_balance (family_id, currency, balance, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(family_id, currency) DO UPDATE SET
                   balance=excluded.balance, updated_at=excluded.updated_at""",
            (family_id, currency.value, new_balance, now)
        )
        self.db.execute(
            """INSERT INTO currency_transaction
               (family_id, currency, amount, balance_after, reason, ref_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (family_id, currency.value, amount, new_balance, reason, ref_id, now)
        )
        self.db.commit()
        return True, new_balance, ""

    def batch_transfer(
        self,
        family_id: str,
        transfers: list[tuple[CurrencyKind, int]],
        reason: str,
        ref_id: str = "",
    ) -> tuple[bool, list[str]]:
        """批量原子转移（全部成功或全部回滚）。
        transfers: [(currency, amount), ...]
        返回 (成功, 错误信息列表)。
        """
        errors = []
        results: list[tuple[bool, int]] = []
        for currency, amount in transfers:
            ok, new_bal, err = self.transfer(family_id, currency, amount, reason, ref_id)
            if not ok:
                errors.append(f"{currency.value}: {err}")
            results.append((ok, new_bal))
        if errors:
            self.db.rollback()
            return False, errors
        return True, []

    def get_transactions(
        self,
        family_id: str,
        currency: Optional[CurrencyKind] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Transaction]:
        """查询流水记录。"""
        if currency:
            rows = self.db.execute(
                """SELECT _id, family_id, currency, amount, balance_after,
                          reason, ref_id, created_at
                   FROM currency_transaction
                   WHERE family_id=? AND currency=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (family_id, currency.value, limit, offset)
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT _id, family_id, currency, amount, balance_after,
                          reason, ref_id, created_at
                   FROM currency_transaction
                   WHERE family_id=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (family_id, limit, offset)
            ).fetchall()
        return [
            Transaction(
                tx_id=r[0], family_id=r[1], currency=CurrencyKind(r[2]),
                amount=r[3], balance_after=r[4], reason=r[5],
                ref_id=r[6], created_at=r[7]
            )
            for r in rows
        ]
