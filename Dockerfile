FROM python:3.12-slim

# Runs as a dedicated non-root user - if the app process is ever
# compromised, it inherits this user's limited permissions, not root.
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Lets Docker/Jenkins/orchestration tooling know if the app is actually
# serving requests, not just "the process is running."
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

# gunicorn, not Flask's dev server - a real WSGI server, no debug mode.
# Config (including the preload_app fix for a multi-worker startup race)
# lives in gunicorn.conf.py.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
