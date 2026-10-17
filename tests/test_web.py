"""M8 web app tests: pages, the analysis and screening flows, the advisor's choices and
local-only safety. Synthetic data, fake SCB and Bolagsverket, no network and no LLM."""

import datetime as dt
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import fake_registry, fake_scb
from helpers import unit as scb_unit
from openpyxl import load_workbook

from diligens.analysis.bridge import ebitda_bridge
from diligens.config import Settings
from diligens.models import Flag, FlagCategory, Ledger
from diligens.pipeline import run_analysis
from diligens.sie.generator import build_demo_company, generate_demo_company
from diligens.sources.bolagsverket import BolagsverketUnavailable
from diligens.web import views
from diligens.web.app import create_app
from diligens.web.workspace import AnalysisMeta, Workspace

SECRETS = ("scb-key-not-real", "bv-id-not-real", "bv-secret-not-real")
PAGES = (
    "/",
    "/analyser",
    "/analyser/ny",
    "/screening",
    "/screening/ny",
    "/metod",
    "/installningar",
)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key=None,
        scb_api_key=SECRETS[0],
        bolagsverket_client_id=SECRETS[1],
        bolagsverket_client_secret=SECRETS[2],
    )


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "demo_bolag.se"
    generate_demo_company(42, path)
    return path


def _units() -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    units = [scb_unit(1), scb_unit(2, lan="12"), scb_unit(3, jur="10"), scb_unit(9)]
    pe = {n: f"1655990{n:05d}" for n in (1, 2, 3, 9)}
    return units, {"6": [pe[1], pe[2], pe[3]], "7": [pe[9]]}


def _app(workspace: Path, demo: Path) -> TestClient:
    units, turnover = _units()
    app = create_app(
        settings_provider=_settings,
        workspace=workspace,
        scb_factory=lambda s: fake_scb(units, turnover),  # type: ignore[arg-type]
        bolagsverket_factory=lambda s: fake_registry(),
        inline_jobs=True,
        demo_file=demo,
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, demo: Path) -> Iterator[TestClient]:
    with _app(tmp_path / "arbetsyta", demo) as c:
        yield c


def test_every_page_renders_without_secrets(client: TestClient) -> None:
    for path in PAGES:
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Diligens" in page.text and "Due diligence" in page.text
        assert not any(secret in page.text for secret in SECRETS), path
    assert "Koden räknar" in client.get("/metod").text
    missing = client.get("/analyser/20990101-000000-abcdef")
    assert missing.status_code == 404 and "finns inte" in missing.text


def test_analysis_flow_from_upload_to_excel(client: TestClient, tmp_path: Path, demo: Path) -> None:
    started = client.post(
        "/analyser",
        files={"fil": ("demo_bolag.se", demo.read_bytes(), "application/octet-stream")},
        data={"narstaende": "Ägarbolaget AB"},
    )
    # Upload, job and result: the inline job is done, so the redirects end on the summary.
    assert started.status_code == 200
    assert started.url.path.endswith("/sammanfattning")
    item_id = started.url.path.split("/")[2]
    assert "Demobolaget Konsult AB" in started.text and "Stämmer mot SIE-filen" in started.text

    result = run_analysis(demo, use_llm=False, related_parties=["Ägarbolaget AB"])
    assert views.tsek(result.bridge.adjusted) in started.text

    for tab in (
        "resultatrakning",
        "balansrakning",
        "rorelsekapital",
        "nettoskuld",
        "brygga",
        "flaggor",
        "fragor",
        "avstamning",
        "memo",
    ):
        page = client.get(f"/analyser/{item_id}/{tab}")
        assert page.status_code == 200, tab
    assert "Kräver granskning" in client.get(f"/analyser/{item_id}/flaggor").text
    assert client.get(f"/analyser/{item_id}/okand").status_code == 404

    # Every figure traces to a voucher: B473 as booked, as a page and as a drawer fragment.
    voucher = client.get(f"/analyser/{item_id}/verifikat/B473")
    assert voucher.status_code == 200 and "3041" in voucher.text and "1510" in voucher.text
    fragment = client.get(f"/analyser/{item_id}/verifikat/B473?del=1")
    assert "<html" not in fragment.text and "Verifikat B473" in fragment.text

    pack = client.get(f"/analyser/{item_id}/fil/dd_pack.xlsx")
    assert pack.status_code == 200
    assert load_workbook(io.BytesIO(pack.content)).sheetnames[0] == "Sammanfattning"
    assert client.get(f"/analyser/{item_id}/fil/memo.md").text.startswith("## Sammanfattning")

    # Saved in the workspace: a new app on the same folder lists it.
    with _app(tmp_path / "arbetsyta", demo) as again:
        assert "Demobolaget Konsult AB" in again.get("/analyser").text

    deleted = client.post(f"/analyser/{item_id}/ta-bort")
    assert deleted.status_code == 200 and "Inga analyser ännu" in deleted.text


