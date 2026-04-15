web: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: celery -A app.tasks.celery_app worker --loglevel=info --concurrency=1
beat: celery -A app.tasks.celery_app beat --loglevel=info
