#!/bin/bash
# Cloudflare Quick Tunnel + FastAPI 서버 시작 스크립트
# launchd 또는 수동 실행용

set -e

PROJECT_DIR="/Users/turtlesoup0-macmini/Projects/itpe-guideline-tracker-api"
TUNNEL_LOG="/tmp/cloudflare-tunnel.log"
API_LOG="/tmp/guideline-api.log"
TUNNEL_URL_FILE="/tmp/guideline-tracker-tunnel-url.txt"

cd "$PROJECT_DIR"

# 1. FastAPI 서버 시작 (이미 실행 중이면 스킵)
if ! lsof -i :8000 -t >/dev/null 2>&1; then
    echo "[$(date)] Starting FastAPI server..."
    source .venv/bin/activate
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload > "$API_LOG" 2>&1 &
    sleep 3
    echo "[$(date)] FastAPI started (PID: $!)"
else
    echo "[$(date)] FastAPI already running on :8000"
fi

# 2. Cloudflare Tunnel 시작
echo "[$(date)] Starting Cloudflare Tunnel..."
cloudflared tunnel --url http://localhost:8000 > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
sleep 6

# 3. 터널 URL 추출
TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)

if [ -n "$TUNNEL_URL" ]; then
    echo "$TUNNEL_URL" > "$TUNNEL_URL_FILE"
    echo "[$(date)] Tunnel URL: $TUNNEL_URL"
    echo "[$(date)] URL saved to: $TUNNEL_URL_FILE"

    # 헬스체크
    if curl -s "$TUNNEL_URL/health" | grep -q "ok"; then
        echo "[$(date)] Health check passed!"
    else
        echo "[$(date)] WARNING: Health check failed"
    fi
else
    echo "[$(date)] ERROR: Could not extract tunnel URL"
    cat "$TUNNEL_LOG"
    exit 1
fi

echo "[$(date)] All services running. Tunnel PID: $TUNNEL_PID"
echo ""
echo "=== 프론트엔드 연결 ==="
echo "Vercel 환경변수에 설정:"
echo "  NEXT_PUBLIC_API_URL=$TUNNEL_URL"
echo ""
echo "터널 URL 확인: cat $TUNNEL_URL_FILE"
echo "터널 로그: tail -f $TUNNEL_LOG"
echo "API 로그: tail -f $API_LOG"

# 터널 프로세스 대기 (launchd용)
wait $TUNNEL_PID
