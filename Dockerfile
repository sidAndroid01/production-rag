FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/opt/models \
    RAG_HOST=0.0.0.0
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra embeddings

# Bake the embedding weights into the image so the read-only runtime never
# downloads at request time.
COPY embeddings.py ./
RUN python -c "from embeddings import LocalFastEmbedder; LocalFastEmbedder().embed_query('warm')"

COPY *.py ./
COPY migrations ./migrations
COPY evals ./evals

RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"
CMD ["python", "api.py"]
