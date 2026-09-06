"""
模块注册系统 - RaidCaptain Sync Server v3.1
所有模块通过此系统声明接入，main.py 不再硬编码 include_router。

设计灵感来自游戏引擎的 Plugin System:
- 每个模块是一个独立的 Plugin
- Plugin 声明自己的路由、订阅的事件
- main.py 只负责启动框架，不关心具体模块
- 加新模块 = 在 modules/ 目录新建文件 + 在 main.py 加一行 register
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)


# Module protocol
class RaidModule(Protocol):
    """所有模块必须实现的协议。"""

    @property
    def id(self) -> str:
        ...

    @property
    def display_name(self) -> str:
        ...

    @property
    def version(self) -> str:
        ...

    @property
    def description(self) -> str:
        ...

    def get_routers(self) -> list[APIRouter]:
        ...

    async def on_register(self, app: FastAPI) -> None:
        ...

    async def on_unregister(self) -> None:
        ...


@dataclass
class ModuleRegistry:
    """全局模块注册中心。"""

    _modules: dict[str, RaidModule] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def register(self, module: RaidModule) -> None:
        if module.id in self._modules:
            logger.warning("Module %s already registered, replacing", module.id)
        self._modules[module.id] = module
        if module.id not in self._order:
            self._order.append(module.id)
        logger.info(
            "Module registered: %s (%s v%s)",
            module.id, module.display_name, module.version,
        )

    def get(self, module_id: str) -> Optional[RaidModule]:
        return self._modules.get(module_id)

    def all(self) -> list[RaidModule]:
        return [self._modules[mid] for mid in self._order]

    def ids(self) -> list[str]:
        return list(self._order)

    async def init_all(self, app: FastAPI) -> None:
        for mid in self._order:
            module = self._modules[mid]
            try:
                # Optional: trigger module._ensure_schema once
                if hasattr(module, '_ensure_schema') and hasattr(module, '_open_db'):
                    try:
                        db = module._open_db()
                        # _open_db() may return connection or generator (FastAPI Depends)
                        if hasattr(db, 'execute'):
                            module._ensure_schema(db)
                        else:
                            module._ensure_schema(next(db))
                    except Exception:
                        pass
                for router in module.get_routers():
                    app.include_router(router)
                    logger.debug("  -> router registered: %s", router.prefix)
                await module.on_register(app)
            except Exception as e:
                logger.exception("Failed to init module %s: %s", mid, e)
                raise

    async def shutdown_all(self) -> None:
        for mid in reversed(self._order):
            module = self._modules[mid]
            try:
                await module.on_unregister()
            except Exception as e:
                logger.warning("Module %s shutdown error: %s", mid, e)

    def status(self) -> list[dict]:
        return [
            {
                "id": m.id,
                "name": m.display_name,
                "version": m.version,
                "routers": len(m.get_routers()),
            }
            for m in self.all()
        ]


# 全局单例
module_registry = ModuleRegistry()


# 模块基类
class BaseModule:
    """模块基类 - 推荐继承此类而非实现 Protocol。"""

    id: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""

    def __init__(self):
        self._routers: list[APIRouter] = []
        self._build_routers()

    def _build_routers(self) -> None:
        """子类重写：创建 self._routers"""
        pass

    def get_routers(self) -> list[APIRouter]:
        return self._routers

    async def on_register(self, app: FastAPI) -> None:
        pass

    async def on_unregister(self) -> None:
        pass