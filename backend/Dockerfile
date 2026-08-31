# syntax=docker/dockerfile:1

FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir .

FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app

WORKDIR /srv
COPY --from=builder /venv /venv
COPY --chown=root:root app ./app
COPY --chown=root:root alembic ./alembic
COPY --chown=root:root alembic.ini ./

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Default command: API. The worker overrides this in compose.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
