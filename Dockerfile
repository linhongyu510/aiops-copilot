FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/workspace/.venv/bin:${PATH}"

WORKDIR /workspace

RUN pip install --no-cache-dir "uv>=0.8,<1"

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN uv sync --frozen --no-dev --extra local-embeddings

COPY static ./static
COPY mcp_servers ./mcp_servers
COPY aiops-docs ./aiops-docs

RUN useradd --create-home --uid 10001 aiops \
    && mkdir -p /workspace/logs /workspace/uploads \
    && chown -R aiops:aiops /workspace

USER aiops
EXPOSE 9900

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9900/live', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9900"]
