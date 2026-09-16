"""The secrets in .env must actually reach the containers.

Found while preparing the owner's .env for the OpenAI key: `x-app-env`
listed seven variables, and `OPENAI_API_KEY` / `GREEN_API_*` were not
among them. `Settings` reads `.env` relative to the working directory,
which inside the image is /srv — and the file is deliberately not baked
into the image. So the key could be pasted into .env, the stack
restarted, and the bot would still answer nobody: no key, no transport.

Nothing caught it because the tests build `Settings` in-process and the
dev stack has no keys at all, so "не задан" looked like the expected
state. This test is the guard: the environment file is wired into both
processes that talk to OpenAI and GREEN-API, and the in-network
addresses still win over whatever .env carries for host-side tooling.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
#: The two processes that hold secrets: the API (sandbox, knowledge base
#: helpers) and the worker (answers, media, transport).
SECRET_BEARING = ("backend", "worker")


def env_files(service: str) -> list[str]:
    """Normalise both accepted shapes: ["file"] and [{path: file}]."""
    raw = SERVICES[service].get("env_file") or []
    if isinstance(raw, str):
        raw = [raw]
    return [entry if isinstance(entry, str) else entry["path"] for entry in raw]


@pytest.mark.parametrize("service", SECRET_BEARING)
def test_the_environment_file_is_wired_into_the_service(service: str) -> None:
    assert ".env" in env_files(service), (
        f"{service} would start without OPENAI_API_KEY and GREEN_API_*: "
        "they exist only in .env and nothing else passes them in"
    )


@pytest.mark.parametrize("service", SECRET_BEARING)
def test_in_network_addresses_win_over_the_environment_file(service: str) -> None:
    """.env carries host-side URLs for local alembic runs; a container
    must still talk to the service names. `environment` is applied after
    `env_file`, so it overrides — this asserts it is actually set."""
    environment = SERVICES[service]["environment"]
    assert "postgres:5432" in environment["DATABASE_URL"]
    assert environment["REDIS_URL"] == "redis://redis:6379/0"


def test_the_frontend_gets_no_secrets() -> None:
    """nginx serves static files; handing it the keys would widen the
    blast radius of a web-server compromise for nothing."""
    assert env_files("frontend") == []
    assert "environment" not in SERVICES["frontend"]


def test_every_setting_the_example_documents_can_arrive() -> None:
    """A variable listed in .env.example is worthless if nothing carries
    it into the container: either compose names it explicitly, or the
    environment file does."""
    documented = {
        line.split("=", 1)[0].lstrip("# ").strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and line.lstrip("# ")[:1].isupper()
    }
    compose_only = {"COMPOSE_PROJECT_NAME", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    named = set(SERVICES["worker"]["environment"])
    for variable in documented - compose_only - named:
        assert ".env" in env_files("worker"), f"{variable} never reaches the worker"
