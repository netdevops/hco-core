"""The Phase 1 identity: the `X-HCO-Actor` header.

The header is a development identity. Any client can send any name. Phase 3
replaces it with OIDC.
"""

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
    if not is_valid_name(x_hco_actor):
        raise InvalidActorError(x_hco_actor)
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return Actor(
        x_hco_actor, "user", "api", None if request_id is None else str(request_id)
    )
