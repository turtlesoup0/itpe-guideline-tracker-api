FROM python:3.12-slim

WORKDIR /app

# System deps (psycopg2 build)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# App code
COPY . .

# Alembic migration on startup + uvicorn
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
