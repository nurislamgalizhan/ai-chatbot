"""SPEC §18: every setting exists in .env.example — and nothing in
.env.example points at a setting that no longer exists."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic_core import PydanticUndefined

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
#: Consumed by docker-compose.yml itself, not by the application.
COMPOSE_ONLY = {"COMPOSE_PROJECT_NAME", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_required_settings_are_listed() -> None:
    required = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if field.default is PydanticUndefined and field.default_factory is None
    }
    assert required, "Settings has required fields (database_url, ...)"
    assert required <= _example_keys()


def test_example_keys_all_exist_as_settings() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    unknown = _example_keys() - fields - COMPOSE_ONLY
    assert unknown == set(), f".env.example mentions settings that do not exist: {unknown}"


def test_example_has_no_values_for_secrets() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("OPENAI_API_KEY", "GREEN_API_TOKEN", "ENCRYPTION_KEY", "POSTGRES_PASSWORD"):
        match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
        assert match is not None, key
        assert match.group(1).strip() == "", f"{key} must stay empty in the example"
