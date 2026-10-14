"""SIE4 parser, see docs/DOMAIN.md section 1."""

import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

from diligens.models import (
    Account,
    Balance,
    FiscalYear,
    IntegrityIssue,
    Ledger,
    Transaction,
    Voucher,
)

Token = str | list[str]


class SieParseError(ValueError):
    """A known record is malformed. Carries the line number for the user."""

    def __init__(self, line_no: int, message: str) -> None:
        super().__init__(f"rad {line_no}: {message}")
        self.line_no = line_no


def _tokenize(line: str) -> list[Token]:
    """Split a SIE line into fields. Quoted strings unescape `\\"`; `{...}` becomes a list."""
    tokens: list[Token] = []
    group: list[str] | None = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "{":
            group = []
            i += 1
            continue
        if ch == "}":
            tokens.append(group if group is not None else [])
            group = None
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(line[i])
                i += 1
            i += 1  # closing quote
            value = "".join(buf)
        else:
            start = i
            while i < n and not line[i].isspace() and line[i] not in '{}"':
                i += 1
            value = line[start:i]
        if group is not None:
            group.append(value)
        else:
            tokens.append(value)
    if group is not None:
        tokens.append(group)
    return tokens


def _str(tokens: list[Token], idx: int, line_no: int) -> str:
    if idx >= len(tokens):
        raise SieParseError(line_no, f"fält {idx} saknas")
    tok = tokens[idx]
    if isinstance(tok, list):
        raise SieParseError(line_no, f"fält {idx} är en objektlista, väntade värde")
    return tok


def _opt_str(tokens: list[Token], idx: int) -> str | None:
    if idx >= len(tokens):
        return None
    tok = tokens[idx]
    return tok if isinstance(tok, str) and tok != "" else None


def _date(raw: str, line_no: int) -> dt.date:
    try:
        return dt.datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise SieParseError(line_no, f"ogiltigt datum {raw!r}") from exc


def _amount(raw: str, line_no: int) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise SieParseError(line_no, f"ogiltigt belopp {raw!r}") from exc


def _balance(tokens: list[Token], line_no: int) -> Balance:
    return Balance(
        year_index=int(_str(tokens, 1, line_no)),
        account=_str(tokens, 2, line_no),
        amount=_amount(_str(tokens, 3, line_no), line_no),
    )


def _transaction(tokens: list[Token], line_no: int) -> Transaction:
    # #TRANS account {objects} amount [date] [text] [quantity] [sign]
    account = _str(tokens, 1, line_no)
    if len(tokens) < 4 or not isinstance(tokens[2], list):
        raise SieParseError(line_no, "#TRANS saknar objektlista eller belopp")
    amount = _amount(_str(tokens, 3, line_no), line_no)
    raw_date = _opt_str(tokens, 4)
    return Transaction(
        account=account,
        amount=amount,
        date=_date(raw_date, line_no) if raw_date else None,
        text=_opt_str(tokens, 5),
    )


# Tags that are recognised but carry nothing the ledger needs yet.
_IGNORED_TAGS = {"#FLAGGA", "#FORMAT", "#PROGRAM", "#GEN", "#KPTYP", "#KTYP", "#PSALDO"}


def parse_sie(path: Path) -> Ledger:
    """Parse a SIE4 file (CP437) into a Ledger. Unknown tags become warnings."""
    lines = path.read_text(encoding="cp437").splitlines()

    company_name = ""
    org_nr = ""
    fiscal_years: list[FiscalYear] = []
    accounts: dict[str, Account] = {}
    opening: list[Balance] = []
    closing: list[Balance] = []
    results: list[Balance] = []
    vouchers: list[Voucher] = []
    warnings: list[str] = []

    pending: list[Token] | None = None  # #VER header waiting for its block
    pending_line = 0
    block: list[Transaction] | None = None

    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue

        if line == "{":
            if pending is None or block is not None:
                raise SieParseError(line_no, "'{' utan föregående #VER")
            block = []
            continue
        if line == "}":
            if pending is None or block is None:
                raise SieParseError(line_no, "'}' utan öppnat verifikat")
            vouchers.append(_voucher(pending, block, pending_line))
            pending, block = None, None
            continue

        tokens = _tokenize(line)
        tag = _str(tokens, 0, line_no).upper()

        if block is not None:
            if tag == "#TRANS":
                block.append(_transaction(tokens, line_no))
            elif tag in {"#RTRANS", "#BTRANS"}:
                # Supplementary/removed rows; #TRANS carries the booked amounts.
                continue
            else:
                warnings.append(f"rad {line_no}: okänd tagg {tag} i verifikat ignorerad")
            continue
        if pending is not None:
            raise SieParseError(pending_line, "#VER saknar '{'-block")

        if tag == "#SIETYP":
            sie_type = _str(tokens, 1, line_no)
            if sie_type != "4":
                raise SieParseError(line_no, f"SIE typ {sie_type} stöds inte, endast typ 4")
        elif tag == "#FNAMN":
            company_name = _str(tokens, 1, line_no)
        elif tag == "#ORGNR":
            org_nr = _str(tokens, 1, line_no)
        elif tag == "#RAR":
            fiscal_years.append(
                FiscalYear(
                    index=int(_str(tokens, 1, line_no)),
                    start=_date(_str(tokens, 2, line_no), line_no),
                    end=_date(_str(tokens, 3, line_no), line_no),
                )
            )
        elif tag == "#KONTO":
            number = _str(tokens, 1, line_no)
            accounts[number] = Account(number=number, name=_opt_str(tokens, 2) or "")
        elif tag == "#IB":
            opening.append(_balance(tokens, line_no))
        elif tag == "#UB":
            closing.append(_balance(tokens, line_no))
        elif tag == "#RES":
            results.append(_balance(tokens, line_no))
        elif tag == "#VER":
            pending, pending_line = tokens, line_no
        elif tag in _IGNORED_TAGS:
            continue
        else:
            warnings.append(f"rad {line_no}: okänd tagg {tag} ignorerad")

    if pending is not None:
        raise SieParseError(pending_line, "#VER avslutas inte med '}'")

    return Ledger(
        company_name=company_name,
        org_nr=org_nr,
        fiscal_years=fiscal_years,
        accounts=accounts,
        opening_balances=opening,
        closing_balances=closing,
        results=results,
        vouchers=vouchers,
        warnings=warnings,
    )


def _voucher(header: list[Token], transactions: list[Transaction], line_no: int) -> Voucher:
    # #VER series number date [text] [regdate] [sign]
    raw_reg = _opt_str(header, 5)
    return Voucher(
        series=_str(header, 1, line_no),
        number=_str(header, 2, line_no),
        date=_date(_str(header, 3, line_no), line_no),
        text=_opt_str(header, 4) or "",
        reg_date=_date(raw_reg, line_no) if raw_reg else None,
        transactions=transactions,
    )


def check_integrity(ledger: Ledger) -> list[IntegrityIssue]:
    """Return every integrity violation from DOMAIN.md section 1. Never raises."""
    raise NotImplementedError("M1: check_integrity")
