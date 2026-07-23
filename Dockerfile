FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md requirements.lock requirements-dev.lock ./
COPY src ./src
RUN pip install --require-hashes --requirement requirements.lock \
    && pip install --no-deps .

FROM base AS test
RUN pip install --require-hashes --requirement requirements-dev.lock
COPY alembic.ini ./
COPY tests ./tests
COPY migrations ./migrations
CMD ["pytest", "-q"]

FROM base AS runtime
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["club-api"]
