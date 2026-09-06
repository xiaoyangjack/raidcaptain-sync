"""
RaidCaptain Sync Server - FastAPI 入口 v3.1
=============================================
家庭/家长/孩子设备 云端同步服务

v3.1 架构：
- 模块化（ModuleRegistry）
- 事件总线（EventBus + EventKind 枚举）
- 多模块修订号（RevisionManager）
- 故事线 Bundle 系统
- 成就系统

启动：
    uvicorn raidcaptain_sync.main:app --host 0.0.0.0 --port 8000
"""
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from raidcaptain_sync import __version__
from raidcaptain_sync.config import settings
from raidcaptain_sync.db.init_db import init_db
from raidcaptain_sync.deps import (
    auth_device, auth_parent, bump_revision, device_sockets, get_db,
    get_revision, parent_sockets, ws_push,
)
from raidcaptain_sync.modules.achievement_module import create_achievement_module
from raidcaptain_sync.modules.admin_module import create_admin_module
from raidcaptain_sync.modules.animation_module import create_animation_module
from raidcaptain_sync.modules.announcement_module import create_announcement_module
from raidcaptain_sync.modules.auth_module import create_auth_module
from raidcaptain_sync.modules.parent_api import create_parent_api_module
from raidcaptain_sync.modules.rank_module import create_rank_module
from raidcaptain_sync.modules.reward_module import create_reward_module
from raidcaptain_sync.modules.storyline_module import create_storyline_module
from raidcaptain_sync.modules.task_module import create_task_module
from raidcaptain_sync.services.auth import make_token as make_task_id
from raidcaptain_sync.services.event_bus import event_bus
from raidcaptain_sync.services.module_registry import module_registry
from raidcaptain_sync.services.revision import RevisionManager


# ── 日志 ────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("raidcaptain_sync")


