# Citadel: FastAPI app + web UI. Postgres runs as its own service (see
# docker-compose.yml). Build:  docker build -t citadel .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml ./
RUN mkdir citadel && touch citadel/__init__.py \
    && pip install -e . \
    && rm -rf citadel

COPY citadel ./citadel

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "citadel.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
