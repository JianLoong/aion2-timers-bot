# The bot only needs the three runtime deps; tests and the venv stay out of the image.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY bot.py ./
COPY aion2_timerbot ./aion2_timerbot

# Timers live in /app/data, which compose mounts from ./data on your machine.
RUN useradd --create-home --uid 1000 timer \
    && mkdir -p /app/data \
    && chown -R timer:timer /app/data
USER timer

CMD ["python", "bot.py"]
