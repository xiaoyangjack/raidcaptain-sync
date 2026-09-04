# Zeabur 部署用 Dockerfile
# 用途：把 RaidCaptain 同步服务端打包成容器，跑在 Zeabur 的节点上
# 用法：在 Zeabur 项目里选 "Deploy from Dockerfile"，上传本文件即可

FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用 layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务端代码
COPY main.py .
COPY static/ static/

# Zeabur 通过 PORT 环境变量指定端口，服务端读取它
ENV PORT=8000

# 启动服务（0.0.0.0 让容器内所有网卡都监听）
CMD ["sh", "-c", "python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]