# Runtime image. One process (waitress + a thread pool) — the app's in-memory
# caches, managed Tor process, and wanted worker all assume a single process.
FROM python:3.12-slim

# Stream logs to `docker logs` immediately (stdout is block-buffered in
# containers otherwise, and the worker's output would lag or never appear).
ENV PYTHONUNBUFFERED=1

# Tor, so the app can route AudiobookBay requests through it. The app starts
# and manages the tor process itself; no system service needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits don't bust the pip layer.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

# Run as a non-root user. /data holds the download log + the persisted secret
# key; if you mount a host dir there, it must be writable by uid 1000
# (`chown -R 1000:1000 ./data`) — otherwise those two features degrade with a
# warning but the app still runs.
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /data \
    && chown app:app /data
USER app

EXPOSE 5078

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:5078/healthz', timeout=4)" || exit 1

CMD ["python", "main.py"]
