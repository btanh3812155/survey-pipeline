FROM python:3.12-slim

WORKDIR /app

# install system dependencies for opencv and cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libvips42 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
ENV FORWARDED_ALLOW_IPS=127.0.0.1,::1,caddy WEB_CONCURRENCY=2
CMD ["python", "-m", "core.server"]
