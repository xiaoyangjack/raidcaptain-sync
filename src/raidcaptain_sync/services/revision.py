"""
修订号管理 - RaidCaptain Sync Server v3.1
每个模块独立 revision，支持精准的"有变化才推送"。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ModuleRev:
    module_id: str
    rev: int
    updated_at: int


class RevisionManager:
    """模块修订号管理器。"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, family_id: str, module_id: str) -> int:
        """获取某模块当前 revision。"""
        row = self.db.execute(
            "SELECT rev FROM module_revision WHERE family_id=? AND module_id=?",
            (family_id, module_id),
        ).fetchone()
        return row["rev"] if row else 0

    def bump(self, family_id: str, module_id: str) -> int:
        """递增某模块 revision，返回新值。"""
        now = int(time.time() * 1000)
        self.db.execute(
            """INSERT INTO module_revision(family_id, module_id, rev, updated_at)
               VALUES(?,?,1,?)
               ON CONFLICT(family_id, module_id) DO UPDATE SET
               rev=rev+1, updated_at=excluded.updated_at""",
            (family_id, module_id, now),
        )
        return self.get(family_id, module_id)

    def reset(self, family_id: str, module_id: str) -> None:
        """重置某模块 revision 为 0。"""
        self.db.execute(
            "DELETE FROM module_revision WHERE family_id=? AND module_id=?",
            (family_id, module_id),
        )

    def get_all(self, family_id: str) -> dict[str, int]:
        """获取该家庭所有模块 revision。"""
        rows = self.db.execute(
            "SELECT module_id, rev FROM module_revision WHERE family_id=?",
            (family_id,),
        ).fetchall()
        return {r["module_id"]: r["rev"] for r in rows}

    def get_multi(self, family_id: str, modules: list[str]) -> dict[str, int]:
        """批量获取多个模块 revision。"""
        if not modules:
            return {}
        placeholders = ",".join("?" * len(modules))
        rows = self.db.execute(
            f"SELECT module_id, rev FROM module_revision "
            f"WHERE family_id=? AND module_id IN ({placeholders})",
            [family_id, *modules],
        ).fetchall()
        result = {m: 0 for m in modules}
        for r in rows:
            result[r["module_id"]] = r["rev"]
        return result

    def bump_multi(self, family_id: str, modules: list[str]) -> dict[str, int]:
        """批量递增多个模块 revision。"""
        for m in modules:
            self.bump(family_id, m)
        return self.get_multi(family_id, modules)

    def get_task_legacy(self, family_id: str) -> int:
        """兼容旧 API：返回任务 revision（优先查 task_revision）。"""
        row = self.db.execute(
            "SELECT rev FROM task_revision WHERE family_id=?", (family_id,)
        ).fetchone()
        if row:
            return row["rev"]
        return self.get(family_id, "tasks")

    def bump_task_legacy(self, family_id: str) -> int:
        """兼容旧 API：同时更新 task_revision 和 module_revision。"""
        self.db.execute(
            "INSERT INTO task_revision(family_id, rev) VALUES(?,1) "
            "ON CONFLICT(family_id) DO UPDATE SET rev=rev+1",
            (family_id,),
        )
        return self.bump(family_id, "tasks")


class StandardModules:
    AUTH = "auth"
    TASKS = "tasks"
    TEMPLATES = "templates"
    PATROL = "patrol"
    APPEALS = "appeals"
    STORYLINE = "storyline"
    ACHIEVEMENTS = "achievements"
    ANNOUNCEMENTS = "announcements"


ALL_MODULES: list[str] = [
    StandardModules.AUTH,
    StandardModules.TASKS,
    StandardModules.TEMPLATES,
    StandardModules.PATROL,
    StandardModules.APPEALS,
    StandardModules.STORYLINE,
    StandardModules.ACHIEVEMENTS,
    StandardModules.ANNOUNCEMENTS,
]
