"""Client for Bolagsverket's free API "Värdefulla datamängder", see docs/DATA_SOURCES.md.

OAuth2 client credentials with the read scope; the token is cached until shortly before it
expires and fetched again once if the API rejects it. Organisation lookup, document list
(newest report first) and document download (zip with iXBRL). The API has a single
production environment. Credentials and tokens are never put in messages or logs.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from diligens.config import Settings
from diligens.models import AnnualReport, DocumentInfo
from diligens.sources.ixbrl import NotAnnualReport, ReportUnreadable, read_package

TOKEN_URL = "https://portal.api.bolagsverket.se/oauth2/token"
BASE_URL = "https://gw.api.bolagsverket.se/vardefulla-datamangder/v1"
SCOPE = "vardefulla-datamangder:read vardefulla-datamangder:ping"
MAX_ATTEMPTS = 3
MAX_PER_SECOND = 4  # shared by all threads; the API answers 429 when called faster
MAX_WAIT = 10.0  # seconds; caps a long Retry-After
TIMEOUT = 15  # seconds per request; keeps a full screening well under a minute
MAX_DOCUMENTS = 2
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
TOKEN_MARGIN = 60  # seconds before expiry when a new token is fetched

# Paths in the recorded spec (tests/fixtures/bolagsverket/openapi.json) the client uses.
CLIENT_PATHS = ["/isalive", "/organisationer", "/dokumentlista", "/dokument/{dokumentId}"]


class BolagsverketError(Exception):
    """Swedish message for the advisor; never contains credentials."""


class BolagsverketUnavailable(BolagsverketError):
    """Bolagsverket cannot be reached right now (network, overload, server errors)."""


class ReportNotReadable(BolagsverketError):
    """A digital annual report exists but could not be read."""


def org_nr10(org_nr: str) -> str:
    """Bolagsverket's identitetsbeteckning: the ten digits of an organisation number."""
    digits = "".join(c for c in org_nr if c.isdigit())
    if len(digits) == 12 and digits.startswith("16"):
        digits = digits[2:]
    if len(digits) != 10:
        raise BolagsverketError(f"Organisationsnumret {org_nr} måste ha 10 siffror.")
    return digits


def _message(response: httpx.Response) -> str:
    status = response.status_code
    try:
        body = response.json()
        detail = f"{body.get('title', '')} ({body.get('instance', '')})".strip()
    except (ValueError, AttributeError):
        detail = ""
    if status == 429:
        return "Bolagsverket begränsar antalet anrop just nu (status 429). Försök igen om en stund."
    if status in (401, 403):
        return (
            "Bolagsverket avvisade inloggningen. Kontrollera BOLAGSVERKET_CLIENT_ID och "
            "BOLAGSVERKET_CLIENT_SECRET i .env."
        )
    if status == 404:
        return f"Uppgiften hittades inte hos Bolagsverket {detail}."
    if status >= 500:
        return f"Bolagsverket svarar inte just nu (status {status}). Försök igen senare."
    return f"Bolagsverket kunde inte besvara frågan, status {status} {detail}."


UNREACHABLE = "Kunde inte nå Bolagsverket. Kontrollera anslutningen."


def make_bolagsverket_client(settings: Settings) -> "BolagsverketClient":
    """Client from the credentials in .env, which are passed on and never printed."""
    if settings.bolagsverket_client_id is None or settings.bolagsverket_client_secret is None:
        raise BolagsverketError(
            "BOLAGSVERKET_CLIENT_ID eller BOLAGSVERKET_CLIENT_SECRET saknas i .env."
        )
    return BolagsverketClient(
        settings.bolagsverket_client_id.get_secret_value(),
        settings.bolagsverket_client_secret.get_secret_value(),
    )


class BolagsverketClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        token_url: str = TOKEN_URL,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = (client_id, client_secret)
        self._http = httpx.Client(timeout=TIMEOUT, transport=transport)
        self._token_url = token_url
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last: float | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BolagsverketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _access_token(self, rejected: str | None = None) -> str:
        """A valid token. `rejected` is a token the API refused: only that one is replaced,
        so threads that hit the same 401 do not each fetch a new token."""
        with self._lock:  # screening calls the API from several threads
            if self._token and self._token != rejected and self._clock() < self._expires:
                return self._token
            for attempt in range(2):
                try:
                    response = self._http.post(
                        self._token_url,
                        data={"grant_type": "client_credentials", "scope": SCOPE},
                        auth=self._credentials,
                    )
                    break
                except httpx.TransportError as exc:
                    if attempt == 1:
                        raise BolagsverketUnavailable(UNREACHABLE) from exc
                    self._sleep(1)
            if not response.is_success:
                raise BolagsverketError(_message(response))
            try:
                body = response.json()
                self._token = str(body["access_token"])
                expires_in = float(body.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise BolagsverketError(
                    "Bolagsverket gav ett oväntat svar på inloggningen."
                ) from exc
            self._expires = self._clock() + expires_in - TOKEN_MARGIN
            return self._token

    def _pace(self) -> None:
        """At most MAX_PER_SECOND calls, counted across all threads."""
        with self._pace_lock:
            if self._last is not None:
                wait = 1 / MAX_PER_SECOND - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()

    def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        rejected: str | None = None
        response: httpx.Response | None = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            token = self._access_token(rejected)
            self._pace()
            try:
                response = self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError:
                attempt += 1
                if attempt == MAX_ATTEMPTS:
                    raise BolagsverketUnavailable(UNREACHABLE) from None
                self._sleep(2**attempt)
                continue
            if response.status_code == 401 and rejected is None:
                rejected = token  # revoked or expired early: replace it once
                continue
            if (response.status_code == 429 or response.status_code >= 500) and (
                attempt < MAX_ATTEMPTS - 1
            ):
                attempt += 1
                retry_after = response.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else 2**attempt
                self._sleep(min(wait, MAX_WAIT))
                continue
            break
        assert response is not None
        if response.status_code == 429 or response.status_code >= 500:
            raise BolagsverketUnavailable(_message(response))
        if not response.is_success:
            raise BolagsverketError(_message(response))
        return response

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise BolagsverketError("Bolagsverket gav ett svar som inte gick att läsa.") from exc
        if not isinstance(body, dict):
            raise BolagsverketError("Bolagsverket gav ett oväntat svar.")
        return body

    def company_name(self, org_nr: str) -> str | None:
        body = self._json(
            self._request("POST", "/organisationer", {"identitetsbeteckning": org_nr10(org_nr)})
        )
        for organisation in body.get("organisationer") or []:
            names = (organisation.get("organisationsnamn") or {}).get("organisationsnamnLista")
            if names:
                return str(names[0].get("namn", "")) or None
        return None

    def documents(self, org_nr: str) -> list[DocumentInfo]:
        """Filed annual report packages, newest reporting period first."""
        body = self._json(
            self._request("POST", "/dokumentlista", {"identitetsbeteckning": org_nr10(org_nr)})
        )
        docs = []
        for d in body.get("dokument") or []:
            if not isinstance(d, dict) or not d.get("dokumentId"):
                continue
            try:
                docs.append(
                    DocumentInfo(
                        document_id=d["dokumentId"],
                        period_end=d.get("rapporteringsperiodTom"),
                        registered=d.get("registreringstidpunkt"),
                    )
                )
            except ValueError:
                continue  # malformed dates: skip the entry rather than the company
        return sorted(docs, key=lambda d: (d.period_end is not None, d.period_end), reverse=True)

    def download(self, document_id: str) -> bytes:
        content = self._request("GET", f"/dokument/{document_id}").content
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise ReportNotReadable("Årsredovisningen är för stor för att läsas.")
        return content

    def latest_report(self, org_nr: str) -> AnnualReport | None:
        """Figures from the newest digital annual report; None when none is filed digitally.

        Raises ReportNotReadable when a report exists but cannot be read, so a parsing problem
        never passes for a missing report. At most MAX_DOCUMENTS packages are tried.
        """
        for doc in self.documents(org_nr)[:MAX_DOCUMENTS]:
            try:
                return read_package(self.download(doc.document_id), doc.document_id)
            except NotAnnualReport:
                continue  # e.g. a package without figures; try the previous one
            except ReportUnreadable as exc:
                raise ReportNotReadable(f"Årsredovisningen kunde inte läsas: {exc}") from exc
        return None
