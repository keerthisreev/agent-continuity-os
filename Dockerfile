FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root runtime user; the app writes nothing to the repo tree.
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Render injects $PORT; default 5000 for local runs.
EXPOSE 5000

# Production server (gunicorn). Single worker: scenario-run throttling
# counters and usage totals are per-process, so one worker keeps them
# exact. The dev server (dashboard/app.py) is for local runs only:
#   python3 dashboard/app.py --host 0.0.0.0 --port 5000
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 60 wsgi:app"]
