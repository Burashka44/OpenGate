FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_BIND_HOST=0.0.0.0

# Системные зависимости для pillow/qrcode
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Секреты не в образе: config.py.example → config.py, значения из env (BOT_TOKEN/ADMIN_IDS)
RUN cp config.py.example config.py \
    && mkdir -p logs database backup \
    && useradd -r -u 10001 -d /app -s /usr/sbin/nologin opengate \
    && chown -R opengate:opengate /app

USER opengate

EXPOSE 8081

# Heartbeat пишет main.py; если файла нет / устарел — контейнер unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import time,pathlib; p=pathlib.Path('logs/heartbeat'); assert p.exists() and time.time()-p.stat().st_mtime<90"

CMD ["python", "main.py"]
