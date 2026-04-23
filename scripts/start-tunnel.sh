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
    # 이전 URL과 비교해서 변경됐을 때만 Vercel 동기화 (재배포 비용 절약)
    PREV_URL=""
    [ -f "$TUNNEL_URL_FILE" ] && PREV_URL=$(cat "$TUNNEL_URL_FILE")

    echo "$TUNNEL_URL" > "$TUNNEL_URL_FILE"
    echo "[$(date)] Tunnel URL: $TUNNEL_URL"
    echo "[$(date)] URL saved to: $TUNNEL_URL_FILE"

    # 헬스체크
    if curl -s "$TUNNEL_URL/health" | grep -q "ok"; then
        echo "[$(date)] Health check passed!"
    else
        echo "[$(date)] WARNING: Health check failed"
    fi

    # Vercel env 자동 동기화 (URL 변경 시 + vercel CLI 있을 때만)
    if [ "$TUNNEL_URL" != "$PREV_URL" ] && command -v vercel >/dev/null 2>&1; then
        echo "[$(date)] Vercel env 동기화 중 (이전: $PREV_URL)"
        WEB_DIR="/Users/turtlesoup0-macmini/Projects/itpe-guideline-tracker-web"
        if [ -d "$WEB_DIR" ]; then
            (
                cd "$WEB_DIR"
                vercel env rm NEXT_PUBLIC_API_URL production --yes 2>/dev/null || true
                printf "%s" "$TUNNEL_URL" | vercel env add NEXT_PUBLIC_API_URL production 2>&1 | tail -1
                # 백그라운드로 재배포 (터널 기동 블로킹 방지)
                nohup vercel --prod --cwd "$WEB_DIR" >/tmp/vercel-autodeploy.log 2>&1 &
                echo "[$(date)] Vercel 재배포 백그라운드 시작 (로그: /tmp/vercel-autodeploy.log)"
            )
        fi
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
