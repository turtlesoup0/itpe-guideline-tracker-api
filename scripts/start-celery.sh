#!/bin/bash
# Celery Worker + Beat 시작 스크립트 (launchd용).
#
# Worker와 Beat를 백그라운드로 띄우고, 둘 중 하나라도 죽으면 스크립트 종료.
# launchd가 KeepAlive로 재시작해 줌.

set -e

PROJECT_DIR="/Users/turtlesoup0-macmini/Projects/itpe-guideline-tracker-api"
WORKER_LOG="/tmp/celery-worker.log"
BEAT_LOG="/tmp/celery-beat.log"

cd "$PROJECT_DIR"
source .venv/bin/activate

# 의존성 확인: Redis 포트가 열려 있어야 함
if ! nc -z localhost 6379 2>/dev/null; then
    echo "[$(date)] ERROR: Redis(:6379) 연결 불가 — SSH 터널 또는 Redis 서버 확인 필요"
    exit 1
fi

echo "[$(date)] Celery Worker 시작 (concurrency=1, loglevel=info)"
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    --concurrency=1 \
    --logfile="$WORKER_LOG" &
WORKER_PID=$!

echo "[$(date)] Celery Beat 시작 (timezone=Asia/Seoul)"
celery -A app.tasks.celery_app beat \
    --loglevel=info \
    --logfile="$BEAT_LOG" \
    --pidfile=/tmp/celery-beat.pid \
    --schedule=/tmp/celerybeat-schedule &
BEAT_PID=$!

echo "[$(date)] Worker PID=$WORKER_PID, Beat PID=$BEAT_PID"

# 둘 중 하나라도 죽으면 전체 종료 (launchd가 재시작)
trap 'echo "[$(date)] SIGTERM — 프로세스 종료"; kill $WORKER_PID $BEAT_PID 2>/dev/null; exit 0' TERM INT

# bash 3.2 호환 polling loop — 5초마다 두 프로세스 살아있는지 확인
while kill -0 $WORKER_PID 2>/dev/null && kill -0 $BEAT_PID 2>/dev/null; do
    sleep 5
done

echo "[$(date)] Celery 프로세스 하나 종료 감지. 남은 프로세스도 종료."
kill $WORKER_PID $BEAT_PID 2>/dev/null
wait 2>/dev/null

exit 1
