"""The settings read `HCO_` environment variables and hide their secrets."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from hco_core.settings import CoreSettings

HCO_VARIABLES = (
    "HCO_DATABASE_URL",
    "HCO_LOG_LEVEL",
    "HCO_LOG_FORMAT",
    "HCO_SOT",
    "HCO_SOT_PATH",
    "HCO_CONNECTION",
    "HCO_TRANSCRIPTS_DIR",
    "HCO_API_URL",
    "HCO_ACTOR",
    "HCO_VALIDATION_KEY",
)
URL_MARKER = "pw-marker-71"
VALIDATION_KEY = "key-marker-0123456789abcdef0123456789"


@pytest.fixture
def empty_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove every `HCO_` variable and run from a folder with no `.env` file."""
    monkeypatch.chdir(tmp_path)
    for name in HCO_VARIABLES:
        monkeypatch.delenv(name, raising=False)


pytestmark = pytest.mark.usefixtures("empty_environment")


def test_defaults_when_the_environment_is_empty() -> None:
    """Each setting has a default for local development."""
    settings = CoreSettings()

    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://hco:hco@localhost:5432/hco"
    )
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.sot == "yaml"
    assert settings.sot_path == Path("inventory.yaml")
    assert settings.connection == "mock"
    assert settings.transcripts_dir == Path("transcripts")
    assert settings.api_url == "http://localhost:8080"
    assert settings.actor is None
    assert settings.validation_key is None


def test_reads_every_hco_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each setting reads its `HCO_` variable."""
    monkeypatch.setenv("HCO_DATABASE_URL", "postgresql+psycopg://u:p@db:6543/app")
    monkeypatch.setenv("HCO_LOG_LEVEL", "debug")
    monkeypatch.setenv("HCO_LOG_FORMAT", "console")
    monkeypatch.setenv("HCO_SOT", "nautobot")
    monkeypatch.setenv("HCO_SOT_PATH", "lab/inventory.yaml")
    monkeypatch.setenv("HCO_CONNECTION", "scrapli")
    monkeypatch.setenv("HCO_TRANSCRIPTS_DIR", "lab/transcripts")
    monkeypatch.setenv("HCO_API_URL", "http://hco.example:9000")
    monkeypatch.setenv("HCO_ACTOR", "alice")
    monkeypatch.setenv("HCO_VALIDATION_KEY", VALIDATION_KEY)

    settings = CoreSettings()

    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://u:p@db:6543/app"
    )
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "console"
    assert settings.sot == "nautobot"
    assert settings.sot_path == Path("lab/inventory.yaml")
    assert settings.connection == "scrapli"
    assert settings.transcripts_dir == Path("lab/transcripts")
    assert settings.api_url == "http://hco.example:9000"
    assert settings.actor == "alice"
    assert settings.validation_key is not None
    assert settings.validation_key.get_secret_value() == VALIDATION_KEY


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_a_plain_postgres_url_gets_the_psycopg_dialect(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    """A plain Postgres URL works, as the specification says."""
    monkeypatch.setenv("HCO_DATABASE_URL", f"{scheme}://hco:hco@127.0.0.1:55432/hco")

    assert CoreSettings().database_url.get_secret_value() == (
        "postgresql+psycopg://hco:hco@127.0.0.1:55432/hco"
    )


@pytest.mark.parametrize(
    "url", ["mysql://u:{password}@db/app", "postgresql+asyncpg://u:{password}@db/app"]
)
def test_refuses_another_database_and_hides_the_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """The error names the setting, never the URL with its password."""
    monkeypatch.setenv("HCO_DATABASE_URL", url.format(password=URL_MARKER))

    with pytest.raises(ValidationError, match="database_url") as caught:
        CoreSettings()

    assert URL_MARKER not in str(caught.value)


@pytest.mark.parametrize(
    ("variable", "value"), [("HCO_LOG_FORMAT", "xml"), ("HCO_LOG_LEVEL", "LOUD")]
)
def test_refuses_an_invalid_log_setting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """The error names the log setting."""
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.removeprefix("HCO_").lower()):
        CoreSettings()


def test_printed_settings_hold_no_password_and_no_validation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A printed or dumped settings object shows no secret."""
    monkeypatch.setenv("HCO_DATABASE_URL", f"postgresql://hco:{URL_MARKER}@db/hco")
    monkeypatch.setenv("HCO_VALIDATION_KEY", VALIDATION_KEY)

    settings = CoreSettings()

    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert URL_MARKER not in text
        assert VALIDATION_KEY not in text
