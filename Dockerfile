FROM temporalio/temporal:1.5.1 AS temporal-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 --shell /bin/bash appuser \
    && mkdir -p /app \
    && chown appuser:appuser /app

WORKDIR /app

COPY --chown=appuser:appuser pyproject.toml README.md ./
COPY --chown=appuser:appuser src ./src

RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir pytest pytest-asyncio ruff mypy

COPY --chown=appuser:appuser tests ./tests
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser docs ./docs
COPY --chown=appuser:appuser plans ./plans

COPY --from=temporal-cli /usr/local/bin/temporal /usr/local/bin/temporal
RUN chmod 755 /usr/local/bin/temporal

ENV TEMPORAL_DEV_SERVER_PATH=/usr/local/bin/temporal

USER appuser

CMD ["bash", "scripts/test-in-container.sh"]
