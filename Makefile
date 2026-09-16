COMPOSE ?= docker compose
COMPOSE_DEV = $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
PY ?= .venv/bin/python

.PHONY: up up-dev down build logs migrate revision test lint fmt \
        create-admin reset-admin-password kb-import seed-prices venv smoke \
        eval eval-live eval-live-fixture load-test preflight \
        backup backup-verify

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

seed-prices:
	$(COMPOSE) run --rm backend python -m app.cli seed-prices

backup:
	./deploy/backup/pg_backup.sh

# Restores the newest dump into a throwaway database and checks what came
# back. The live database is untouched.
backup-verify:
	./deploy/backup/backup_verify.sh

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
	cd backend && ../.venv/bin/ruff check app ../tests && ../.venv/bin/ruff format --check app ../tests
	cd backend && ../.venv/bin/mypy app
	cd backend && ../.venv/bin/bandit -q -r app -c pyproject.toml
	.venv/bin/pip-audit --skip-editable --progress-spinner off

fmt:
	cd backend && ../.venv/bin/ruff check --fix app && ../.venv/bin/ruff format app

# --- acceptance (phase 10) ---
# Offline eval: fake providers, a scratch database next to the real one
# (created, migrated, dropped). Costs nothing. Report -> reports/.
eval:
	mkdir -p reports
	$(COMPOSE) run --rm -T backend python -m app.cli eval > reports/eval-offline.md
	@echo "Отчёт: reports/eval-offline.md"

# Live eval: real OpenAI against YOUR knowledge base and YOUR limits.
# Asks for confirmation and prints the cost at the end.
eval-live:
	mkdir -p reports
	$(COMPOSE) run --rm backend python -m app.cli eval --live --yes > reports/eval-live.md
	@echo "Отчёт: reports/eval-live.md"

# Live eval against the REFERENCE knowledge base in a scratch database:
# the real model, repeatable criteria, and the owner's own database and
# reports untouched. This is the cheap one — a few tenge.
eval-live-fixture:
	mkdir -p reports
	$(COMPOSE) run --rm -T backend python -m app.cli eval --live --fixture --yes \
		> reports/eval-live-fixture.md
	@echo "Отчёт: reports/eval-live-fixture.md"

# 5 and 30 parallel chats through the whole chain, DRY_RUN, scratch database.
load-test:
	mkdir -p reports
	$(COMPOSE) run --rm -T backend python -m app.cli load-test > reports/load-test.md
	@echo "Отчёт: reports/load-test.md"

# Readiness checks (free). Add --with-provider by hand to also ask GREEN-API.
preflight:
	$(COMPOSE) run --rm -T backend python -m app.cli preflight
