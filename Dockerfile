# RaidCaptain Sync Server - 单阶段构建
FROM python:3.11-slim

# 时区 / 工具
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装依赖（gcc + libffi-dev 让 oss2 / cryptography 能编译）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY src/ /app/
COPY static/ /app/static/

# 数据目录
RUN mkdir -p /data && chmod 777 /data

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

ENV PYTHONPATH=/app \
    RAID_SYNC_DIR=/data \
    RAID_SYNC_DB=/data/sync.db \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "raidcaptain_sync.main:app", "--host", "0.0.0.0", "--port", "8000"]
