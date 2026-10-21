"""The Phase 1 identity: the `X-HCO-Actor` header.

The header is a development identity. Any client can send any name. Phase 3
replaces it with OIDC.
"""

import unicodedata
from typing import Annotated, Final

import structlog
from fastapi import Header

from hco_core.api.errors import MissingActorError
from hco_core.services.approval import InvalidActorError, is_valid_name
from hco_core.services.audit import Actor

ACTOR_HEADER: Final = "X-HCO-Actor"


def current_actor(
    x_hco_actor: Annotated[str | None, Header()] = None,
) -> Actor:
    """Return the actor of the request, from the `X-HCO-Actor` header.

    The header bytes are UTF-8, and the name is their NFKC form.

    Raises:
        MissingActorError: If the header is missing.
        InvalidActorError: If the name is not valid for the approval service.

    """
    if x_hco_actor is None:
        message = (
            f"send the {ACTOR_HEADER} header with your user name; "
            "a request that changes data needs it"
        )
        raise MissingActorError(message)
    name = _utf8_name(x_hco_actor)
    if name is None or not is_valid_name(name):
        raise InvalidActorError(x_hco_actor)
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return Actor(name, "user", "api", None if request_id is None else str(request_id))


def _utf8_name(header: str) -> str | None:
    """Return the NFKC name in the header, which Starlette decodes as Latin-1."""
    try:
        return unicodedata.normalize("NFKC", header.encode("latin-1").decode("utf-8"))
    except UnicodeError:
        return None
