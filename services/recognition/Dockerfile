FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY shared/ ./shared/
RUN pip install -e ./shared/schemas \
    -e ./shared/audit \
    -e ./shared/storage \
    --no-cache-dir

COPY services/recognition/requirements.txt requirements.txt
RUN pip install -r requirements.txt --no-cache-dir

COPY services/recognition/ ./services/recognition/
COPY models/ ./models/

EXPOSE 8003

CMD ["python", "-m", "services.recognition.main"]
