# Hosted prototype image (Hugging Face Spaces, Docker SDK). Also runs anywhere:
#   docker build -t invoice-decisioning . && docker run -p 7860:7860 -e GEMINI_API_KEY=... invoice-decisioning
FROM python:3.12-slim

# tesseract for the scanner path; libglib for OpenCV's headless build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000; give it a home and own the app dir.
RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . .
USER user

ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    LLM_CACHE_MODE=cached
EXPOSE 7860

# run.sh migrates and seeds the database, checks the samples, then serves one worker.
CMD ["bash", "run.sh"]
