# Build context must be the REPOSITORY ROOT:
#   docker build -f services/recognition/Dockerfile -t innovision-recognition .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/recognition/requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY shared ./shared
COPY services/recognition ./services/recognition
COPY models ./models

EXPOSE 8003

CMD ["python", "-m", "services.recognition.main"]
