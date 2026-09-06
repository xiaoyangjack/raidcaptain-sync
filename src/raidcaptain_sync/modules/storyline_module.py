"""
故事线模块 v3.2 - RaidCaptain Sync Server
Bundle v2 schema: DAG 多分支剧情 + 任意事件触发 + 版本迁移。

Bundle v2 结构:
{
  "bundle_id": "world_v2",
  "version": "2.0.0",
  "migration": {
    "from_version": "1.0.0",       # 从哪个版本迁移
    "preserve_progress": true,      # 是否保留孩子进度
    "reset_on_conflict": "soft"     # 冲突策略
  },
  "story_graph": {                  # DAG 替换线性 chapters
    "nodes": [
      {
        "node_id": "intro",
        "type": "story",             # story | choice | challenge | task
        "title": "...",
        "next": ["morning_choice"],   # 单一后继
        "rewards": {"merit": 100, "rank": "列兵", "animation": "anim_promote_private"}
      },
      {
        "node_id": "morning_choice",
        "type": "choice",             # 分支选择
        "options": [
          {"label": "坚持早起", "next": "early_rise"},
          {"label": "睡懒觉", "next": "late_rise"}
        ]
      },
      {
        "node_id": "early_rise",
        "type": "task",
        "completion": {
          "type": "event",            # 任意 EventKind
          "event": "task_completion",
          "task_id": "morning"
        },
        "rewards": {"merit": 50, "points": 20}
      }
    ]
  },
  "achievement_links": ["first_blood"],
  "rank_links": ["private", "sergeant"],
  "animation_links": ["anim_intro_v2", "anim_promote_private"]
}

兼容说明:
- v1 的 chapters 数组会被自动转换为 v2 的 story_graph.nodes（线性）
- v2 上传时若迁移规则存在，自动处理旧进度
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
from raidcaptain_sync.services.revision import StandardModules


# ── Schemas ────────────────────────────────────────────────────────


class BundleUploadRequest(BaseModel):
    bundle: dict


class ProgressReport(BaseModel):
    bundle_id: str
    node_id: str = ""                   # v2: 用 node_id
    completed_nodes: list[str] = Field(default_factory=list)
    unlocked_nodes: list[str] = Field(default_factory=list)
    chosen_path: dict = Field(default_factory=dict)  # 分支选择记录
    # v1 兼容字段
    chapter_id: str = ""
    episode_id: str = ""
    completed_episodes: list[str] = Field(default_factory=list)
    unlocked_chapters: list[str] = Field(default_factory=list)


# ── Bundle 校验 / 转换 ────────────────────────────────────────────


def is_v2_bundle(data: dict) -> bool:
    return "story_graph" in data or "migration" in data or data.get("schema_version", 1) >= 2


def v1_to_v2(data: dict) -> dict:
    """v1 格式自动转换为 v2 (DAG 线性结构)."""
    if is_v2_bundle(data):
        return data
    chapters = data.get("chapters", [])
    nodes = []
    prev_next = []
    for ch in chapters:
        chapter_id = ch.get("chapter_id")
        episodes = ch.get("episodes", [])
        for ep in episodes:
            ep_id = ep.get("episode_id")
            node_id = f"{chapter_id}/{ep_id}"
            nodes.append({
                "node_id": node_id,
                "type": ep.get("type", "story"),
                "title": ep.get("title", ""),
                "completion": ep.get("completion_condition", {}),
                "rewards": ep.get("rewards", {}),
                "next": []
            })
    for i in range(len(nodes) - 1):
        nodes[i]["next"] = [nodes[i + 1]["node_id"]]
    return {
        "schema_version": 2,
        "bundle_id": data.get("bundle_id", ""),
        "version": data.get("version", "1.0.0"),
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "story_graph": {"nodes": nodes},
        "achievement_links": [],
        "rank_links": [],
        "animation_links": [],
    }


def validate_bundle(data: dict) -> tuple[str, str, int, int]:
    """校验 Bundle (v1/v2 兼容)，返回 (bundle_id, version, nodes, edges)。"""
    if "bundle_id" not in data or not data["bundle_id"]:
        raise HTTPException(400, "bundle missing bundle_id")
    if "version" not in data or not data["version"]:
        raise HTTPException(400, "bundle missing version")
    if "title" not in data or not data["title"]:
        raise HTTPException(400, "bundle missing title")

    # 自动转换为 v2
    v2_data = v1_to_v2(data)
    nodes = v2_data["story_graph"]["nodes"]
    if not isinstance(nodes, list):
        raise HTTPException(400, "story_graph.nodes must be array")

    edges = 0
    for node in nodes:
        if "node_id" not in node:
            raise HTTPException(400, "node missing node_id")
        next_refs = node.get("next", [])
        if isinstance(next_refs, list):
            edges += len(next_refs)
        # choice 类型需要 options
        if node.get("type") == "choice" and not node.get("options"):
            raise HTTPException(400, f"choice node {node['node_id']} missing options")
    return data["bundle_id"], data["version"], len(nodes), edges


def compute_checksum(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def bundle_json(row) -> dict:
    return {
        "bundle_id": row["bundle_id"],
        "version": row["version"],
        "title": row["title"],
        "description": row["description"] or "",
        "total_chapters": row["total_chapters"],
        "total_episodes": row["total_episodes"],
        "active": bool(row["active"]),
        "published": bool(row["published_at"]),
        "size_bytes": row["size_bytes"],
        "checksum": row["checksum"] or "",
        "schema_version": row["schema_version"] if hasattr(row, "keys") and "schema_version" in row.keys() else 2,
        "created_at": row["created_at"],
        "published_at": row["published_at"],
    }


# ── Module ──────────────────────────────────────────────────────


class StorylineModule(BaseModule):
    id = StandardModules.STORYLINE
    display_name = "故事线"
    version = "1.1.0"  # Bumped: v2 DAG + migration
    description = "剧情 Bundle v2 (DAG 多分支 + 任意事件触发 + 版本迁移)"

    def __init__(self, get_db, auth_parent, auth_device, ws_push, device_sockets, bump_revision):
        self._get_db = get_db
        self._auth_parent = auth_parent
        self._auth_device = auth_device
        self._ws_push = ws_push
        self._device_sockets = device_sockets
        self._bump_revision = bump_revision
        self._routers: list = []
        self._build_routers()

    def _open_db(self):
        """Phase 6：module_registry.init_all 自动建表 helper."""
        return self._get_db()

    def _ensure_schema(self, db) -> None:
        # 添加 schema_version 字段（如果不存在）
        try:
            db.execute("ALTER TABLE storyline_bundle ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 2")
        except Exception:
            pass  # 字段已存在
        try:
            db.execute("ALTER TABLE storyline_bundle ADD COLUMN migration_from TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # 扩展 storyline_progress 表以支持 DAG 节点
        try:
            db.execute("ALTER TABLE storyline_progress ADD COLUMN completed_nodes TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE storyline_progress ADD COLUMN chosen_path TEXT NOT NULL DEFAULT '{}'")
        except Exception:
            pass

    def _build_routers(self) -> None:
        parent_router = APIRouter(prefix="/api/parent/storyline", tags=["storyline"])
        device_router = APIRouter(prefix="/api/device/storyline", tags=["storyline"])

        @parent_router.post("/bundles")
        async def upload_bundle(
            body: BundleUploadRequest,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            self._ensure_schema(db)

            bundle_id, version, total_nodes, total_edges = validate_bundle(body.bundle)
            title = body.bundle.get("title", "")
            description = body.bundle.get("description", "")
            bundle_raw = json.dumps(body.bundle, ensure_ascii=False)
            checksum = compute_checksum(body.bundle)
            size_bytes = len(bundle_raw.encode())
            schema_version = 2 if is_v2_bundle(body.bundle) else 1
            migration_from = body.bundle.get("migration", {}).get("from_version", "")

            # 查找已有版本
            existing = db.execute(
                "SELECT _id, active FROM storyline_bundle "
                "WHERE family_id=? AND bundle_id=? AND version=?",
                (fid, bundle_id, version),
            ).fetchone()

            now = int(time.time() * 1000)
            if existing:
                db.execute(
                    """UPDATE storyline_bundle SET title=?, description=?,
                       total_chapters=?, total_episodes=?, bundle_json=?,
                       size_bytes=?, checksum=?, schema_version=?, migration_from=? WHERE _id=?""",
                    (title, description, total_nodes, total_edges,
                     bundle_raw, size_bytes, checksum, schema_version,
                     migration_from, existing["_id"]),
                )
                bid = existing["_id"]
            else:
                cur = db.execute(
                    """INSERT INTO storyline_bundle(family_id, bundle_id, version,
                        title, description, total_chapters, total_episodes,
                        bundle_json, size_bytes, checksum, schema_version, migration_from,
                        active, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (fid, bundle_id, version, title, description,
                     total_nodes, total_edges, bundle_raw, size_bytes, checksum,
                     schema_version, migration_from, now),
                )
                bid = cur.lastrowid

            # 处理 v1 → v2 迁移（如果存在旧版本）
            if migration_from:
                self._handle_migration(db, fid, bundle_id, migration_from, version)

            self._bump_revision(db, fid)

            await event_bus.publish(EventContext(
                family_id=fid, kind=EventKind.BUNDLE_PUBLISHED,
                data={"bundle_id": bundle_id, "version": version,
                      "title": title, "schema_version": schema_version},
                db=db,
            ))

            return {"ok": True, "bundle_id": bundle_id, "version": version,
                    "schema_version": schema_version,
                    "migration_from": migration_from,
                    "checksum": checksum, "size_bytes": size_bytes}

        @parent_router.get("/bundles")
        def list_bundles(
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            rows = db.execute(
                "SELECT * FROM storyline_bundle WHERE family_id=? "
                "ORDER BY active DESC, created_at DESC",
                (fam["id"],),
            ).fetchall()
            return {"bundles": [bundle_json(r) for r in rows]}

        @parent_router.get("/bundles/{bundle_id}")
        def get_bundle_detail(
            bundle_id: str,
            version: str = "",
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            if version:
                row = db.execute(
                    "SELECT * FROM storyline_bundle "
                    "WHERE family_id=? AND bundle_id=? AND version=?",
                    (fam["id"], bundle_id, version),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM storyline_bundle "
                    "WHERE family_id=? AND bundle_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (fam["id"], bundle_id),
                ).fetchone()
            if not row:
                raise HTTPException(404, "bundle not found")
            return bundle_json(row)

        @parent_router.delete("/bundles/{bundle_id}")
        def delete_bundle(
            bundle_id: str,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            fam = self._auth_parent(db, authorization)
            db.execute(
                "UPDATE storyline_bundle SET active=0 "
                "WHERE family_id=? AND bundle_id=?",
                (fam["id"], bundle_id),
            )
            self._bump_revision(db, fam["id"])
            return {"ok": True}

        @parent_router.post("/bundles/{bundle_id}/publish")
        async def publish_bundle(
            bundle_id: str,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE storyline_bundle SET published_at=? "
                "WHERE family_id=? AND bundle_id=? AND active=1",
                (now, fid, bundle_id),
            )
            rev = self._bump_revision(db, fid)
            await self._ws_push(fid, self._device_sockets, {
                "type": "bundle_published",
                "bundle_id": bundle_id,
                "revision": rev,
            })
            return {"ok": True, "revision": rev}

        @parent_router.post("/bundles/migrate", summary="手动触发迁移")
        async def migrate_bundle(
            body: dict,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            """手动从旧版本迁移到新版本。"""
            self._ensure_schema(db)
            fam = self._auth_parent(db, authorization)
            fid = fam["id"]
            from_version = body.get("from_version", "")
            to_bundle_id = body.get("bundle_id", "")
            to_version = body.get("version", "")
            result = self._handle_migration(db, fid, to_bundle_id, from_version, to_version)
            return result

        # 设备端
        @device_router.get("/bundles")
        def device_list_bundles(
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            rows = db.execute(
                "SELECT * FROM storyline_bundle "
                "WHERE family_id=? AND active=1 AND published_at IS NOT NULL "
                "ORDER BY created_at DESC",
                (dev["family_id"],),
            ).fetchall()
            return {"bundles": [bundle_json(r) for r in rows]}

        @device_router.get("/bundles/{bundle_id}/download")
        async def device_download_bundle(
            bundle_id: str,
            version: str = "",
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            if version:
                row = db.execute(
                    "SELECT * FROM storyline_bundle "
                    "WHERE family_id=? AND bundle_id=? AND version=? "
                    "AND active=1 AND published_at IS NOT NULL",
                    (dev["family_id"], bundle_id, version),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM storyline_bundle "
                    "WHERE family_id=? AND bundle_id=? AND active=1 "
                    "AND published_at IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (dev["family_id"], bundle_id),
                ).fetchone()
            if not row:
                raise HTTPException(404, "bundle not available")

            now = int(time.time() * 1000)
            db.execute(
                """INSERT INTO storyline_progress(family_id, bundle_id,
                    device_token_hash, device_name, version, downloaded_at, last_progress_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(family_id, bundle_id, device_token_hash) DO UPDATE SET
                    downloaded_at=excluded.downloaded_at,
                    version=excluded.version""",
                (dev["family_id"], bundle_id, dev["token_hash"],
                 dev["name"], row["version"], now, now),
            )
            db.commit()

            await event_bus.publish(EventContext(
                family_id=dev["family_id"], kind=EventKind.BUNDLE_DOWNLOADED,
                device_name=dev["name"], device_token_hash=dev["token_hash"],
                data={"bundle_id": bundle_id, "version": row["version"]},
                db=db,
            ))
            return {
                "bundle": json.loads(row["bundle_json"]),
                "checksum": row["checksum"],
                "version": row["version"],
                "schema_version": row["schema_version"] if hasattr(row, "keys") and "schema_version" in row.keys() else 2,
            }

        @device_router.post("/progress")
        async def device_report_progress(
            body: ProgressReport,
            authorization: str | None = Header(None),
            db=Depends(self._get_db),
        ):
            self._ensure_schema(db)
            dev = self._auth_device(db, authorization)
            fid = dev["family_id"]
            now = int(time.time() * 1000)

            # v2 用 node_id 和 completed_nodes，v1 兼容用 chapter_id
            completed = body.completed_nodes or body.completed_episodes
            unlocked = body.unlocked_nodes or body.unlocked_chapters
            current = body.node_id or body.chapter_id

            db.execute(
                """UPDATE storyline_progress SET
                    current_chapter=?, completed_episodes=?, unlocked_chapters=?,
                    completed_nodes=?, chosen_path=?, last_progress_at=?
                   WHERE family_id=? AND bundle_id=? AND device_token_hash=?""",
                (current or "",
                 json.dumps(body.completed_episodes),
                 json.dumps(body.unlocked_chapters),
                 json.dumps(completed),
                 json.dumps(body.chosen_path),
                 now, fid, body.bundle_id, dev["token_hash"]),
            )

            if body.episode_id:
                await event_bus.publish(EventContext(
                    family_id=fid, kind=EventKind.EPISODE_COMPLETED,
                    device_name=dev["name"], device_token_hash=dev["token_hash"],
                    data={"bundle_id": body.bundle_id, "episode_id": body.episode_id},
                    db=db,
                ))

            for nid in unlocked:
                await event_bus.publish(EventContext(
                    family_id=fid, kind=EventKind.CHAPTER_UNLOCKED,
                    device_name=dev["name"], device_token_hash=dev["token_hash"],
                    data={"bundle_id": body.bundle_id, "node_id": nid},
                    db=db,
                ))

            return {"ok": True, "completed_nodes": completed}

        self._routers = [parent_router, device_router]

    def _handle_migration(
        self, db, family_id: str, bundle_id: str,
        from_version: str, to_version: str,
    ) -> dict:
        """处理 v1 → v2 迁移。保留孩子的旧进度。"""
        # 查找旧版本进度
        old_progress = db.execute(
            """SELECT completed_episodes, unlocked_chapters, current_chapter,
                      device_token_hash, version
               FROM storyline_progress
               WHERE family_id=? AND bundle_id=? AND version=?""",
            (family_id, bundle_id, from_version),
        ).fetchall()

        if not old_progress:
            return {"ok": False, "reason": f"无 v{from_version} 进度可迁移"}

        migrated_count = 0
        for prog in old_progress:
            old_completed = json.loads(prog[0] or "[]")
            old_unlocked = json.loads(prog[1] or "[]")
            old_current = prog[2]
            device_hash = prog[3]

            # v1 episode "ch1/ep1" → v2 node "ch1/ep1"
            new_completed = old_completed
            new_unlocked = old_unlocked

            # 更新现有 progress 记录（用新版本）
            db.execute(
                """UPDATE storyline_progress SET
                    version=?, completed_nodes=?,
                    last_progress_at=?
                   WHERE family_id=? AND bundle_id=? AND device_token_hash=? AND version=?""",
                (to_version, json.dumps(new_completed), int(time.time() * 1000),
                 family_id, bundle_id, device_hash, from_version),
            )
            migrated_count += 1

        db.commit()
        return {
            "ok": True,
            "from_version": from_version,
            "to_version": to_version,
            "migrated_devices": migrated_count,
        }

def create_storyline_module(
    get_db, auth_parent, auth_device, ws_push, device_sockets, bump_revision,
):
    return StorylineModule(
        get_db, auth_parent, auth_device, ws_push, device_sockets, bump_revision,
    )
