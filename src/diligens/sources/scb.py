"""Client for SCB's business register API (AFR v1), see docs/DATA_SOURCES.md.

Every call carries the `X-API-Key` header, is spaced to at most five calls per second and is
retried on 429 (honouring Retry-After), dropped connections and server errors. SCB's problem
details become Swedish messages. SCB updates its data 04:00 to 04:30 every night and is not
called in that window. The key is never put in a message or a log.
"""

import datetime as dt
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from diligens.config import Settings
from diligens.models import ScbUnit, ScbUnitFull

BASE_URL = "https://apiafr.scb.se"
MAX_PER_SECOND = 5
MAX_ATTEMPTS = 3
MAX_LIMIT = 5000
MAX_WAIT = 10.0  # seconds; caps a long Retry-After
UNEXPECTED = "SCB gav ett oväntat svar. Försök igen senare."
STOCKHOLM = ZoneInfo("Europe/Stockholm")

# Spec path template to response schema, for every endpoint the client uses. A test checks
# them against the recorded spec in tests/fixtures/scb/openapi.json.
CLIENT_ENDPOINTS: dict[str, str] = {
    "/v{v}/api-info": "apiInfoData",
    "/v{v}/juridiskaenheter/naringsgren/{naringsgrenKod}": "jePartialDataCollectionResponseData",
    "/v{v}/juridiskaenheter/omsattningsklass/{omsKlKod}": "jePartialDataCollectionResponseData",
    "/v{v}/juridiskaenheter/anstalldaklass/{anstKlKod}": "jePartialDataCollectionResponseData",
    "/v{v}/juridiskaenheter/lan/{lanKod}": "jePartialDataCollectionResponseData",
    "/v{v}/juridiskaenheter/naringsgren/{naringsgrenKod}/count": "countResponseData",
    "/v{v}/juridiskaenheter/omsattningsklass/{omsKlKod}/count": "countResponseData",
    "/v{v}/juridiskaenheter/{peOrgNr}/full": "jeFullData",
    "/v{v}/kodtabeller/naringsgrenkoder": "naringsgrenKodData",
    "/v{v}/kodtabeller/omsklkoder": "omsKlKodData",
    "/v{v}/kodtabeller/anstklkoder": "anstKlKodData",
    "/v{v}/kodtabeller/lankoder": "lanKodData",
    "/v{v}/kodtabeller/jurformkoder": "jurformKodData",
    "/v{v}/kodtabeller/ftgstatkoder": "ftgStatKodData",
}

Filter = Literal["naringsgren", "omsattningsklass", "anstalldaklass", "lan"]
CodeTable = Literal[
    "naringsgrenkoder", "omsklkoder", "anstklkoder", "lankoder", "jurformkoder", "ftgstatkoder"
]


class ScbError(Exception):
    """Swedish message for the advisor; never contains the API key."""


def pe_org_nr(org_nr: str) -> str:
    """SCB's 12 digit key from an organisation number in any common form."""
    digits = "".join(c for c in org_nr if c.isdigit())
    if len(digits) == 10:
        return "16" + digits
    if len(digits) == 12:
        return digits
    raise ScbError(f"Organisationsnumret {org_nr} har inte 10 eller 12 siffror.")


def _message(status: int, detail: str) -> str:
    if status == 401:
        return "SCB avvisade API-nyckeln. Kontrollera SCB_API_KEY i .env."
    if status == 404:
        return f"Uppgiften hittades inte hos SCB ({detail})."
    if status == 429:
        return "SCB begränsar antalet anrop just nu. Försök igen om en stund."
    if status >= 500:
        return f"SCB svarar inte just nu (status {status}). Försök igen senare."
    return f"SCB kunde inte besvara frågan: {detail}"


def make_scb_client(settings: Settings) -> "ScbClient":
    """Client from the key in .env, which is passed on and never printed."""
    if settings.scb_api_key is None:
        raise ScbError("SCB_API_KEY saknas i .env.")
    return ScbClient(settings.scb_api_key.get_secret_value())


class ScbClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(STOCKHOLM),
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._last: float | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ScbClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._last is not None:
            wait = 1 / MAX_PER_SECOND - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        local = self._now()
        if local.hour == 4 and local.minute < 30:
            raise ScbError(
                "SCB uppdaterar sitt register mellan 04:00 och 04:30. Försök igen efter 04:30."
            )
        response: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ScbError("Kunde inte nå SCB. Kontrollera anslutningen.") from None
                self._sleep(2**attempt)
                continue
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < MAX_ATTEMPTS - 1:
                retry_after = response.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else 2**attempt
                self._sleep(min(wait, MAX_WAIT))
                continue
            break
        assert response is not None
        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise ScbError(UNEXPECTED) from exc
        try:
            detail = str(response.json().get("detail", ""))
        except (ValueError, AttributeError):
            detail = response.text[:200]
        raise ScbError(_message(response.status_code, detail))

    def api_info(self) -> dict[str, Any]:
        info: dict[str, Any] = self._get("/v1/api-info")
        return info

    def count(self, kind: Filter, code: str | int) -> int:
        try:
            return int(self._get(f"/v1/juridiskaenheter/{kind}/{code}/count")["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScbError(UNEXPECTED) from exc

    def legal_units(
        self, kind: Filter, code: str | int, limit: int = MAX_LIMIT
    ) -> Iterator[ScbUnit]:
        """Every legal unit matching one filter, following the cursor until hasMore is false."""
        params: dict[str, Any] = {"limit": min(limit, MAX_LIMIT)}
        while True:
            page = self._get(f"/v1/juridiskaenheter/{kind}/{code}", params)
            try:
                units = [ScbUnit.model_validate(raw) for raw in page.get("jes") or []]
                pagination = page.get("pagination") or {}
                more = bool(pagination.get("hasMore"))
                cursor = pagination.get("nextCursorId") if more else None
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                raise ScbError(UNEXPECTED) from exc
            yield from units
            if not more or cursor is None:
                return
            params["cursorId"] = cursor

    def full(self, org_nr: str) -> ScbUnitFull:
        raw = self._get(f"/v1/juridiskaenheter/{pe_org_nr(org_nr)}/full")
        try:
            return ScbUnitFull.model_validate(raw)
        except (ValidationError, KeyError, TypeError) as exc:
            raise ScbError(UNEXPECTED) from exc

    def code_table(self, name: CodeTable) -> dict[str, str]:
        """Code to Swedish text; codes as strings, since responses send them as strings."""
        rows = self._get(f"/v1/kodtabeller/{name}")
        try:
            return {str(row["kod"]): str(row["klartext"]).strip() for row in rows}
        except (KeyError, TypeError) as exc:
            raise ScbError(UNEXPECTED) from exc
