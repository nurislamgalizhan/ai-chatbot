COMPOSE ?= docker compose
COMPOSE_DEV = $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
PY ?= .venv/bin/python

.PHONY: up up-dev down build logs migrate revision test lint fmt \
        create-admin reset-admin-password kb-import venv smoke

up:
	$(COMPOSE) up -d --build

up-dev:
	$(COMPOSE_DEV) up -d --build

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f --tail=100

migrate:
	$(COMPOSE) run --rm backend alembic upgrade head

revision:
	$(COMPOSE) run --rm backend alembic revision --autogenerate -m "$(m)"

create-admin:
	$(COMPOSE) run --rm -it backend python -m app.cli create-admin

reset-admin-password:
	$(COMPOSE) run --rm -it backend python -m app.cli reset-admin-password

kb-import:
	$(COMPOSE) run --rm -v $(PWD)/seed:/srv/seed:ro backend python -m app.cli kb-import

smoke:
	$(COMPOSE) ps
	$(COMPOSE) exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read().decode())"

# --- local development (host venv) ---

venv:
	python3.14 -m venv .venv
	.venv/bin/pip install -e "backend[dev]"

test:
	$(PY) -m pytest tests -q

lint:
	cd backend && ../.venv/bin/ruff check app && ../.venv/bin/ruff format --check app
	cd backend && ../.venv/bin/mypy app
	cd backend && ../.venv/bin/bandit -q -r app -c pyproject.toml
	.venv/bin/pip-audit --skip-editable --progress-spinner off

fmt:
	cd backend && ../.venv/bin/ruff check --fix app && ../.venv/bin/ruff format app
