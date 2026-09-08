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

# Bake the tiktoken encoding into the image: the container has no business
# downloading tokenizer data at runtime (and may have no egress for it).
ENV TIKTOKEN_CACHE_DIR=/srv/tiktoken-cache
RUN mkdir -p /srv/tiktoken-cache \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && chown -R root:root /srv/tiktoken-cache && chmod -R a+rX /srv/tiktoken-cache

# The media volume's mount point is created HERE, owned by the app user:
# Docker copies the image's ownership onto a fresh named volume, so
# without this the worker would hit EACCES on the very first download
# (a new volume is root:root 0755, and the process runs as uid 10001).
RUN mkdir -p /data/media && chown -R app:app /data && chmod 700 /data/media

USER app

# Default command: API. The worker overrides this in compose.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