# ── Lifespan（替代 deprecated 的 startup 事件）──────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子。"""
    logger.info("=" * 60)
    logger.info("RaidCaptain Sync v%s 启动中...", __version__)
    logger.info("数据库路径: %s", settings.db_path)

    # 1. 初始化数据库 + schema + 索引 + 数据迁移
    init_db()
    logger.info("✅ 数据库初始化完成（已启用 WAL 模式 + 索引）")

    # 1.1 初始化 EconomyService 表（family_balance / currency_balance / points_transaction）
    # 必须在主服务启动时显式触发一次，否则 reward/approval 接口调用时才会懒建表
    from raidcaptain_sync.services.economy import EconomyService
    with sqlite3.connect(str(settings.db_path), timeout=30.0) as _db:
        EconomyService(_db)  # 触发表创建
    logger.info("✅ 经济系统表已就绪（family_balance / currency_balance / points_transaction）")

    # 2. 注册所有模块（v3.2 模块化架构）
    logger.info("📦 注册业务模块...")

    # AuthModule 必须最先注册（认证是所有业务的基础）
    module_registry.register(create_auth_module())

    def bump_rev(db, fid, module_id: str = None):
        if module_id is None:
            return RevisionManager(db).bump_task_legacy(fid)
        return RevisionManager(db).bump(fid, module_id)

    def get_rev(db, fid, module_id: str = None):
        if module_id is None:
            return RevisionManager(db).get_task_legacy(fid)
        return RevisionManager(db).get(fid, module_id)

    module_registry.register(create_task_module(
        get_db, auth_parent, auth_device, ws_push, device_sockets,
        bump_rev, get_rev, make_task_id,
    ))
    module_registry.register(create_storyline_module(
        get_db, auth_parent, auth_device, ws_push, device_sockets, bump_rev,
    ))
    module_registry.register(create_achievement_module(
        get_db, bump_rev, ws_push, parent_sockets,
    ))
    module_registry.register(create_announcement_module(
        get_db, bump_rev, ws_push, parent_sockets,
    ))
    # v3.2 新增模块
    module_registry.register(create_rank_module(
        get_db, ws_push, parent_sockets, bump_rev,
    ))
    module_registry.register(create_reward_module(
        get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_rev,
    ))
    # v3.3 新增模块
    module_registry.register(create_admin_module(
        get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_rev,
    ))
    module_registry.register(create_animation_module(
        get_db, auth_parent, auth_device, ws_push, parent_sockets, bump_rev,
    ))
    # v3.3.1 新增：Parent 端专用 API（余额/流水/空间/种子数据）
    module_registry.register(create_parent_api_module(
        get_db, auth_parent, ws_push, parent_sockets, bump_rev,
    ))

    # 3. 初始化模块：调用 on_register + 注册路由
    await module_registry.init_all(app)
    logger.info("✅ 已注册 %d 个模块:", len(module_registry.ids()))
    for s in module_registry.status():
        logger.info("   • %s v%s - %s (%d routes)",
                    s["id"], s["version"], s["name"], s["routers"])

    # 4. 静态资源（家长网页）
    # 多路径查找：兼容本地 / 容器 / Railway Nixpacks
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    SEARCH_PATHS = [
        Path("/app/static"),  # Railway Dockerfile 路径
        Path("/app/src/static"),  # Railway Nixpacks
        Path(__file__).resolve().parent.parent.parent / "static",  # 本地开发（/app/src/raidcaptain_sync/main.py -> /app/src/static）
        Path(__file__).resolve().parent.parent / "static",  # 本地开发 alt
    ]
    STATIC_DIR = None
    for path in SEARCH_PATHS:
        if path.exists() and (path / "parent.html").exists():
            STATIC_DIR = path
            break

    if STATIC_DIR:
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="parent-static")

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "parent.html")
        logger.info("✅ 静态资源已挂载: /static + / → %s/parent.html", STATIC_DIR)
    else:
        logger.warning("⚠️  parent.html 未找到，以下路径均不存在:")
        for path in SEARCH_PATHS:
            exists = path.exists()
            parent_exists = (path / "parent.html").exists() if exists else False
            logger.warning("     %s | exists=%s | parent.html=%s", path, exists, parent_exists)
    from raidcaptain_sync.services.oss_storage import oss_storage
    if oss_storage._enabled:
        logger.info("✅ OSS 存储已启用 (bucket=%s)", settings.oss_bucket)
    else:
        logger.warning(
            "⚠️  OSS 未配置（OSS_ACCESS_KEY_ID/SECRET 未设置），证据文件将降级存储为 base64"
        )

    logger.info("=" * 60)
    logger.info("🎮 服务就绪，等待连接...")
    yield

    logger.info("🔌 RaidCaptain Sync 关闭中...")
    await module_registry.shutdown_all()


# ── FastAPI 实例 ───────────────────────────────────────────────
app = FastAPI(
    title="RaidCaptain Sync",
    version=__version__,
    description="家庭/家长/孩子设备 云端同步服务（OSS 照片 + 实时 WebSocket + 模块化架构）",
    lifespan=lifespan,
)


# ── 状态路由（必须在静态资源挂载前注册，避免被 '/' 覆盖）────
from raidcaptain_sync.routers.status import router as status_router
from raidcaptain_sync.websockets.realtime import router as ws_router
app.include_router(status_router)
app.include_router(ws_router)

# ── Legacy routers（保持 v3.0 客户端兼容性，在模块注册之后 include，
#    确保新模块的 /api/* 路由覆盖 legacy 中的同名路由）──────────────
from raidcaptain_sync.routers.evidence import router as evidence_router
from raidcaptain_sync.routers.events import router as events_router
from raidcaptain_sync.routers.device import router as device_router
from raidcaptain_sync.routers.templates import router as templates_router
from raidcaptain_sync.routers.appeals import router as appeals_router
from raidcaptain_sync.routers.admin import router as admin_router
app.include_router(evidence_router)
app.include_router(events_router)
app.include_router(device_router)
app.include_router(templates_router)
app.include_router(appeals_router)
app.include_router(admin_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "raidcaptain_sync.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )