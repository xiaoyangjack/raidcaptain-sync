# ── 多阶段构建：缩小镜像体积 ──────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装编译依赖（用于 oss2 等带 C 扩展的包）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── 运行时镜像 ─────────────────────────────────────────────
FROM python:3.11-slim

# 时区 / 健康检查工具
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户运行（安全）
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app \
    && mkdir -p /app /data \
    && chown -R app:app /app /data

WORKDIR /app

# 复制已编译的依赖
COPY --from=builder /install /usr/local

# 复制应用代码（src/raidcaptain_sync/* → /app/raidcaptain_sync/*）
# 关键：PYTHONPATH=/app 使 `uvicorn raidcaptain_sync.main:app` 能找到模块
COPY --chown=app:app src/ /app/
COPY --chown=app:app static/ /app/static/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

USER app

# 数据持久化卷
VOLUME ["/data"]

# 健康检查（容器编排探针）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

# Railway / Zeabur / 自建 ECS 都支持 ${PORT} 环境变量
ENV RAID_SYNC_DIR=/data \
    RAID_SYNC_DB=/data/sync.db \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn raidcaptain_sync.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]