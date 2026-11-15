# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CASHVIBE_DB_PATH=/app/data/cashvibe.db \
    HOME=/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    groupadd --gid 1000 cashvibe && \
    useradd --uid 1000 --gid cashvibe --no-create-home --home-dir /app cashvibe && \
    mkdir -p /app/data && chown cashvibe:cashvibe /app /app/data

COPY --chown=cashvibe:cashvibe . .
USER cashvibe
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
