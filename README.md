# RaidCaptain Sync Server (v3.1 模块化版)

家庭/家长/孩子设备 云端同步服务 - **专业游戏引擎级模块化架构**。

## 🎯 演进历程

| 版本 | 架构 | 核心改进 |
|------|------|---------|
| M2.5 | 单文件 1330+ 行 | 原版（已重构） |
| v3.0 | 分层 routers/services | OSS 照片存储、WAL + 索引、/health 探针 |
| **v3.1** | **模块化 + 事件总线 + 多模块修订号** | 故事线 Bundle、成就系统、公告系统 |

## 🏗 v3.1 核心架构

```
┌─────────────────────────────────────────────────────────┐
│                   RaidCaptain Sync v3.1                │
├─────────────────────────────────────────────────────────┤
│                                                          │
│   ┌────────────────────────────────────────────────┐   │
│   │              Module Registry                  │   │
│   │   (5 个业务模块 + 1 个兼容层)                  │   │
│   └────────────────────────────────────────────────┘   │
│           │                                              │
│  ┌────────┼──────────┬──────────┬──────────┐            │
│  ▼        ▼          ▼          ▼          ▼            │
│ ┌──┐  ┌─────┐    ┌────────┐ ┌──────┐ ┌──────┐         │
│ │Auth│ │Tasks│    │Storyline│ │Achie│ │Annou│        │
│ │   │  │     │    │(剧情)   │ │vements│ │nce│         │
│ └──┘  └─────┘    └────────┘ └──────┘ └──────┘         │
│  │        │           │         │        │             │
│  └────────┴───────────┴─────────┴────────┘             │
│              │                                          │
│  ┌───────────▼───────────────────────────────┐        │
│  │        共享基础设施                         │        │
│  │  EventBus │ RevisionMgr │ OSSStorage       │        │
│  │  Auth     │ DB (WAL)    │ WS Push           │        │
│  └────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

### v3.1 三大创新

| 创新 | 设计灵感 | 解决的问题 |
|------|---------|----------|
| **EventBus（事件总线）** | 游戏引擎 Event Dispatcher | 强类型事件、模块自动订阅 |
| **ModuleRegistry（模块注册）** | 游戏引擎 Plugin System | 加新功能零硬编码改 main.py |
| **RevisionManager（多模块修订号）** | 游戏引擎 Asset Versioning | 各模块独立版本、精准推送 |

## 📦 当前已注册的模块

```
GET /health
{
  "modules": [
    {"id": "auth", "name": "认证与家庭管理", "version": "1.0.0"},
    {"id": "tasks", "name": "任务系统", "version": "1.0.0"},
    {"id": "storyline", "name": "故事线", "version": "1.0.0"},
    {"id": "achievements", "name": "成就系统", "version": "1.0.0"},
    {"id": "announcements", "name": "公告系统", "version": "1.0.0"}
  ]
}
```

## 📁 项目结构

```
RaidCaptain_Server/
├── src/raidcaptain_sync/
│   ├── main.py                # FastAPI 入口 + 模块注册中心
│   ├── config.py              # 环境变量配置
│   ├── deps.py                # get_db, auth_*, ws_push
│   ├── db/init_db.py          # schema + 6 索引 + 数据迁移
│   │
│   ├── services/              # 共享基础设施
│   │   ├── auth.py            # PBKDF2 + Token 生成
│   │   ├── oss_storage.py     # 阿里云 OSS 通用上传
│   │   ├── event_bus.py       # 事件总线 (EventKind + EventBus)
│   │   ├── module_registry.py # 模块注册中心 (BaseModule)
│   │   └── revision.py        # 多模块修订号 (RevisionManager)
│   │
│   ├── modules/               # 业务模块（独立功能）
│   │   ├── auth_module.py     # 认证与配对
│   │   ├── task_module.py     # 任务下发 + 模块同步
│   │   ├── storyline_module.py# 故事线 Bundle 系统
│   │   ├── achievement_module.py # 成就系统
│   │   └── announcement_module.py # 公告系统
│   │
│   ├── routers/               # Legacy 兼容路由（保留 v3.0 客户端）
│   │   ├── events.py          # 事件历史/统计
│   │   ├── templates.py       # 任务模板
│   │   ├── appeals.py         # 申诉审核
│   │   ├── evidence.py        # 证据查看
│   │   ├── admin.py           # 数据管理
│   │   ├── device.py          # 设备事件上报
│   │   └── status.py          # 状态/健康检查
│   │
│   └── websockets/realtime.py # WS 实时通道
├── static/parent.html         # 家长网页端
├── scripts/backup_db.sh       # 自动备份脚本
├── Dockerfile                 # 多阶段构建
├── docker-compose.yml         # 本地编排
├── requirements.txt
├── .env.example
├── DEPLOY_CN.md               # 阿里云/腾讯云部署指南
└── README.md
```

## 🚀 快速开始

### 本地开发

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境（可选 OSS）
cp .env.example .env

# 3. 启动
uvicorn raidcaptain_sync.main:app --host 0.0.0.0 --port 8000 --reload

# 4. 验证模块加载
curl http://localhost:8000/health | jq .modules
```

