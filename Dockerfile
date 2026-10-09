FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    LEDGER_INBOX_DATA_DIR=/data \
    PATH=/app/.venv/bin:$PATH

RUN useradd --create-home --uid 10001 ledger \
    && mkdir -p /data && chown ledger:ledger /data && chmod 700 /data
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.11.9 \
    && uv sync --locked --no-dev --no-editable

USER ledger
VOLUME ["/data"]
EXPOSE 8080
CMD ["ledger-inbox", "serve", "--host", "0.0.0.0", "--port", "8080"]
