# CCR App + Claude Code 一体镜像 (本机 docker, 注册到 hub.qpqi.group 作为第二个 app).
#
# Layers:
#   1. node:20-slim (claude-code 是 nodejs cli)
#   2. python 3.12 + pip
#   3. claude-code npm install -g
#   4. ccr-app source + requirements

FROM node:20-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git curl ca-certificates \
        && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (官方 npm package). 网络敏感: timeout + retry.
RUN npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm install -g @anthropic-ai/claude-code

# CCR app deps (装 python deps). 国内 pip 走 tuna mirror.
WORKDIR /app
COPY requirements.txt /app/requirements.txt
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN python3 -m venv /opt/ccr-venv \
    && /opt/ccr-venv/bin/pip install --no-cache-dir --timeout 120 --retries 5 \
        -i ${PIP_INDEX} -r /app/requirements.txt

# CCR app 源码 (build context root = ClaudeCodeRemote/)
COPY claude_code_remote /app/claude_code_remote

# 数据目录持久化点 (db + cwd workspace)
ENV CCR_DB_PATH=/data/ccr.sqlite \
    CCR_DEFAULT_CWD=/workspace \
    CCR_HOST=0.0.0.0 \
    CCR_PORT=1884 \
    CCR_CLAUDE_BIN=claude
RUN mkdir -p /data /workspace

EXPOSE 1884

# Token 通过 env / pair flow 拿; CCR_HUB_URL + CCR_HUB_DEVICE_TOKEN +
# CCR_HUB_APP_NAME 由 docker-compose 注入.
CMD ["/opt/ccr-venv/bin/python3", "-m", "uvicorn", \
     "claude_code_remote.server.main:app", \
     "--host", "0.0.0.0", "--port", "1884", "--log-level", "info"]
