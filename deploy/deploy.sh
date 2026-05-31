#!/usr/bin/env bash
# ============================================================
# CCR 一键部署脚本 — spec §18
#
# 用法:
#   deploy/deploy.sh                  # 完整部署 (本地 + OfficeMac + DSM hub)
#   deploy/deploy.sh --skip-remote    # 只部署本地 ustc / kimi, 跳过远程
#   deploy/deploy.sh --only-local     # 等同 --skip-remote
#
# 行为:
#   按顺序每台做完再做下一台, 每台等 healthz / systemctl is-active 确认.
#   SSH 不通的机器自动跳过 (不影响整体的最终结果).
#   最终输出全局状态 DONE:0 / DONE:1, 让 agent 一眼判.
# ============================================================

set -euo pipefail

PROJECT="$HOME/codes/ClaudeCodeRemoteAutoTest/ClaudeCodeRemote"
SERVE_DIR="$PROJECT/claude_code_remote/server/static"
HUB_STATIC_DIR="$PROJECT/claude_code_remote/server/static"
SYNOLOGY_DIR="$HOME/SynologyDrive/Claude"

SKIP_REMOTE=0
for arg in "$@"; do
    case "$arg" in
        --skip-remote|--only-local|-l) SKIP_REMOTE=1 ;;
    esac
done

branch=$(cd "$PROJECT" && git rev-parse --abbrev-ref HEAD)
echo "[deploy] branch=$branch $(date '+%H:%M:%S')"
echo "------------------------------------------------"

# ---------- 0. 在 git 上标记大概花了什么 ----------
cd "$PROJECT"
if [ -n "$(git status --porcelain)" ]; then
    echo "[deploy] uncommitted changes, skipping git meta check"
fi

# ---------- 1. 本地 app 端 -----------------------
echo ""
echo "[1/3] Local servers (ustc + kimi)..."
cp "$SERVE_DIR"/{app.js,sw.js,style.css,index.html} "$SYNOLOGY_DIR"/ 2>/dev/null || true

for svc in ccr-ustc ccr-kimi; do
    echo -n "  restarting $svc ... "
    systemctl --user restart "$svc" 2>&1 || { echo "FAIL:$svc restart error $?"; exit 1; }
    # 轮询 healthz (max 15 tries)
    port=""
    case "$svc" in
        ccr-ustc) port=1886 ;;
        ccr-kimi) port=1887 ;;
    esac
    ok=0
    for i in $(seq 15); do
        st=$(systemctl --user is-active "$svc" 2>&1) || true
        if [ "$st" = "active" ]; then
            # uvicorn 可能 active 但还没 ready — 走 healthz
            hz=$(curl -sf "http://127.0.0.1:$port/healthz" 2>&1 || echo "")
            if [ "$hz" = '{"status":"ok"}' ]; then
                ok=1; break
            fi
        fi
        sleep 1
    done
    if [ "$ok" = "1" ]; then
        echo "OK"
    else
        echo "FAIL:$svc not healthy after 15s"
        echo "  systemctl status $svc:"
        systemctl --user status "$svc" --no-pager 2>&1 | head -6
        exit 1
    fi
done

# ---------- 2. OfficeMac --------------------------
echo ""
echo "[2/3] OfficeMac..."
if [ "$SKIP_REMOTE" = "1" ]; then
    echo "  --skip-remote, pass"
else
    if ssh -o ConnectTimeout=5 OfficeMac echo ok 2>/dev/null; then
        echo "  rsync server files ..."
        rsync -av "$PROJECT/claude_code_remote/server"/{api.py,db.py,main.py,hub_client.py,\
            static/app.js,static/sw.js,static/style.css,static/index.html} \
            OfficeMac:~/.venv/ccr/lib/python3.12/site-packages/claude_code_remote/server/ 2>&1 | tail -3
        rsync -av "$PROJECT/claude_code_remote/mcp/ask_user_server.py" \
            OfficeMac:~/.venv/ccr/lib/python3.12/site-packages/claude_code_remote/mcp/ 2>&1 | tail -1
        echo "  launchctl restart ..."
        ssh OfficeMac 'launchctl unload ~/Library/LaunchAgents/com.hwaipy.ccr.plist; \
                       launchctl load -w ~/Library/LaunchAgents/com.hwaipy.ccr.plist' 2>&1 | tail -2
        sleep 3
        # 等 hub_client ready — tail 日志看有没有 "hub_client ready"
        ssh OfficeMac 'tail -20 ~/Library/Logs/ccr.log 2>/dev/null' | grep "hub_client ready" | tail -1 && echo "  OfficeMac OK" \
            || echo "  WARN: could not confirm hub_client ready (check ~/Library/Logs/ccr.log)"
    else
        echo "  SSH unreachable, skip"
    fi
fi

# ---------- 3. DSM hub ---------------------------
echo ""
echo "[3/3] DSM hub..."
if [ "$SKIP_REMOTE" = "1" ]; then
    echo "  --skip-remote, pass"
else
    if ssh -o ConnectTimeout=5 DSM echo ok 2>/dev/null; then
        echo "  rsync code ..."
        rsync -av "$PROJECT/claude_code_remote"/hub/{db.py,forwarder.py,api.py,tunnel.py} \
            "$PROJECT/claude_code_remote"/server/{api.py,main.py,hub_client.py,static/app.js,static/sw.js,static/style.css,static/index.html} \
            DSM:/home/ubuntu/dockers/ClaudeCodeRemote/claude_code_remote/ --relative 2>&1 | tail -5
        echo "  docker-compose build + up ..."
        ssh DSM 'cd /home/ubuntu/dockers/ClaudeCodeRemote/deploy && \
           docker-compose -f docker-compose.hub.yml up -d --build ccr-hub' 2>&1 | tail -3
        sleep 4
        hz=$(curl -sf https://vibe.qpqi.group/healthz 2>&1 || echo "")
        if [ "$hz" = '{"status":"ok"}' ]; then
            # 校验 hub 的 SW 版本
            sw=$(curl -sf https://vibe.qpqi.group/static/sw.js 2>&1 | grep "CACHE" | head -1 | grep -oP '"[^"]+"')
            echo "  hub healthz OK, SW=$sw"
        else
            echo "  WARN: hub healthz not ok ($hz), check DSM manually"
        fi
    else
        echo "  SSH unreachable, skip"
    fi
fi

# ---------- 汇总 ---------------------------
echo ""
echo "================================================"
echo "deploy SUMMARY"
echo "  local   ustc  kimi : DONE"
if [ "$SKIP_REMOTE" = "0" ]; then
    echo "  remote OfficeMac   : see log"
    echo "  remote DSM hub     : see log"
fi
echo "================================================"
echo "[deploy] DONE:0 at $(date '+%H:%M:%S')"