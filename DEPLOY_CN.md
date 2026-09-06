# RaidCaptain Sync Server - 国内云服务器部署指南

> 本文档面向中国大陆用户，涵盖阿里云 ECS、腾讯云轻量应用服务器的完整部署流程。

---

## 📋 目录

1. [方案选型](#1-方案选型)
2. [阿里云 ECS 部署](#2-阿里云-ecs-部署)
3. [腾讯云轻量应用服务器部署](#3-腾讯云轻量应用服务器部署)
4. [域名 + HTTPS](#4-域名--https)
5. [OSS 对象存储配置](#5-oss-对象存储配置)
6. [数据备份策略](#6-数据备份策略)
7. [运维监控](#7-运维监控)
8. [故障排查](#8-故障排查)
9. [扩展路线图](#9-扩展路线图)

---

## 1. 方案选型

### 轻量方案（推荐，开发测试阶段）

| 项目 | 配置 |
|------|------|
| **规格** | 2核 2GB / 50GB SSD |
| **典型价格** | 阿里云 ¥60/月，腾讯云 ¥60/月 |
| **并发** | ~200 户家庭 |
| **适用** | 初期 < 500 户 |

### 稳定方案（生产环境）

| 项目 | 配置 |
|------|------|
| **规格** | 2核 4GB / 100GB SSD |
| **典型价格** | 阿里云 ¥120/月，腾讯云 ¥100/月 |
| **并发** | ~1000 户家庭 |
| **适用** | 500 ~ 2000 户 |

### OSS 费用估算

| 存储量 | 存储费 | 流量费（估） | 合计/月 |
|--------|--------|-------------|---------|
| 10GB | ¥0.12 | ¥5 | **¥5** |
| 50GB | ¥0.60 | ¥25 | **¥25** |
| 100GB | ¥1.20 | ¥50 | **¥51** |

> 估算基于华东 1 区标准存储，流量按月均 500MB/家庭计算。

---

## 2. 阿里云 ECS 部署

### 2.1 创建实例

1. 登录 [阿里云 ECS 控制台](https://ecs.console.aliyun.com)
2. 选择 **实例 → 创建实例**
3. 配置：
   - **地域**：华东 1（杭州）/ 华北 2（北京）均可
   - **镜像**：Ubuntu 22.04 LTS 64位
   - **规格**：2核 2GB 起步
   - **存储**：40GB SSD
   - **带宽**：按量付费 10Mbps 或固定 5Mbps
4. 设置 root 密码，**勾选"创建后分配公网 IP"**
5. 安全组放行：`TCP 8000`（服务）、`TCP 80`（可选 Nginx）、`TCP 443`（HTTPS）

### 2.2 SSH 登录

```bash
ssh root@<你的公网IP>
# 首次登录后立即更新系统
apt update && apt upgrade -y
```

### 2.3 安装 Docker

```bash
# 一键安装 Docker
curl -fsSL https://get.docker.com | sh

# 启用 Docker 服务
systemctl enable docker
systemctl start docker

# 验证
docker --version  # 预期：Docker version 26.x.x
```

### 2.4 部署应用

```bash
# 创建数据目录
mkdir -p /data/raidcaptain
chmod 777 /data/raidcaptain

# 创建 .env 配置文件
cat > /data/raidcaptain/.env << 'EOF'
RAID_SYNC_DIR=/data
RAID_SYNC_DB=/data/sync.db
RAID_HOST=0.0.0.0
RAID_PORT=8000
RAID_LOG_LEVEL=INFO

# OSS 配置（从阿里云控制台获取）
RAID_OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
RAID_OSS_BUCKET=raidcaptain-evidence
RAID_OSS_ACCESS_KEY_ID=<你的AccessKey ID>
RAID_OSS_ACCESS_KEY_SECRET=<你的AccessKey Secret>
RAID_OSS_REGION=cn-hangzhou
EOF

chmod 600 /data/raidcaptain/.env

# 上传项目文件（本地执行）
# 先在本地打包：
#   cd /Users/mac/Downloads/RaidCaptain_Server
#   tar --exclude='*.pyc' --exclude='__pycache__' \
#       --exclude='.git' -czf raidcaptain.tar.gz .
# 然后用 scp 上传：
scp raidcaptain.tar.gz root@<你的公网IP>:/tmp/

# 在服务器解压
cd /data/raidcaptain
tar -xzf /tmp/raidcaptain.tar.gz
```

### 2.5 启动服务

```bash
cd /data/raidcaptain

# 拉取 Python 基础镜像并构建
docker compose build

# 后台启动
docker compose up -d

# 查看日志
docker compose logs -f

# 查看健康状态
curl http://localhost:8000/health
# 预期：{"status":"ok","db":true,"oss":false,"version":"3.0.0"}
```

### 2.6 进程守护（systemd，防止重启后未启动）

```bash
cat > /etc/systemd/system/raidcaptain.service << 'EOF'
[Unit]
Description=RaidCaptain Sync Server
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/data/raidcaptain
ExecStartPre=/usr/bin/docker compose -f /data/raidcaptain/docker-compose.yml build
ExecStart=/usr/bin/docker compose -f /data/raidcaptain/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose -f /data/raidcaptain/docker-compose.yml down
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable raidcaptain
systemctl start raidcaptain

# 验证
systemctl status raidcaptain
curl http://localhost:8000/health
```

---

## 3. 腾讯云轻量应用服务器部署

> 腾讯云轻量应用服务器更适合个人开发者，性价比更高。

### 3.1 创建实例

1. 登录 [腾讯云控制台](https://console.cloud.tencent.com/lighthouse)
2. **创建实例**
3. 配置：
   - **地域**：上海/广州
   - **镜像**：Ubuntu 22.04 LTS
   - **规格**：2核 2GB
   - **系统盘**：50GB SSD
   - **带宽**：5Mbps 按量计费
4. **放行端口**：防火墙放行 `8000/tcp`
5. 设置密码，购买

### 3.2 部署步骤

与阿里云相同，参考 2.3 ~ 2.6 节。

> **腾讯云对象存储（COS）备选方案：**
> 如果使用腾讯云 COS 代替阿里云 OSS，需修改 `oss_storage.py` 中的 SDK 为 `cos-python-sdk-v5`。

---

## 4. 域名 + HTTPS

### 4.1 购买域名

- 阿里云万网 / 腾讯云 DNSPod
- 推荐 `.cn` 或 `.com`，首年 ¥20-35

### 4.2 申请免费 SSL 证书

**方式 A：Let's Encrypt（推荐）**

```bash
# 安装 certbot
apt install -y certbot python3-certbot-nginx

# 申请证书（需要域名已解析到服务器 IP）
certbot certonly --nginx -d api.yourdomain.com

# 证书自动续期
certbot renew --dry-run
```

**方式 B：阿里云 DV 证书（免费）**

1. 阿里云控制台 → SSL 证书 → 免费证书
2. 下载 Nginx 格式证书
3. 上传到服务器 `/etc/nginx/certs/`

### 4.3 Nginx 反向代理配置

```bash
apt install -y nginx

cat > /etc/nginx/sites-available/raidcaptain << 'EOF'
server {
    listen 80;
    server_name api.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    # SSL 优化
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;
    add_header Strict-Transport-Security "max-age=63072000" always;

    client_max_body_size 20M;  # 照片上传大小限制

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
EOF

ln -sf /etc/nginx/sites-available/raidcaptain /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
systemctl enable nginx
```

---

## 5. OSS 对象存储配置

### 5.1 阿里云 OSS

1. 登录 [阿里云 OSS 控制台](https://oss.console.aliyun.com)
2. **创建 Bucket**：
   - 名称：`raidcaptain-evidence`（全局唯一）
   - 地域：与 ECS 同一地域（节省流量费）
   - 存储类型：标准存储
   - 读写权限：**私有**（重要！）
3. **获取 AccessKey**：
   - RAM 控制台 → 用户 → 创建用户（Programmatic Access）
   - 授权 `AliyunOSSFullAccess`（或精细化策略）
4. **设置生命周期**（自动清理旧文件）：
   - 控制台 → Bucket → 基础设置 → 生命周期
   - 规则：照片超过 90 天后转为低频访问，180 天后删除
5. **配置 CORS**（如需前端直传）：
   - 允许来源：`*`
   - 允许方法：GET, POST
   - 允许头：`*`

### 5.2 OSS 访问凭证（安全最佳实践）

**推荐：使用 RAM 角色而非 AccessKey Secret**

```python
# oss_storage.py 中的备选初始化方式
import oss2
from aliyunsdkcore.auth.credentials import AccessKeyCredential, StsTokenCredential
from aliyunsdkcore.request import CommonRequest

# 使用 STS Token（临时凭证，90 分钟过期）
auth = oss2.StsAuth(...)
bucket = oss2.Bucket(auth, endpoint, bucket_name)
```

### 5.3 OSS 费用优化

| 策略 | 节省比例 |
|------|---------|
| 与 ECS 同地域（同 VPC 内传不收费） | ~50% |
| 开启 OSS 传输加速 | +10% 流量，但体验更好 |
| 照片压缩（上传前在 App 端压缩到 1MB 以内） | ~70% 存储/流量 |
| 设置合理的生命周期规则 | ~30% 存储费 |

---

## 6. 数据备份策略

### 6.1 本地定时备份

```bash
# 添加 crontab 每天凌晨 3 点备份
crontab -e

# 添加以下行：
0 3 * * * /data/raidcaptain/scripts/backup_db.sh >> /var/log/backup.log 2>&1
```

### 6.2 OSS 跨区域复制（灾备）

在 OSS 控制台开启**跨区域复制**：
- 源 Bucket：华东 1
- 目标 Bucket：华北 2
- 复制粒度：增量和全量
- 费用：¥0.75/GB（只收流量费，存储免费）

### 6.3 备份恢复测试

```bash
# 定期演练：下载备份文件到临时目录，验证完整性
cd /tmp
sqlite3 test_restore.db ".tables"
# 如果报错则备份文件损坏，需排查
```

---

## 7. 运维监控

### 7.1 基础监控

**阿里云云监控（免费）：**
- 控制台 → 云监控 → 主机监控
- 添加 ECS 实例
- 监控指标：CPU > 80%、内存 > 85%、磁盘 > 90%、进程存活

**告警规则：**
- CPU 使用率 > 85% 持续 5 分钟
- 内存使用率 > 90% 持续 5 分钟
- `/health` 端点连续 3 次返回非 200

### 7.2 日志收集

```bash
# Docker 容器日志重定向到文件（方便日志收集）
docker compose logs -f app --tail 100 > /var/log/raidcaptain.log

# 使用 logrotate 自动轮转
cat > /etc/logrotate.d/raidcaptain << 'EOF'
/var/log/raidcaptain.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root adm
    postrotate
        docker compose -f /data/raidcaptain/docker-compose.yml kill -s USR1 app
    endscript
}
EOF
```

### 7.3 进阶监控（可选）

**Prometheus + Grafana 方案：**

1. 在 Grafana Cloud 免费注册
2. 在服务器安装 `node_exporter`
3. 在 Grafana 添加 Prometheus 数据源
4. 导入预制面板（Docker Container Stats）

---

## 8. 故障排查

### 8.1 服务无法启动

```bash
# 查看详细日志
docker compose logs --tail 100 app

# 常见原因：
# 1. 端口被占用
netstat -tlnp | grep 8000
# 解决方案：修改 .env 中 RAID_PORT=8001

# 2. 数据库权限问题
ls -la /data/sync.db
# 解决方案：chmod 666 /data/sync.db

# 3. OSS 配置错误（暂时降级）
# 注释掉 .env 中的 OSS 相关配置，验证基础功能
```

### 8.2 数据库损坏

```bash
# 检查 WAL 模式
sqlite3 /data/sync.db "PRAGMA journal_mode;"

# 如果是 DELETE 模式，改为 WAL
sqlite3 /data/sync.db "PRAGMA journal_mode=WAL;"

# 完整性检查
sqlite3 /data/sync.db "PRAGMA integrity_check;"

# 如需从备份恢复
systemctl stop raidcaptain
cp /tmp/sync_20260906.db.gz /data/sync.db
gzip -d /data/sync.db
chmod 666 /data/sync.db
systemctl start raidcaptain
```

### 8.3 OSS 上传失败

```bash
# 测试 OSS 连通性
python3 -c "
import oss2
auth = oss2.Auth('<你的KeyId>', '<你的KeySecret>')
bucket = oss2.Bucket(auth, 'https://oss-cn-hangzhou.aliyuncs.com', 'raidcaptain-evidence')
bucket.put_object('test.txt', b'ok')
print('OSS 连通正常')
"

# 检查策略权限
# RAM 控制台 → 权限策略 → 查看 AliyunOSSFullAccess
# 确保包含 oss:PutObject 动作
```

### 8.4 WebSocket 连接失败

```bash
# 检查 Nginx WebSocket 支持（必须）
grep -A5 "proxy_http_version" /etc/nginx/sites-available/raidcaptain
# 必须有：proxy_http_version 1.1 和 proxy_set_header Upgrade $http_upgrade

# 检查防火墙
ufw status
# 确保 8000 或 443 端口开放
```

---

## 9. 扩展路线图

### 短期（1-3 个月，< 500 户）

- [x] 单机 SQLite + OSS（M3 当前状态）
- [ ] 添加 `/api/v2/refresh-token` 端点（短期 Token 刷新）
- [ ] 限流中间件（slowapi）防止滥用
- [ ] 照片上传前 App 端压缩（节省 70% OSS 流量）

### 中期（3-12 个月，500-2000 户）

- [ ] PostgreSQL 迁移（支持多实例）
- [ ] Redis Pub/Sub（WebSocket 多实例广播）
- [ ] Redis 缓存热点数据（任务列表/申诉列表）
- [ ] OSS CDN 加速（全国访问优化）

### 长期（> 1 年，> 2000 户）

- [ ] 读写分离（PostgreSQL 主从）
- [ ] 按家庭分库（分表分库中间件）
- [ ] 微服务拆分（鉴权服务 / 任务服务 / 证据服务）
- [ ] 分布式对象存储（阿里云 OSS 多副本）
- [ ] 全链路压测 + 自动扩缩容

---

## 附录：快速命令速查

```bash
# 启动
systemctl start raidcaptain

# 停止
systemctl stop raidcaptain

# 重启
systemctl restart raidcaptain

# 查看状态
systemctl status raidcaptain

# 查看日志
journalctl -u raidcaptain -f

# 健康检查
curl http://localhost:8000/health | jq

# 进入容器
docker exec -it raidcaptain-app-1 /bin/sh

# 手动备份
/data/raidcaptain/scripts/backup_db.sh

# 更新代码后重建
cd /data/raidcaptain
docker compose down
docker compose pull
docker compose up -d --build
```
