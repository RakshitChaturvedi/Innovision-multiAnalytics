# NOTE: build context must be the REPOSITORY ROOT, not services/detection:
#   docker build -f services/detection/Dockerfile -t innovision-detection .
#
# FIX: the previous Dockerfile only copied `src` and `main.py`. Every
# module imports `shared.*`, so the image built successfully and then
# crashed with ModuleNotFoundError on first start.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/detection/requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Application code
COPY services/detection/src ./src
COPY services/detection/main.py ./main.py

# Shared library — required, previously missing
COPY shared ./shared

RUN mkdir -p /models/detection \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /models

USER appuser

ENV PYTHONPATH=/app \
    YOLO_CONFIG_DIR=/tmp/ultralytics

CMD ["python", "main.py"]
