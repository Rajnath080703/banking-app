# Production image: small, non-root, no compilers, no secrets baked in.
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn==23.0.0
COPY bank ./bank
COPY migrations ./migrations
COPY wsgi.py run.py ./
RUN useradd --system --uid 10001 --home /app bank && mkdir -p /app/instance /data/uploads \
    && chown -R bank:bank /app/instance /data/uploads
USER bank
ENV BANK_ENV=production BANK_UPLOAD_DIR=/data/uploads
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["gunicorn", "wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--timeout", "60", \
     "--graceful-timeout", "30", "--forwarded-allow-ips", "*"]
# (no gunicorn access log: it would print query strings, which can contain a searched PAN or mobile;
#  the app writes its own structured request log without them)
