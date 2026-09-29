# Build context must be the REPOSITORY ROOT:
#   docker build -f services/event_processing/Dockerfile -t innovision-event-processing .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY services/event_processing/requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY shared ./shared
COPY services/event_processing ./services/event_processing

CMD ["python", "-m", "services.event_processing.main"]
