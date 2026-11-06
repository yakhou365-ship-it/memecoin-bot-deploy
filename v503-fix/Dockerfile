FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY frontend/dist/ ./frontend/dist/
# V505: create BOTH the legacy data dir (local-dev default) and the /app/data
# absolute path that the Railway Volume mount will overlay. The Volume itself
# is attached at deploy time by the operator (see README-V505.md).
RUN mkdir -p /app/backend/data /app/data /app/data/models
WORKDIR /app/backend
ENV PYTHONPATH=/app/backend
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
# V505: don't hard-code DATABASE_URL — let config.py auto-detect the
# Railway Volume path (RAILWAY_VOLUME_MOUNT_PATH env var injected by Railway).
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
