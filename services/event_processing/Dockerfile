FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY services/event_processing/requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY services/event_processing/src ./src
COPY services/event_processing/main.py ./main.py
COPY shared ./shared

ENV PYTHONPATH=/app

CMD ["python", "main.py"]