### Docker 部署

```bash
docker compose up -d
docker compose logs -f app
```

## 🆕 v3.1 新增端点

### 故事线系统 (StorylineModule)

```
POST   /api/parent/storyline/bundles              上传 Bundle
GET    /api/parent/storyline/bundles              列出所有 Bundle
GET    /api/parent/storyline/bundles/{id}         Bundle 详情
DELETE /api/parent/storyline/bundles/{id}         删除 Bundle
POST   /api/parent/storyline/bundles/{id}/publish 一键发布给设备
GET    /api/device/storyline/bundles              设备拉取可用 Bundle
GET    /api/device/storyline/bundles/{id}/download 设备下载完整 Bundle JSON
POST   /api/device/storyline/progress             上报章节进度
```

### 成就系统 (AchievementModule)

```
GET    /api/parent/achievements           家长查看所有成就定义
GET    /api/parent/achievements/unlocked  已解锁成就
POST   /api/parent/achievements           创建成就定义
POST   /api/parent/achievements/{id}/claim 领取奖励
GET    /api/achievements/progress         设备端成就进度
```

### 公告系统 (AnnouncementModule)

```
POST   /api/parent/announcements              创建公告
GET    /api/parent/announcements              列出公告
POST   /api/parent/announcements/{id}/read   标记已读
DELETE /api/parent/announcements/{id}         删除公告
```

### 按模块精准同步

```
GET /api/tasks/sync?revisions={"tasks":5,"storyline":2}
→ 设备端只拉取有变化的模块
```

### Admin 仪表盘

```
GET /api/admin/overview
→ 各模块统计 + 修订号 + 事件订阅关系
```

## 🎯 添加新功能的标准流程

```
1. 在 src/raidcaptain_sync/modules/ 创建新模块文件
   继承 BaseModule，实现 id/display_name/version/description

2. 在 _build_routers 中创建 APIRouter 并添加路由

3. 在 main.py lifespan 中：
   module_registry.register(create_your_module(...))

4. 如果新模块触发/响应事件：
   from raidcaptain_sync.services.event_bus import event_bus
   event_bus.subscribe(EventKind.YOUR_EVENT, your_handler)
   # 或在事件发布时使用：
   await event_bus.publish(EventContext(
       family_id=fid,
       kind=EventKind.YOUR_EVENT,
       data={...}
   ))

5. 完成后访问 /health 检查模块已注册
```

## 🔧 配置

参见 `.env.example`：
- `RAID_SYNC_DIR` — SQLite 数据目录（默认 `/data`）
- `RAID_OSS_*` — 阿里云 OSS 配置（不配置则降级为 base64）
- `RAID_PARENT_TOKEN_EXPIRY_DAYS` — 家长 Token 过期（默认 30 天）

## 🧪 测试

```python
from fastapi.testclient import TestClient
from raidcaptain_sync.main import app

with TestClient(app) as c:
    # Health
    r = c.get('/health')
    print(r.json()['modules'])

    # Full lifecycle
    r = c.post('/api/family/register', json={'password': 'test'})
    fc = r.json()['family_code']
    r = c.post('/api/parent/login', json={'family_code': fc, 'password': 'test'})
    # ...
```

## 📝 主要变更记录

- **v3.1.0** (2026-09-06)
  - 模块化架构（5 个独立业务模块）
  - 事件总线（EventKind 强类型枚举）
  - 多模块修订号（RevisionManager）
  - 故事线 Bundle 系统
  - 成就系统（自动解锁）
  - 公告系统
  - 按模块精准同步（`/api/tasks/sync`）
  - Admin 仪表盘（`/api/admin/overview`）
  - 模块状态显示在 `/health`

- **v3.0.0** (2026-09-06)
  - 完整模块化重构
  - 照片 OSS 化（核心改进）
  - SQLite WAL + 索引
  - `/health` 健康探针
  - 修复 main.py review_appeal 参数数量 bug

- **M2.5** (2026-09-02) — 原版，单文件 1330+ 行

## 🔐 安全特性

- PBKDF2-SHA256 密码哈希（120k 迭代）
- Token SHA-256 哈希存储
- HMAC timing-safe 密码比对
- 8 位随机家庭码（10⁸ 组合）
- Bearer Token + ?token= 兼容

## 📊 性能基线

- 单实例（2C2G）：~200 户家庭并发
- WebSocket：30 秒心跳，超时离线
- SQLite WAL 模式支持读并发
- OSS 异步签名 URL（7200 秒有效）

## 🔜 后续规划

- [ ] PostgreSQL 迁移（>2000 户）
- [ ] Redis Pub/Sub（多实例 WS）
- [ ] 限流中间件（slowapi）
- [ ] loguru 结构化日志
- [ ] Alembic 数据库迁移
- [ ] Prometheus + Grafana
- [ ] refresh_token 短期化