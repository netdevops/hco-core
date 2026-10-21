"""The `X-HCO-Actor` header is UTF-8, and the stored name is its NFKC form."""

from http import HTTPStatus

import pytest
from sqlalchemy import Engine, text

from tests.api.conftest import Api

FULLWIDTH_A = chr(0xFF21)
CYRILLIC_A = chr(0x0430)


def synced_actor(api: Api, engine: Engine, header: str | bytes) -> str:
    """Sync with the header, and return the actor of the audit row."""
    response = api.call("POST", "/api/v1/devices/sync", actor=header)
    assert response.status_code == HTTPStatus.OK
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT actor FROM hco.audit_event")
        ).scalar_one()


@pytest.mark.parametrize(
    ("header", "stored"),
    [
        ("José", "José"),
        (f"{FULLWIDTH_A}lice", "Alice"),
        (f"{CYRILLIC_A}lice", f"{CYRILLIC_A}lice"),
    ],
    ids=["accent", "fullwidth", "cyrillic"],
)
def test_the_stored_name_is_the_decoded_nfkc_text(
    api: Api, engine: Engine, header: str, stored: str
) -> None:
    """A UTF-8 name is never stored as Latin-1 text, and NFKC folds width."""
    assert synced_actor(api, engine, header) == stored


def test_a_header_that_is_not_utf8_is_400(api: Api) -> None:
    """Bytes that do not decode as UTF-8 are not a name."""
    response = api.call("POST", "/api/v1/devices/sync", actor=b"al\xffice")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["type"] == "urn:hco-core:problem:invalid-actor"


def test_a_zero_width_space_in_the_name_is_400(api: Api) -> None:
    """An invisible character fails the name rule after the UTF-8 decode."""
    response = api.call("POST", "/api/v1/devices/sync", actor="ali\u200bce")

    assert response.status_code == HTTPStatus.BAD_REQUEST
