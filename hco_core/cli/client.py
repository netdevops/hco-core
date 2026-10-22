"""The HTTP client of the CLI. It talks only to the REST API of hco-core.

Each error becomes one plain sentence in `ApiError`. A sentence never shows a
password or a user name from `HCO_API_URL`, and never shows a control character
from an answer.
"""

import json
import re
import unicodedata
from typing import Final, cast

import httpx
from pydantic import BaseModel, ValidationError

from hco_core.api.actor import ACTOR_HEADER
from hco_core.api.deps import API_PREFIX
from hco_core.api.errors import PROBLEM_TYPE

TIMEOUT_SECONDS: Final = 30.0
"""The default time that one request waits for the API."""
API_SCHEMES: Final = frozenset({"http", "https"})
INVALID_URL: Final = "HCO_API_URL is not an http or https URL with a host"
MAX_DETAIL: Final = 500
_LINE_BREAKS: Final = re.compile(r"[^\S ]+")
"""White space other than a space, such as a tab or a new line."""


class ApiError(Exception):
    """The API refused a request or is not reachable.

    `problem` holds the problem document of the API, if it sent one.
    """

    def __init__(self, sentence: str, problem: dict[str, object] | None = None) -> None:
        super().__init__(sentence)
        self.problem = problem


class ApiClient:
    """Send requests to `/api/v1` of the API, with the actor in `X-HCO-Actor`."""

    def __init__(
        self,
        base_url: str,
        actor: str | None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = safe_url(base_url)
        """The API URL with no user information, safe to print."""
        headers: dict[bytes, bytes] = (
            {} if actor is None else {ACTOR_HEADER.encode(): actor.encode()}
        )
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def get[Model: BaseModel](
        self,
        path: str,
        model: type[Model],
        params: dict[str, str] | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> Model:
        """Return the answer to a GET as the model, or raise `ApiError`."""
        request = self._http.build_request("GET", path, params=params, timeout=timeout)
        return self._send(request, model)

    def post[Model: BaseModel](
        self,
        path: str,
        model: type[Model],
        body: dict[str, str] | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> Model:
        """Return the answer to a POST as the model, or raise `ApiError`."""
        request = self._http.build_request("POST", path, json=body, timeout=timeout)
        return self._send(request, model)

    def _send[Model: BaseModel](
        self, request: httpx.Request, model: type[Model]
    ) -> Model:
        try:
            response = self._http.send(request)
        except httpx.HTTPError as error:
            raise ApiError(self._failure(request.method, error)) from None
        if not response.is_success:
            raise self._error(response)
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            message = (
                f"the API at {self.url} sent an answer that this CLI cannot read; "
                "use the same hco-core version for both"
            )
            raise ApiError(message) from None

    def _failure(self, method: str, error: httpx.HTTPError) -> str:
        """Return the sentence for a request that got no answer."""
        reason = type(error).__name__
        if isinstance(error, httpx.TimeoutException) and not isinstance(
            error, httpx.ConnectTimeout
        ):
            if method == "GET":
                return f"the API at {self.url} did not answer in time ({reason}); try again"
            return (
                f"the API at {self.url} did not answer in time ({reason}), and it "
                "may have done the change; check with hco-core run list or "
                "hco-core approval list before you try again"
            )
        return (
            f"cannot reach the API at {self.url} ({reason}); "
            "check HCO_API_URL, and start the API with: hco-core serve api"
        )

    def _error(self, response: httpx.Response) -> ApiError:
        problem = _problem(response)
        if problem is None:
            return ApiError(
                f"the API at {self.url} answered HTTP {response.status_code}"
            )
        if problem["type"] == f"{PROBLEM_TYPE}missing-actor":
            message = (
                f"the API needs your user name in the {ACTOR_HEADER} header; "
                "add --as <name>, or set HCO_ACTOR"
            )
            return ApiError(message, problem)
        return ApiError(_sentence(problem), problem)


def safe_url(base_url: str) -> str:
    """Return the URL of the API with no user information.

    Raises:
        ApiError: If the URL is not an http or https URL with a host.

    """
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL:
        raise ApiError(INVALID_URL) from None
    if url.scheme not in API_SCHEMES or not url.host:
        raise ApiError(INVALID_URL)
    return str(url.copy_with(userinfo=b""))


def _problem(response: httpx.Response) -> dict[str, object] | None:
    """Return the problem document of the answer, or `None` if it is not one."""
    try:
        body: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    problem = cast("dict[str, object]", body)
    kind, detail = problem.get("type"), problem.get("detail")
    if not isinstance(kind, str) or not isinstance(detail, str):
        return None
    return problem


def _sentence(problem: dict[str, object]) -> str:
    """Return the detail, and the field and reason of each schema error."""
    fields = [_field(item) for item in _list(problem.get("errors"))]
    detail = str(problem["detail"])
    return f"{detail} ({'; '.join(fields)})" if fields else detail


def _field(item: object) -> str:
    entry = cast("dict[str, object]", item) if isinstance(item, dict) else {}
    location = ".".join(str(part) for part in _list(entry.get("loc")))
    return f"{location}: {entry.get('msg', '')}"


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def printable(text: str) -> str:
    """Return the text on one line, with no control character, and cut.

    The spaces at the start stay, so a config line keeps its indentation.
    """
    visible = "".join(
        character
        for character in text
        if character.isspace() or unicodedata.category(character)[0] != "C"
    )
    return _LINE_BREAKS.sub(" ", visible).rstrip()[:MAX_DETAIL]
