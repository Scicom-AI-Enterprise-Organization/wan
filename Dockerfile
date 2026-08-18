FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first so source edits do not invalidate the install layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && pip install 'uvicorn[standard]>=0.23' 'httpx>=0.24' opentelemetry-instrumentation-httpx

COPY pyproject.toml README.md ./
COPY wan ./wan
RUN pip install --no-deps .

COPY app.py ./

# Run unprivileged; nothing here needs root.
RUN useradd --create-home --uid 10001 app && chown -R app /srv
USER app

EXPOSE 8000

# No curl in slim, and stdlib urllib keeps the image small.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
