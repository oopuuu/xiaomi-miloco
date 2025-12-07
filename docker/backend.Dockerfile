# syntax=docker/dockerfile:1.4
# Set pip index URL.
ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple


################################################
# Frontend Builder
################################################
FROM node:20-slim AS frontend-builder

WORKDIR /app
COPY web_ui/ /app/

RUN npm install
RUN npm run build


################################################
# Backend Base
################################################
FROM python:3.12-slim AS backend-base

# Restate PIP index URL.
ARG PIP_INDEX_URL

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# -------------------------------------------------------------------------
# [通用依赖安装]
# 1. ffmpeg: 核心组件，用于 RTSP 推流 (仅做 copy/remux，无需显卡驱动)
# 2. curl: MediaMTX 的 runOnDemand 钩子必须依赖它
# 3. procps: 提供 ps/top 命令，方便容器内调试
# -------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set working directory.
WORKDIR /app

# Copy app files.
COPY miloco_server/pyproject.toml /app/miloco_server/pyproject.toml
COPY miot_kit/pyproject.toml /app/miot_kit/pyproject.toml

# Install dependencies
RUN pip config set global.index-url "${PIP_INDEX_URL}" \
    && pip install --upgrade pip setuptools wheel \
    && pip install --no-build-isolation /app/miloco_server \
    && pip install --no-build-isolation /app/miot_kit \
    && rm -rf /app/miloco_server \
    && rm -rf /app/miot_kit


################################################
# Backend
################################################
FROM backend-base AS backend

# Set working directory.
WORKDIR /app

# Copy app files.
COPY miloco_server /app/miloco_server
COPY config/server_config.yaml /app/config/server_config.yaml
COPY config/prompt_config.yaml /app/config/prompt_config.yaml
COPY scripts/start_server.py /app/start_server.py
COPY miot_kit /app/miot_kit

# Install project.
RUN pip install --no-build-isolation -e /app/miloco_server \
    && pip install --no-build-isolation -e /app/miot_kit \
    && rm -rf /app/miloco_server/static \
    && rm -rf /app/miloco_server/.temp \
    && rm -rf /app/miloco_server/.log

# Update frontend dist.
COPY --from=frontend-builder /app/dist/ /app/miloco_server/static/

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://127.0.0.1:8000/api/health || exit 1

# Start application
CMD ["python3", "start_server.py"]