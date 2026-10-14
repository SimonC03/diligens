"""BAS account to statement line, per docs/DOMAIN.md section 2."""

from diligens.models import Ledger, MappedAccount, Mapping, StatementKind

# (first account, last account, line, statement), in account order.
BAS_LINES: tuple[tuple[int, int, str, StatementKind], ...] = (
    (1000, 1399, "Anläggningstillgångar", "BR"),
    (1400, 1499, "Lager", "BR"),
    (1500, 1599, "Kundfordringar", "BR"),
    (1600, 1799, "Övriga kortfristiga fordringar och förutbetalda kostnader", "BR"),
    (1800, 1899, "Kortfristiga placeringar", "BR"),
    (1900, 1999, "Kassa och bank", "BR"),
    (2000, 2099, "Eget kapital", "BR"),
    (2100, 2199, "Obeskattade reserver", "BR"),
    (2200, 2299, "Avsättningar", "BR"),
    (2300, 2399, "Långfristiga skulder", "BR"),
    (2400, 2499, "Leverantörsskulder och kortfristiga skulder till kreditinstitut", "BR"),
    (2500, 2599, "Skatteskulder", "BR"),
    (2600, 2699, "Moms och punktskatter", "BR"),
    (2700, 2799, "Personalens skatter och avgifter", "BR"),
    (2800, 2899, "Övriga kortfristiga skulder", "BR"),
    (2900, 2999, "Upplupna kostnader och förutbetalda intäkter", "BR"),
    (3000, 3699, "Nettoomsättning", "RR"),
    (3700, 3999, "Övriga rörelseintäkter", "RR"),
    (4000, 4999, "Råvaror, handelsvaror och underentreprenader", "RR"),
    (5000, 6999, "Övriga externa kostnader", "RR"),
    (7000, 7699, "Personalkostnader", "RR"),
    (7700, 7899, "Av och nedskrivningar", "RR"),
    (7900, 7999, "Övriga rörelsekostnader", "RR"),
    (8000, 8799, "Finansiella poster", "RR"),
    (8800, 8899, "Bokslutsdispositioner", "RR"),
    (8900, 8999, "Skatt och årets resultat", "RR"),
)


def _line_for(account: str) -> tuple[str, StatementKind] | None:
    if not (len(account) == 4 and account.isdigit()):
        return None
    number = int(account)
    for first, last, line, statement in BAS_LINES:
        if first <= number <= last:
            return line, statement
    return None


def _all_accounts(ledger: Ledger) -> set[str]:
    accounts = set(ledger.accounts)
    for balances in (ledger.opening_balances, ledger.closing_balances, ledger.results):
        accounts.update(b.account for b in balances)
    for voucher in ledger.vouchers:
        accounts.update(t.account for t in voucher.transactions)
    return accounts


def map_accounts(ledger: Ledger) -> Mapping:
    """Map every account seen in the ledger; accounts outside the BAS ranges go to `unmapped`."""
    mapped: dict[str, MappedAccount] = {}
    unmapped: list[str] = []
    for account in sorted(_all_accounts(ledger)):
        hit = _line_for(account)
        if hit is None:
            unmapped.append(account)
        else:
            mapped[account] = MappedAccount(account=account, line=hit[0], statement=hit[1])
    return Mapping(accounts=mapped, unmapped=unmapped)
