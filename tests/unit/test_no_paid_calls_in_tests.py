"""The suite must not be able to spend the owner's money.

`Settings` reads the repo-root `.env` through pydantic-settings, so when
the owner put a real OPENAI_API_KEY there, `create_app` began building
real OpenAI providers inside the tests: the sandbox endpoint answered
200 instead of the honest 503, and any endpoint that reaches a provider
could have billed a live call. conftest blanks those variables before
the app is imported; this test is the tripwire for that.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.main import _build_chat_provider, _build_embedding_provider


def test_settings_carry_no_provider_credentials() -> None:
    settings = get_settings()
    assert settings.openai_api_key == ""
    assert settings.green_api_id_instance == ""
    assert settings.green_api_token == ""


def test_the_app_factory_builds_no_real_providers(settings: Settings) -> None:
    """Belt and braces: even the fixture-built Settings must not produce
    a live client. A provider here means a paid call is one endpoint call
    away."""
    assert _build_chat_provider(settings) is None
    assert _build_embedding_provider(settings) is None
