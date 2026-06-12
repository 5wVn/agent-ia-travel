FROM python:3.12-slim

# Non-root user.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app/ ./app/

# Data volume mount point.
RUN mkdir -p /data && chown -R appuser:appuser /data /app
VOLUME ["/data"]

ENV DB_PATH=/data/prices.db \
    PYTHONUNBUFFERED=1

USER appuser

HEALTHCHECK --interval=5m --timeout=10s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]
