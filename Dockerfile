FROM python:3.12-slim

# Non-root user.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (includes app/templates and app/static for the dashboard).
COPY app/ ./app/

# Dashboard web (LAN only, port 8080). Not exposed to internet — access via the
# NAS VPN/Tailscale. Disabled unless DASHBOARD_PASSWORD is set.
EXPOSE 8080

# Data volume mount point.
RUN mkdir -p /data && chown -R appuser:appuser /data /app
VOLUME ["/data"]

ENV DB_PATH=/data/prices.db \
    PYTHONUNBUFFERED=1

USER appuser

HEALTHCHECK --interval=5m --timeout=10s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]
