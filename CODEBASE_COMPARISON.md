# RaidCaptain 两份代码对比报告

> 路径：实战版 `RaidCaptain_backup_20260901_142132 2/server/main.py`（Railway 在跑）
>       v3.1    `RaidCaptain_Server/src/raidcaptain_sync/`（独立项目，未跑）

## 1. 体量对比

| 维度 | 实战版（M2.7） | v3.1 重构版 |
|---|---|---|
| 入口文件 | 单文件 1333 行 | main.py 仅 178 行，路由全部分文件 |
| 路由文件 | 全在 main.py | 10 个 router 文件，~31 个端点 |
| 业务模块 | 无 | 10 个 module（含故事线、成就、公告、军衔、奖励、动画等）|
| 共享服务 | 工具函数散落 | 8 个 service（auth / oss_storage / event_bus / module_registry / revision / economy / admin_auth）|
| 数据库表 | 9 张 | 18 张 |
| 前端 parent.html | 64 KB（大量今天修的修复） | 92 KB（更完整，含战绩/时间线/学习记录/申诉/模板/管理）|

## 2. 端点对比

| 功能 | 实战版 | v3.1 |
|---|---|---|
| 家庭注册/登录 | ✅ | ✅ |
| 任务下发 CRUD | ✅ | ✅ |
| 模板 CRUD | ✅（含 dispatch）| ✅ |
| 设备事件 push | ✅ `/api/events` | ✅ |
| 家长历史事件 | ✅ `/api/parent/history` | ✅ |
| **学习时段** | ✅ 今天加的 | ✅ |
| 申诉审核 | ✅ | ✅ |
| 证据照片 | ✅（base64 存 SQLite）| ✅（**走 OSS**）|
| 统计/Today 状态 | ✅ | ✅ |
| **故事线 Bundle** | ❌ | ✅（含章节进度上报）|
| **成就系统** | ❌ | ✅（含解锁/领取）|
| **公告系统** | ❌ | ✅ |
| **军衔/奖励系统** | ❌ | ✅（rank + reward 模块）|
| **动画系统** | ❌ | ✅ |
| **按模块同步** | ❌ | ✅ `/api/tasks/sync?revisions=...` |
| **Admin 仪表盘** | 部分 | 完整（`/api/admin/overview`）|
| 健康检查 | `/api/status` | `/health` 含模块列表 |

## 3. 数据模型对比

| 表 | 实战版 | v3.1 |
|---|---|---|
| family / device / task / task_revision / event / template / appeal / evidence_file / patrol_session | ✅ | ✅ |
| **module_revision** | ❌ | ✅（各模块独立版本号）|
| **module_info** | ❌ | ✅ |
| **storyline_bundle / storyline_progress** | ❌ | ✅ |
| **achievement / family_achievement** | ❌ | ✅ |
| **announcement** | ❌ | ✅ |
| **admin / admin_audit_log** | ❌ | ✅ |
| **storyline_subscription** | ❌ | ✅ |

> v3.1 的 patrol_session 表结构跟实战版**完全一致**——好兆头，说明设计思路统一。

## 4. 架构差异（最关键的差异）

### 实战版 (单文件)
```
main.py  ← 全部 API 路由 + 全部业务逻辑 + 全部表操作
```
- 优点：单文件一目了然，调试方便
- 缺点：超过 1000 行后改动牵一发动全身

### v3.1 (模块化)
```
main.py        → 入口 + lifespan + include_router (178 行)
routers/       → 31 个 API 端点（薄层）
services/      → 共享基础设施 (auth / oss / event_bus / registry / revision)
modules/       → 10 个独立业务模块 (storyline / achievement / rank / reward...)
db/init_db.py  → 18 张表 schema
```
- 优点：加新功能 = 新建一个 module 文件 + 在 main 注册，**不动其他文件**
- 缺点：跨模块查询要找对模块

## 5. 实战版独有（v3.1 没有）

实战版是在生产环境经过几十次修复的版本，有些**细节 v3.1 没有**：

