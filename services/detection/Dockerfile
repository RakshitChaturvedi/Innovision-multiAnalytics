FROM python:3.12-slim

# Prevent Python from creating .pyc files
# and make logs appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ------------------------------------------------------------
# System dependencies
# ------------------------------------------------------------

RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libgl1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Working directory
# ------------------------------------------------------------

WORKDIR /app

# ------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------
# Application
# ------------------------------------------------------------

COPY src ./src
COPY main.py .

# ------------------------------------------------------------
# YOLO model directory
# ------------------------------------------------------------

RUN mkdir -p /models/detection

# ------------------------------------------------------------
# Python path
# ------------------------------------------------------------

ENV PYTHONPATH=/app

# ------------------------------------------------------------
# Start detection service
# ------------------------------------------------------------

CMD ["python", "main.py"]