def test_bad_uploads_give_readable_errors(client: TestClient) -> None:
    nothing = client.post("/analyser", data={})
    assert "Välj en SIE4-fil" in nothing.text

    wrong = client.post("/analyser", files={"fil": ("bild.png", b"\x89PNG not a SIE file")})
    assert wrong.status_code == 200 and "Körningen avbröts" in wrong.text
    assert "Inga analyser ännu" in client.get("/analyser").text  # nothing half saved


def test_statement_rows_agree_with_the_kpis(demo: Path) -> None:
    result = run_analysis(demo, use_llm=False)
    months = result.statements.months
    rows = {row.label: row for row in views.income_rows(result)}
    assert rows["EBITDA"].values == [result.kpis.monthly["ebitda"].values[m] for m in months]
    assert rows["EBITDA"].period == result.bridge.reported
    assert rows["Nettoomsättning"].period == result.bridge.revenue
    assert rows["EBIT"].values == [result.kpis.monthly["ebit"].values[m] for m in months]
    balance = {row.label: row for row in views.balance_rows(result)}
    assert all(v == 0 for v in balance["Differens"].values)
    nwc = {row.label: row for row in views.nwc_rows(result)}
    assert nwc["Rörelsekapital"].values == [result.kpis.nwc.values[m] for m in months]
    chart = views.monthly_chart(result)
    assert len(chart.bars) == len(months) and all(b.height >= 0 for b in chart.bars)
    assert views.memo_html("## Rubrik\n- <b>x</b> **fet**") == (
        "<h3>Rubrik</h3>\n<ul>\n<li>&lt;b&gt;x&lt;/b&gt; <strong>fet</strong></li>\n</ul>"
    )


def _flags(ledger: Ledger, refs: list[str], category: FlagCategory) -> list[Flag]:
    by_ref = {v.ref: v for v in ledger.vouchers}
    flags = []
    for ref in refs:
        main = max(
            (t for t in by_ref[ref].transactions if t.account[0] in "34567"),
            key=lambda t: abs(t.amount),
        )
        flags.append(
            Flag(
                voucher_ref=ref,
                account=main.account,
                amount=main.amount,
                category=category,
                rationale="Test.",
                confidence=0.9,
            )
        )
    return flags


def test_bridge_choice_recomputes_and_follows_into_excel(tmp_path: Path, demo: Path) -> None:
    # An analysis with confident flags on the planted anomalies, saved as the app would.
    ledger, facit = build_demo_company(42)
    a = {x.id: x for x in facit.anomalies}
    result = run_analysis(demo, use_llm=False)
    flags = _flags(ledger, a[2].voucher_refs, "engangspost") + _flags(
        ledger, a[4].voucher_refs, "periodisering"
    )
    result = result.model_copy(update={"bridge": ebitda_bridge(result.kpis, ledger, flags)})
    space = Workspace(tmp_path / "arbetsyta")
    item_id = space.new_id()
    space.upload_path(item_id).write_bytes(demo.read_bytes())
    space.save_analysis(
        AnalysisMeta(
            id=item_id,
            created=dt.datetime(2026, 10, 17, 9, 0),
            source_name="demo_bolag.se",
            company_name=result.company_name,
            org_nr=result.org_nr,
            period="2025-01 till 2025-12",
            basis="LTM",
            use_llm=True,
            model="test",
            reconciled=True,
        ),
        result,
    )
    items = result.bridge.items
    assert [i.category for i in items] == ["engangspost", "engangspost", "periodisering"]

    with _app(tmp_path / "arbetsyta", demo) as client:
        # Switch the revenue item off: the totals come back from with_selection.
        choice = [i.category != "periodisering" for i in items]
        answer = client.post(f"/api/analyser/{item_id}/urval", json={"inkludera": choice})
        assert answer.status_code == 200
        body = answer.json()
        expected = result.bridge.reported + sum(
            i.amount for i in items if i.category != "periodisering"
        )
        assert body["justerad_ebitda"] == views.tsek(expected)
        assert body["justerad_omsattning"] == views.tsek(result.bridge.revenue)
        assert body["andrat"] and not body["memo_aktuellt"]

        page = client.get(f"/analyser/{item_id}/brygga")
        assert views.tsek(expected) in page.text and "Urvalet har ändrats" in page.text

        pack = load_workbook(
            io.BytesIO(client.get(f"/analyser/{item_id}/fil/dd_pack.xlsx").content)
        )
        ws = pack["EBITDA brygga"]
        included = {row[1].value: row[5].value for row in ws.iter_rows(min_row=3) if row[1].value}
        assert included["Periodisering"] == 0 and included["Engångspost"] == 1

        # The memo is rewritten with the choice (template without AI) and is current again.
        client.post(f"/analyser/{item_id}/memo")
        assert "Urvalet i bryggan har ändrats" not in client.get(f"/analyser/{item_id}/memo").text
        assert views.tsek(expected) in client.get(f"/analyser/{item_id}/fil/memo.md").text

        # Back to the proposal clears the choice; a wrong length is refused.
        reset = client.post(
            f"/api/analyser/{item_id}/urval", json={"inkludera": [True, True, True]}
        )
        assert not reset.json()["andrat"]
        assert not reset.json()["memo_aktuellt"]  # the memo was written for the choice
        again = client.post(f"/api/analyser/{item_id}/urval", json={"inkludera": choice})
        assert again.json()["memo_aktuellt"]
        bad = client.post(f"/api/analyser/{item_id}/urval", json={"inkludera": [True]})
        assert bad.status_code == 400 and "poster" in bad.json()["error"]


