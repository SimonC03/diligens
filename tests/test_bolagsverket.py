"""M6 Bolagsverket client and iXBRL parser tests. Synthetic fixtures, no network."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from diligens.sources.bolagsverket import (
    CLIENT_PATHS,
    BolagsverketClient,
    BolagsverketError,
    BolagsverketUnavailable,
)
from diligens.sources.ixbrl import parse_ixbrl, read_package

FIXTURES = Path(__file__).parent / "fixtures" / "bolagsverket"
REPORT = (FIXTURES / "arsredovisning_exempel.xhtml").read_bytes()
PACKAGE = (FIXTURES / "dokument_exempel.zip").read_bytes()


def test_ixbrl_parser_reads_figures_with_scale_sign_and_periods() -> None:
    report = parse_ixbrl(REPORT, document_id="doc-1")

    assert report.org_nr == "5599000001"
    assert report.company_name == "Exempelbolaget AB"
    assert (report.period_start, report.period_end) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))
    f = report.figures
    assert f["nettoomsattning"].value == Decimal("32450000")
    assert f["nettoomsattning_fg"].value == Decimal("28900000")
    assert f["rorelseresultat"].value == Decimal("3245000")
    assert f["rorelseresultat_fg"].value == Decimal("-120000")  # sign="-"
    assert f["avskrivningar"].value == Decimal("-640000")
    assert f["arets_resultat"].value == Decimal("2510000")
    assert f["eget_kapital"].value == Decimal("5935000")
    assert f["tillgangar"].value == Decimal("14300000")
    assert f["soliditet"].value == Decimal("0.415")  # scale -2, decimal comma
    assert f["medelantal_anstallda"].value == Decimal("24")
    # Every figure points at its element, context and document.
    ref = f["nettoomsattning"].source_ref
    assert "se-gen-base:Nettoomsattning" in ref and "period0" in ref and "doc-1" in ref

    # The package holds the audit report first; the reader picks the annual report.
    from_zip = read_package(PACKAGE, document_id="doc-1")
    assert from_zip.figures["nettoomsattning"].value == Decimal("32450000")

    # A document without numeric facts (only an audit report) is not an annual report.
    with pytest.raises(ValueError, match="årsredovisning"):
        parse_ixbrl(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>Ingen data</body></html>', "x"
        )


def test_ixbrl_ignores_segment_contexts_and_odd_prior_years() -> None:
    text = REPORT.decode("utf-8")
    # A revenue note split by business line uses a context with a segment and the same dates.
    segment = (
        '<xbrli:context id="segA"><xbrli:entity><xbrli:identifier '
        'scheme="http://www.bolagsverket.se">5599000001</xbrli:identifier><xbrli:segment>'
        "Konsulting</xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01"
        "</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>"
        "</xbrli:context>"
    )
    split = (
        '<p><ix:nonFraction name="se-gen-base:Nettoomsattning" contextRef="segA" unitRef="SEK" '
        'format="ixt:numspacecomma" scale="0" decimals="0">1 000</ix:nonFraction></p>'
    )
    text = text.replace('<xbrli:context id="period0">', segment + '<xbrli:context id="period0">')
    text = text.replace("<body>", "<body>" + split, 1)
    # The prior year was extended to 18 months, so it is not comparable.
    text = text.replace("<xbrli:startDate>2024-01-01", "<xbrli:startDate>2023-07-01")

    report = parse_ixbrl(text.encode("utf-8"), document_id="doc-1")

    assert report.figures["nettoomsattning"].value == Decimal("32450000")
    assert "nettoomsattning_fg" not in report.figures
    assert report.notes == ["Föregående räkenskapsår är 550 dagar och jämförs inte"]


class Server:
    """Fake token endpoint and API, recording requests."""

    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = documents
        self.requests: list[httpx.Request] = []
        self.tokens = 0
        self.fail_next: list[httpx.Response] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            self.tokens += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"t{self.tokens}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "vardefulla-datamangder:read",
                },
            )
        if self.fail_next:
            return self.fail_next.pop(0)
        if request.url.path.endswith("/dokumentlista"):
            body = json.loads(request.content)
            docs = self.documents if body["identitetsbeteckning"] == "5599000001" else []
            return httpx.Response(200, json={"dokument": docs})
        if request.url.path.endswith("/dokument/doc-2"):
            return httpx.Response(200, content=PACKAGE, headers={"content-type": "application/zip"})
        if request.url.path.endswith("/organisationer"):
            return httpx.Response(
                200,
                json={
                    "organisationer": [
                        {
                            "organisationsnamn": {
                                "organisationsnamnLista": [{"namn": "Exempelbolaget AB"}]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            404,
            json={
                "type": "about:blank",
                "instance": "not.found",
                "status": 404,
                "title": "Not Found",
            },
        )


def _client(server: Server, clock: list[float]) -> BolagsverketClient:
    return BolagsverketClient(
        "id-not-real",
        "secret-not-real",
        transport=httpx.MockTransport(server.handle),
        clock=lambda: clock[0],
        sleep=lambda s: None,
    )


def test_bolagsverket_client_tokens_documents_and_errors() -> None:
    spec = json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))
    assert set(CLIENT_PATHS) <= set(spec["paths"])

    documents = [
        {
            "dokumentId": "doc-1",
            "filformat": "application/zip",
            "rapporteringsperiodTom": "2024-12-31",
            "registreringstidpunkt": "2025-06-30",
        },
        {
            "dokumentId": "doc-2",
            "filformat": "application/zip",
            "rapporteringsperiodTom": "2025-12-31",
            "registreringstidpunkt": "2026-06-30",
        },
    ]
    server = Server(documents)
    clock = [0.0]
    client = _client(server, clock)

    docs = client.documents("559900-0001")
    assert [d.document_id for d in docs] == ["doc-2", "doc-1"]  # newest first
    token_request = server.requests[0]
    assert token_request.url.path == "/oauth2/token"
    form = dict(x.split("=") for x in token_request.content.decode().split("&"))
    assert form["grant_type"] == "client_credentials"
    assert "vardefulla-datamangder%3Aread" in form["scope"]
    assert token_request.headers["Authorization"].startswith("Basic ")
    api = server.requests[1]
    assert api.headers["Authorization"] == "Bearer t1"
    assert json.loads(api.content) == {"identitetsbeteckning": "5599000001"}

    report = client.latest_report("5599000001")
    assert report is not None and report.document_id == "doc-2"
    assert report.figures["nettoomsattning"].value == Decimal("32450000")
    assert client.company_name("5599000001") == "Exempelbolaget AB"
    assert server.tokens == 1  # token reused while valid

    clock[0] = 3600.0  # expired: a new token is fetched
    client.documents("5599000001")
    assert server.tokens == 2

    # No digital annual report: None, never a guess.
    assert client.latest_report("5599000002") is None

    # A rejected token is refreshed once; other errors become Swedish messages.
    server.fail_next = [
        httpx.Response(
            401,
            json={
                "type": "urn:bolagsverket:error:auth",
                "instance": "x",
                "status": 401,
                "title": "Unauthorized",
            },
        )
    ]
    client.documents("5599000001")
    assert server.tokens == 3
    server.fail_next = [
        httpx.Response(
            400,
            json={
                "type": "urn:bolagsverket:error:validation",
                "instance": "identitetsbeteckning",
                "status": 400,
                "title": "Bad Request",
            },
        )
    ]
    with pytest.raises(BolagsverketError, match="Bolagsverket"):
        client.documents("5599000001")
    with pytest.raises(BolagsverketError, match="10 siffror"):
        client.documents("123")


def test_bolagsverket_paces_calls_and_honours_retry_after() -> None:
    server = Server(documents=[])
    now = [0.0]
    sleeps: list[float] = []
    client = BolagsverketClient(
        "id-not-real",
        "secret-not-real",
        transport=httpx.MockTransport(server.handle),
        clock=lambda: now[0],
        sleep=sleeps.append,
    )
    # Throttled once with a long Retry-After: the wait is capped, then the call succeeds.
    server.fail_next = [httpx.Response(429, headers={"Retry-After": "120"})]
    assert client.documents("5599000001") == []
    assert 10.0 in sleeps  # capped Retry-After
    assert 0.25 in sleeps  # pacing: at most four calls per second

    # Throttled on every attempt: a clear message, as an outage the screening stops on.
    server.fail_next = [httpx.Response(429)] * 3
    with pytest.raises(BolagsverketUnavailable, match="begränsar antalet anrop"):
        client.documents("5599000001")
