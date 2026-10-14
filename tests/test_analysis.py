"""M2 analysis tests. One test per acceptance criterion in docs/PLAN.md."""

from pathlib import Path

from diligens.analysis.mapping import map_accounts
from diligens.models import Account
from diligens.sie.parser import parse_sie

FIXTURES = Path(__file__).parent / "fixtures"
PAYABLES = "Leverantörsskulder och kortfristiga skulder till kreditinstitut"


def test_bas_mapping_returns_unmapped() -> None:
    ledger = parse_sie(FIXTURES / "minimal.se")

    mapping = map_accounts(ledger)

    assert {a: (m.line, m.statement) for a, m in mapping.accounts.items()} == {
        "1510": ("Kundfordringar", "BR"),
        "1930": ("Kassa och bank", "BR"),
        "2081": ("Eget kapital", "BR"),
        "2440": (PAYABLES, "BR"),
        "2611": ("Moms och punktskatter", "BR"),
        "2641": ("Moms och punktskatter", "BR"),
        "3041": ("Nettoomsättning", "RR"),
        "5010": ("Övriga externa kostnader", "RR"),
        "6550": ("Övriga externa kostnader", "RR"),
        "7010": ("Personalkostnader", "RR"),
    }
    assert mapping.unmapped == []

    boundaries = {
        "1000": "Anläggningstillgångar",
        "1399": "Anläggningstillgångar",
        "1400": "Lager",
        "1999": "Kassa och bank",
        "2000": "Eget kapital",
        "2999": "Upplupna kostnader och förutbetalda intäkter",
        "3000": "Nettoomsättning",
        "3699": "Nettoomsättning",
        "3700": "Övriga rörelseintäkter",
        "3740": "Övriga rörelseintäkter",
        "4999": "Råvaror, handelsvaror och underentreprenader",
        "6999": "Övriga externa kostnader",
        "7699": "Personalkostnader",
        "7700": "Av och nedskrivningar",
        "7899": "Av och nedskrivningar",
        "7900": "Övriga rörelsekostnader",
        "8799": "Finansiella poster",
        "8899": "Bokslutsdispositioner",
        "8999": "Skatt och årets resultat",
    }
    names = [*boundaries, "0100", "9999", "ABC"]
    with_edges = ledger.model_copy(
        update={"accounts": {n: Account(number=n, name="x") for n in names}}
    )

    mapped = map_accounts(with_edges)

    for number, line in boundaries.items():
        assert mapped.accounts[number].line == line, number
    # Accounts only seen in transactions are mapped too.
    assert "3041" in mapped.accounts
    assert mapped.unmapped == ["0100", "9999", "ABC"]
