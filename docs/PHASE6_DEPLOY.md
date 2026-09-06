# RaidCaptain Sync v3.3.1 - Phase 6 部署指南

## 📦 变更总览（Phase 6 全部完成）

### 后端新增
| 模块 | 端点 | 说明 |
|------|------|------|
| `parent_api` | `GET /api/parent/balance` | 锐察功绩 + 纲纪指数双轨余额 |
| `parent_api` | `GET /api/parent/transactions` | 流水记录（merit/discipline） |
| `parent_api` | `POST /api/parent/balance/adjust` | 手动调分 |
| `parent_api` | `GET /api/admin/space-usage` | 数据库空间统计 |
| `parent_api` | `POST /api/admin/seed-rewards` | 播撒 4 大分类 + 19 级军衔种子数据 |

### 字段修正（关键！）

#### 1. 货币双轨严格化
| 字段名 | 中文显示 | 用途 | UI 颜色 | Android 端对照 |
|--------|---------|------|---------|---------------|
| `merit` | 🎖️ 锐察功绩 | 商城兑换 + 消费 | `#E8A33D`（Amber） | `Gba.Amber` |
| `discipline` | 🏛️ 纲纪指数 | 军衔晋升依据 | `#8BAC0F`（Green） | `Gba.Green` |

**绝对约束**：
- ✅ `merit` **不能**用于晋升判断
- ✅ `discipline` **不能**用于商城消费
- ✅ 流水 `reason` 字段使用 Android 端枚举（task_reward / task_penalty / exchange / manual / appeal_pass / appeal_rollback）

#### 2. `store_item.price_currency` 默认值修正
- 旧：`'points'`（错误！Android 端实际是 merit）
- 新：`'merit'`（与 Android `prize.points_cost` 实际扣除 `merit_balance` 一致）
- 启动时自动执行 `UPDATE store_item SET price_currency='merit' WHERE price_currency='points'`

#### 3. 军衔晋升规则修正
- 旧：`rank_def.required_currency='merit'`（错误！军衔本应由 discipline 晋升）
- 新：军衔晋升 = `discipline`（纲纪指数）扣除
- 自动迁移：`UPDATE rank_def SET required_currency='discipline' WHERE required_currency IN ('points', 'merit')`

### parent.html 精修
1. **顶部 5 块核心数据**：新增 🎖️ 锐察功绩 + 🏛️ 纲纪指数 + 军衔 + 军衔进度
2. **军需处 Tab（新增 Tab 4）**：4 大分类（游戏时间/娱乐特权/实物奖励/荣誉特权）+ 锐察功绩消费
3. **数据管理 Tab**：新增空间占用卡片（DB 文件/总行数/数据表数）
4. **入场刷新逻辑**：`loadBalanceAndRank()` 自动调用

### 数据库表
- `currency_balance`（经济系统表）
- `currency_transaction`（流水表）
- `family_rank`（新增 discipline 字段）
- `rank_def`（修正 required_currency 枚举）

## 🚀 部署步骤

### 1. Railway 部署（推荐）

```bash
# 在 RaidCaptain_Server 目录
git add .
git commit -m "Phase 6: 经济系统严格化 + parent.html 精修"
git push origin main

# Railway 自动部署
# 1. 登录 https://railway.app
# 2. 选择项目 raidcaptain-sync-production
# 3. Settings → GitHub → 选择仓库 → 自动部署
# 4. 等待 3-5 分钟
```

### 2. 数据迁移（首次升级时执行）

```bash
# Railway Shell 执行：
PYTHONPATH=src python3 -c "
import sqlite3
db = sqlite3.connect('/data/sync.db')
db.executescript('''
  UPDATE store_item SET price_currency='merit' WHERE price_currency='points';
  UPDATE rank_def SET required_currency='discipline' WHERE required_currency IN ('points', 'merit');
''')
db.commit()
print('✅ Migration done')
"
```

### 3. 种子数据（首次部署必须执行）

```bash
# 通过 API 一次性播撒
curl -X POST https://raidcaptain-sync-production.up.railway.app/api/admin/seed-rewards \
  -H "Authorization: Bearer <parent_token>"

# 预期响应：
# {
#   "ok": true,
#   "seeded_items": 16,
#   "seeded_ranks": 19,
#   "categories": ["game_time", "entertainment", "physical", "privilege"]
# }
```

### 4. 验证（E2E）

```bash
# 1. 登录家长
TOKEN=$(curl -s -X POST https://raidcaptain-sync-production.up.railway.app/api/parent/login \
  -H 'Content-Type: application/json' \
  -d '{"family_code":"<your_family_code>","password":"<your_password>"}' | jq -r .parent_token)

# 2. 查询余额
curl -s https://raidcaptain-sync-production.up.railway.app/api/parent/balance \
  -H "Authorization: Bearer $TOKEN" | jq .

# 3. 查询军需处
curl -s https://raidcaptain-sync-production.up.railway.app/api/parent/rewards/items \
  -H "Authorization: Bearer $TOKEN" | jq '.items | length'

# 4. 查询军衔
curl -s https://raidcaptain-sync-production.up.railway.app/api/parent/ranks/progress \
  -H "Authorization: Bearer $TOKEN" | jq .

# 5. 查询空间
curl -s https://raidcaptain-sync-production.up.railway.app/api/admin/space-usage \
  -H "Authorization: Bearer $TOKEN" | jq .
```

## 🔧 回滚方案

如发现严重 bug 需要回滚 Phase 6：

```bash
git revert HEAD --no-edit
git push origin main
# Railway 自动部署回滚
```

## 📊 验收清单

- [x] Phase 1: 双轨余额 + 流水 + 手动调分
- [x] Phase 2: 军需处 4 大分类 + 19 级军衔
- [x] Phase 3: 流水 + 空间统计
- [x] Phase 6: parent.html 顶栏余额 + 军衔徽章 + 军需处 Tab + 空间占用

## 🆘 故障排查

### 1. 种子数据播撒失败：`{ok: false, ...}`
**原因**：数据库表未创建
**修复**：
```bash
# Railway Shell
PYTHONPATH=src python3 -c "
from raidcaptain_sync.db.init_db import init_db
init_db()
print('✅ DB re-initialized')
"
# 然后重新触发 seed-rewards
```

### 2. parent.html 加载后无军需处 Tab
**原因**：浏览器缓存（v0.6 → v0.7 变更）
**修复**：`Ctrl+Shift+R` 强制刷新

### 3. 余额显示为 `?`
**原因**：`/api/parent/balance` 调用失败
**修复**：检查浏览器 Network 面板，确认 401 → 重新登录

## 📝 变更文件清单

```
src/raidcaptain_sync/
├── services/
│   ├── economy.py                      # + DISCIPLINE 货币 + CURRENCY_META
│   └── module_registry.py              # 修复 _open_db generator 处理
├── modules/
│   ├── parent_api.py                   # 🆕 新模块
│   ├── rank_module.py                  # 重写 + on_register 修复
│   ├── reward_module.py                # _open_db + price_currency 默认值修正
│   ├── animation_module.py             # + _open_db
│   └── storyline_module.py             # + _open_db
├── main.py                             # 注册 parent_api + sqlite3 import
└── db/init_db.py                       # + 经济字段修正迁移

static/
└── parent.html                         # 5 卡顶栏 + 军需处 Tab + 空间占用
```
