# worker/Dockerfile
# Production-ready Celery worker with Ollama client

FROM python:3.12-slim

WORKDIR /app

# System dependencies for newspaper3k and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama client (binary for health checks, though we use Python SDK)
RUN curl -L -o /tmp/ollama.deb https://ollama.ai/download/ollama-linux-amd64.deb && \
    dpkg -i /tmp/ollama.deb || apt-get -f install -y && \
    rm -f /tmp/ollama.deb

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt celery[redis] \
    newspaper3k \
    lxml[html_clean] \
    feedparser

# Application code
COPY modules/ ./modules/
COPY worker/ ./worker/
COPY metrq_dj/core/ ./metrq_dj/core/
COPY metrq_dj/metriq_site/__init__.py ./metrq_dj/metriq_site/
COPY metrq_dj/metriq_site/settings.py ./metrq_dj/metriq_site/
COPY metrq_dj/manage.py ./metrq_dj/

# Ensure Python can import from metrq_dj
ENV PYTHONPATH=/app:$PYTHONPATH

# Worker configuration via environment
ENV DJANGO_SETTINGS_MODULE=metriq_site.settings
ENV OLLAMA_HOST=http://ollama:11434
ENV REDIS_URL=redis://redis:6379/0
ENV WORKER_ID=""
ENV LOG_LEVEL=INFO

# Health check (optional)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import celery; import redis; print('OK')" || exit 1

# Run Celery worker
# Concurrency set to 1 for SQLite single-writer safety during claiming
# Workers scale horizontally (more containers) rather than more threads
CMD ["celery", "-A", "worker.tasks.batch", "worker", \
     "--loglevel=INFO", \
     "--concurrency=1", \
     "--pool=prefork", \
     "-Q", "scrape_queue,celery"]
