"""Client for Bolagsverket's free API "Värdefulla datamängder", see docs/DATA_SOURCES.md.

OAuth2 client credentials with the read scope; the token is cached until shortly before it
expires and fetched again once if the API rejects it. Organisation lookup, document list
(newest report first) and document download (zip with iXBRL). The API has a single
production environment. Credentials and tokens are never put in messages or logs.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx

from diligens.models import AnnualReport, DocumentInfo
from diligens.sources.ixbrl import read_package

TOKEN_URL = "https://portal.api.bolagsverket.se/oauth2/token"
BASE_URL = "https://gw.api.bolagsverket.se/vardefulla-datamangder/v1"
SCOPE = "vardefulla-datamangder:read vardefulla-datamangder:ping"
MAX_ATTEMPTS = 3
TOKEN_MARGIN = 60  # seconds before expiry when a new token is fetched

# Paths in the recorded spec (tests/fixtures/bolagsverket/openapi.json) the client uses.
CLIENT_PATHS = ["/isalive", "/organisationer", "/dokumentlista", "/dokument/{dokumentId}"]


class BolagsverketError(Exception):
    """Swedish message for the advisor; never contains credentials."""


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
    except ValueError:
        detail = ""
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
        self._http = httpx.Client(timeout=60, transport=transport)
        self._token_url = token_url
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._expires = 0.0

    def close(self) -> None:
        self._http.close()

    def _access_token(self, refresh: bool = False) -> str:
        if self._token and not refresh and self._clock() < self._expires:
            return self._token
        try:
            response = self._http.post(
                self._token_url,
                data={"grant_type": "client_credentials", "scope": SCOPE},
                auth=self._credentials,
            )
        except httpx.TransportError as exc:
            raise BolagsverketError(
                "Kunde inte nå Bolagsverket. Kontrollera anslutningen."
            ) from exc
        if not response.is_success:
            raise BolagsverketError(_message(response))
        body = response.json()
        self._token = str(body["access_token"])
        self._expires = self._clock() + float(body.get("expires_in", 3600)) - TOKEN_MARGIN
        return self._token

    def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        refreshed = False
        response: httpx.Response | None = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            headers = {"Authorization": f"Bearer {self._access_token(refresh=refreshed)}"}
            try:
                response = self._http.request(
                    method, f"{self._base_url}{path}", json=json, headers=headers
                )
            except httpx.TransportError:
                attempt += 1
                if attempt == MAX_ATTEMPTS:
                    raise BolagsverketError(
                        "Kunde inte nå Bolagsverket. Kontrollera anslutningen."
                    ) from None
                self._sleep(2**attempt)
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = True  # token revoked or expired early: fetch a new one once
                continue
            if (response.status_code == 429 or response.status_code >= 500) and (
                attempt < MAX_ATTEMPTS - 1
            ):
                attempt += 1
                self._sleep(2**attempt)
                continue
            break
        assert response is not None
        if not response.is_success:
            raise BolagsverketError(_message(response))
        return response

    def company_name(self, org_nr: str) -> str | None:
        body = self._request(
            "POST", "/organisationer", {"identitetsbeteckning": org_nr10(org_nr)}
        ).json()
        for organisation in body.get("organisationer", []):
            names = (organisation.get("organisationsnamn") or {}).get("organisationsnamnLista")
            if names:
                return str(names[0].get("namn", "")) or None
        return None

    def documents(self, org_nr: str) -> list[DocumentInfo]:
        """Filed annual report packages, newest reporting period first."""
        body = self._request(
            "POST", "/dokumentlista", {"identitetsbeteckning": org_nr10(org_nr)}
        ).json()
        docs = [
            DocumentInfo(
                document_id=d["dokumentId"],
                period_end=d.get("rapporteringsperiodTom"),
                registered=d.get("registreringstidpunkt"),
            )
            for d in body.get("dokument", [])
        ]
        return sorted(docs, key=lambda d: (d.period_end is not None, d.period_end), reverse=True)

    def download(self, document_id: str) -> bytes:
        return self._request("GET", f"/dokument/{document_id}").content

    def latest_report(self, org_nr: str) -> AnnualReport | None:
        """Figures from the newest digital annual report; None when none is filed digitally."""
        for doc in self.documents(org_nr):
            try:
                return read_package(self.download(doc.document_id), doc.document_id)
            except ValueError:
                continue  # not an iXBRL annual report; try the next one
        return None
