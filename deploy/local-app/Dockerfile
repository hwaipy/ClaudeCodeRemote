# CCR App + Claude Code 一体镜像 (本机 docker, 注册到 hub.qpqi.group 作为第二个 app).
#
# Layers:
#   1. node:20-slim (claude-code 是 nodejs cli)
#   2. python 3.12 + pip
#   3. claude-code npm install -g
#   4. ccr-app source + requirements

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
# 清华 debian 源加速 apt + 装 Node.js 20 (从 nodesource).
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git \
        && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
        && apt-get install -y --no-install-recommends nodejs \
        && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (官方 npm package). 国内 npmmirror + retry.
RUN npm config set registry https://registry.npmmirror.com \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm install -g @anthropic-ai/claude-code

# CCR app deps. 国内 pip 走 tuna mirror.
WORKDIR /app
COPY requirements.txt /app/requirements.txt
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
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
CMD ["python3", "-m", "uvicorn", \
     "claude_code_remote.server.main:app", \
     "--host", "0.0.0.0", "--port", "1884", "--log-level", "info"]
