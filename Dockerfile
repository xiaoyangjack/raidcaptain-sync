# RaidCaptain 同步服务端 Dockerfile
# 适用于 Railway / Zeabur / Fly.io 等容器平台
# 关键：RAID_SYNC_DIR=/data 指向平台的持久卷，数据重启不丢失

FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用 layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务端代码
COPY main.py .
COPY static/ static/

# 【M2.7】平台端口（Railway/Zeabur 自动注入）
ENV PORT=8000
# 【M2.7】持久数据目录（Railway 持久卷默认 /data，Zeabur /var/data）
ENV RAID_SYNC_DIR=/data

# 启动服务（0.0.0.0 让容器内所有网卡都监听）
CMD ["sh", "-c", "mkdir -p /data && python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]