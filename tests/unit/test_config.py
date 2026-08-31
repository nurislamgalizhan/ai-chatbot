import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings

VALID_KEY = Fernet.generate_key().decode()
DB = "postgresql+asyncpg://u:p@localhost/db"
REDIS = "redis://localhost:6379/0"


def make(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "prod",
        "database_url": DB,
        "redis_url": REDIS,
        "encryption_key": VALID_KEY,
        "cors_origins": "https://bot.example.kz",
        "_env_file": None,  # never read a developer's .env in unit tests
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_valid_prod_config() -> None:
    settings = make()
    assert settings.cors_origin_list == ["https://bot.example.kz"]
    assert settings.cookie_secure is True
    assert settings.docs_enabled is False


def test_prod_requires_cors_origins() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        make(cors_origins="")


@pytest.mark.parametrize(
    "origins",
    [
        "http://bot.example.kz",  # not https
        "https://localhost:5173",  # localhost
        "https://127.0.0.1",  # loopback
        "*",  # wildcard
        "https://bot.example.kz/",  # trailing slash
    ],
)
def test_prod_rejects_bad_origins(origins: str) -> None:
    with pytest.raises(ValidationError):
        make(cors_origins=origins)


def test_dev_allows_localhost_http() -> None:
    settings = make(app_env="dev", cors_origins="http://127.0.0.1:8083")
    assert settings.cookie_secure is False
    assert settings.docs_enabled is True


def test_rejects_sync_database_driver() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        make(database_url="postgresql://u:p@localhost/db")


def test_rejects_invalid_fernet_key() -> None:
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        make(encryption_key="not-a-key")
