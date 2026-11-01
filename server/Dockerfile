FROM python:3.12-slim

# EasyOCR needs these; opencv-python-headless still wants libgl on some bases.
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Warm the EasyOCR model into the image so the first scan of the day is not
# the slow one. Without this the operator who starts the shift waits ~30s.
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False)" || true

COPY app ./app
COPY scripts ./scripts
COPY templates ./templates

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
