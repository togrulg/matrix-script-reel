FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
# Shell form so $PORT is expanded; 120s timeout covers FFmpeg stitching
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 600 --workers 1 main:app
