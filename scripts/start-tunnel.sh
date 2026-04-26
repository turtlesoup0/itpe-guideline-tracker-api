#!/bin/bash
# Cloudflare Named Tunnel + FastAPI 서버 시작 스크립트.
# 고정 도메인 (api.tech-insight.org) 사용 — 재시작해도 URL 불변.
# launchd 또는 수동 실행용.

set -e

PROJECT_DIR="/Users/turtlesoup0-macmini/Projects/itpe-guideline-tracker-api"
TUNNEL_NAME="guideline-tracker-api"
API_LOG="/tmp/guideline-api.log"
TUNNEL_LOG="/tmp/cloudflare-tunnel.log"

cd "$PROJECT_DIR"

# 1. FastAPI 서버 시작 (이미 실행 중이면 스킵)
if ! lsof -i :8001 -t >/dev/null 2>&1; then
    echo "[$(date)] Starting FastAPI server..."
    source .venv/bin/activate
    uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload > "$API_LOG" 2>&1 &
    sleep 3
    echo "[$(date)] FastAPI started (PID: $!)"
else
    echo "[$(date)] FastAPI already running on :8001"
fi

# 2. Cloudflare Named Tunnel 실행 (config: ~/.cloudflared/config.yml)
echo "[$(date)] Starting Cloudflare Named Tunnel: $TUNNEL_NAME"
cloudflared tunnel run "$TUNNEL_NAME" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
sleep 4

# 3. 헬스체크 (public URL로)
PUBLIC_URL="https://api.tech-insight.org"
if curl -s --max-time 15 "$PUBLIC_URL/health" | grep -q "ok"; then
    echo "[$(date)] Named Tunnel health OK: $PUBLIC_URL"
else
    echo "[$(date)] WARNING: Named Tunnel 헬스체크 실패 (DNS 전파 대기 중일 수 있음)"
fi

echo "[$(date)] All services running. Tunnel PID: $TUNNEL_PID"
echo "Public URL: $PUBLIC_URL"
echo "Tunnel log: tail -f $TUNNEL_LOG"
echo "API log:    tail -f $API_LOG"

# 터널 프로세스 대기 (launchd용)
wait $TUNNEL_PID
