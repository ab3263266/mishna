FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Run as a non-root user.
RUN useradd --create-home --uid 10001 mishnah && chown -R mishnah /app
USER mishnah

EXPOSE 8000
# $PORT is what Render/Railway/Fly inject; 8000 is the local fallback.
# Migrate, seed reference data, then serve. All three are idempotent, so a
# restart or a redeploy is safe.
CMD ["sh", "-c", "alembic upgrade head && python -m app.db.seed --no-create && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