def test_screening_flow_with_form_tables_and_company_page(client: TestClient) -> None:
    form = client.get("/screening/ny")
    assert "62100 Dataprogrammering" in form.text and "Stockholm" in form.text

    done = client.post(
        "/screening",
        data={
            "sni": "62100",
            "oms_kl": ["6"],
            "anst_kl": ["5"],
            "lan": ["01"],
            "topp": "25",
            "med_bokslut": "1",
        },
    )
    assert done.status_code == 200 and "/screening/" in str(done.url)
    assert "Testbolag 1 AB" in done.text and "Saknas digitalt" in done.text
    assert "Testbolag 3 AB" not in done.text  # not an aktiebolag
    item_id = done.url.path.split("/")[2]

    company = client.get(f"/screening/{item_id}/bolag/5599000001")
    assert company.status_code == 200 and "Nettoomsättning" in company.text
    assert "iXBRL se-gen-base:Nettoomsattning" in company.text
    report = client.get(f"/screening/{item_id}/bolag/5599000001?bokslut=1")
    assert "Årsredovisning 2025-01-01 till 2025-12-31" in report.text
    assert "Soliditet" in report.text

    sheet = client.get(f"/screening/{item_id}/fil/long_list.xlsx")
    assert load_workbook(io.BytesIO(sheet.content)).sheetnames == ["Long list", "Kriterier"]
    assert "SNI 62100" in client.get("/").text  # listed under recent work

    bad = client.post("/screening", data={"sni": "6210"})
    assert "fem siffror" in bad.text


def test_local_only_and_same_site(client: TestClient) -> None:
    foreign_host = client.get("/", headers={"host": "evil.example"})
    assert foreign_host.status_code == 400
    cross_site = client.post(
        "/analyser", data={"demo": "1"}, headers={"origin": "https://evil.example"}
    )
    assert cross_site.status_code == 403
    same_site = client.post("/api/test/scb", headers={"origin": "http://127.0.0.1:8501"})
    assert same_site.status_code == 200 and same_site.json()["ok"]
    assert client.post("/api/test/bolagsverket").json()["ok"]
    assert client.get("/analyser/..%2F..%2Fetc/sammanfattning").status_code == 404


def test_demo_company_runs_from_the_form(client: TestClient) -> None:
    page = client.post("/analyser", data={"demo": "1"})
    assert page.url.path.endswith("/sammanfattning") and "Demobolaget" in page.text
    assert "Utan AI-klassning" in page.text  # no API key in the test settings


class _Throttled:
    """Bolagsverket stand-in that answers 429 until `state["calm"]` is set."""

    def __init__(self, state: dict[str, bool]) -> None:
        self.state = state

    def __enter__(self) -> "_Throttled":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def latest_report(self, org_nr: str) -> None:
        if not self.state["calm"]:
            raise BolagsverketUnavailable("Bolagsverket begränsar antalet anrop just nu.")
        return None


def test_screening_can_fetch_what_bolagsverket_did_not_answer(tmp_path: Path, demo: Path) -> None:
    units, turnover = _units()
    state = {"calm": False}
    app = create_app(
        settings_provider=_settings,
        workspace=tmp_path / "arbetsyta",
        scb_factory=lambda s: fake_scb(units, turnover),  # type: ignore[arg-type]
        bolagsverket_factory=lambda s: _Throttled(state),  # type: ignore[arg-type,return-value]
        inline_jobs=True,
        demo_file=demo,
    )
    with TestClient(app) as client:
        throttled = client.post(
            "/screening", data={"sni": "62100", "oms_kl": ["6"], "med_bokslut": "1"}
        )
        assert "Hämta återstående" in throttled.text and "Kunde inte hämtas" in throttled.text
        item_id = throttled.url.path.split("/")[2]

        state["calm"] = True
        completed = client.post(f"/screening/{item_id}/komplettera")
        assert completed.url.path == f"/screening/{item_id}"
        assert "Hämta återstående" not in completed.text
        assert "Kunde inte hämtas" not in completed.text and "Saknas digitalt" in completed.text
