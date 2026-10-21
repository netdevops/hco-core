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


class ActorNotUtf8Error(InvalidActorError):
    """The header bytes are not UTF-8. The message shows no part of them."""

    def __init__(self) -> None:
        ValueError.__init__(self, f"the {ACTOR_HEADER} header is not UTF-8 text")


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
    if not is_valid_name(name):
        raise InvalidActorError(name)
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return Actor(name, "user", "api", None if request_id is None else str(request_id))


def _utf8_name(header: str) -> str:
    """Return the NFKC name in the header, which Starlette decodes as Latin-1.

    Raises:
        ActorNotUtf8Error: If the header bytes are not UTF-8.

    """
    try:
        return unicodedata.normalize("NFKC", header.encode("latin-1").decode("utf-8"))
    except UnicodeError as error:
        raise ActorNotUtf8Error from error