- ✅ `patrol_session` 表（学习记录）— 不过 v3.1 schema 也有
- ✅ 任务编辑（POST /api/parent/tasks 走 INSERT OR REPLACE）
- ✅ 模板编辑
- ✅ 证据图 inline 显示（`/api/evidence/{ev_id}` 返回二进制 + `?token=` 支持）
- ✅ 现场照 token 鉴权（`<img>` 不能带 header）
- ✅ WebSocket 实时同步 + 设备状态推送
- ✅ 战绩+战况合并「作战指挥台」+ 学习记录子 Tab
- ✅ admin 清空/重置/删除任务
- ✅ 父 token 兼 `?token=` query 双重鉴权
- ✅ api() JSON 解析容错（避免 500 纯文本导致前端崩）

## 6. v3.1 独有（实战版没有）

- ✅ **阿里云 OSS 存照片** — 不再把 base64 塞 SQLite（最重要的改进）
- ✅ **故事线 Bundle 系统** — 给孩子端下发章节内容
- ✅ **成就系统** — 自动解锁、领取
- ✅ **公告系统** — 家长发通知
- ✅ **军衔系统** — 等级晋升
- ✅ **奖励系统** — 礼品兑换
- ✅ **动画系统** — 孩子端动画触发
- ✅ **EventBus** — 模块间事件订阅解耦
- ✅ **ModuleRegistry** — 模块独立注册
- ✅ **RevisionManager** — 按模块独立版本号推送
- ✅ **Admin 模块** — 完整管理后台

## 7. 合并路线图（推荐 3 阶段）

### 阶段 1：把实战版关键修复搬进 v3.1（1-2 天）
不动 v3.1 架构，只修字段：
- evidence_file 改成返回二进制 + token query
- 加 `patrol_session` 写入逻辑
- 加 `learning-stats` / `patrol-sessions` 端点
- 加 task/template 编辑接口
- parent.html 升级到带"作战指挥台 + 学习记录"

**为什么先搬实战版**：
- 实战版已经在跑，是经过验证的代码
- 一次性把所有 bug 修复同步进 v3.1

### 阶段 2：把 v3.1 的模块按需搬进实战版（3-5 天）
**只搬最关键的**：
- ✅ `oss_storage.py`（搬到实战版 services/）— 解决 SQLite 膨胀
- ✅ `event_bus.py`（按需用）— 替代 main.py 里到处 `ws_push` 的硬编码

**不搬**：
- 故事线/成就/军衔等业务模块（实战版不需要）

### 阶段 3：部署到阿里云（2-3 天）
按 `DEPLOY_CN.md`：
- 阿里云 ECS 2C2G + 50G SSD + 阿里云 OSS
- Nginx 反代 + Let's Encrypt HTTPS
- systemd 守护 + 定时备份 + 云监控告警
- 迁移数据：实战版 DB → 阿里云，照片 base64 → OSS

## 8. 你提到的"Zeabur"选型分析

**Zeabur** 是国内友好的 PaaS 平台（总部在台湾，国内访问好），介于 Railway 和自建 ECS 之间：

| 项 | Railway | Zeabur | 阿里云 ECS + OSS |
|---|---|---|---|
| 部署速度 | 30 秒 | 30 秒 | 半天 |
| 国内访问 | 慢（100-300ms）| 快（< 50ms）| 极快（< 30ms）|
| 持久卷 | ✅ | ✅ | ✅ |
| WebSocket | ✅ | ✅ | ✅ |
| 数据位置 | 海外 | 可选国内 | 国内 |
| 定价 | $5-20/月 | ¥30-100/月 | ¥60-120/月 |
| 自由度 | 低 | 中 | 高 |
| 适合阶段 | 国外用户 / 演示 | **国内中小流量** | 国内生产 + 大流量 |

**对 RaidCaptain 的建议**：
- 如果只是给国内小批量用户（< 500 户）试运行 → **Zeabur 是性价比最高的选择**
- 如果要正式生产（> 1000 户 + 数据合规要求）→ 阿里云 ECS + OSS

## 9. 结论

**直接回答你最初的问题**：
1. **DEPLOY_CN.md 和 README.md 是 v3.1 项目的设计文档**，跟实战版没关系
2. **v3.1 是"未来形态"**，实战版是"当下形态"——两份不是替代关系，是演进关系
3. **最优解**：实战版继续在 Railway 跑 + 慢慢把 v3.1 的关键模块（OSS、EventBus）搬过来 + 试运行看是否要迁到 Zeabur / 阿里云

**现在最该做的**（30 分钟成本）：
- 不动代码
- 在 Zeabur 上 deploy 一份实战版做对比测试
- 看国内访问速度是否满意
- 满意则把 Railway 当开发环境、Zeabur 当生产
