FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY templates/ templates/

RUN mkdir -p /data

# Uvicorn log level is driven by UVICORN_LOG_LEVEL so the ./run wrapper can
# bump it to "debug" for verbose runs without rebuilding the image.
CMD ["sh", "-c", "exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --log-level ${UVICORN_LOG_LEVEL:-info}"]
