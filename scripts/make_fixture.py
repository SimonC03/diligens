"""Write tests/fixtures/minimal.se in CP437. Run once; the output is committed.

The expected parse result lives in tests/fixtures/minimal_expected.json.
"""

from pathlib import Path

SIE = """#FLAGGA 0
#PROGRAM "Diligens fixture" 1.0
#FORMAT PC8
#GEN 20260101
#SIETYP 4
#FNAMN "Testbolaget Åkeri AB"
#ORGNR 999999-9999
#RAR 0 20250101 20251231
#KPTYP BAS2014
#KONTO 1510 "Kundfordringar"
#KONTO 1930 "Företagskonto"
#KONTO 2081 "Aktiekapital"
#KONTO 2440 "Leverantörsskulder"
#KONTO 2611 "Utgående moms på försäljning inom Sverige, 25 %"
#KONTO 2641 "Debiterad ingående moms"
#KONTO 3041 "Försäljning tjänster 25 % sv"
#KONTO 5010 "Lokalhyra"
#KONTO 6550 "Konsultarvoden"
#KONTO 7010 "Löner till kollektivanställda"
#KTYP 1930 T
#IB 0 1930 100000.00
#IB 0 2081 -100000.00
#UB 0 1510 0.00
#UB 0 1930 145000.00
#UB 0 2081 -100000.00
#UB 0 2440 -25000.00
#UB 0 2611 -25000.00
#UB 0 2641 15000.00
#RES 0 3041 -100000.00
#RES 0 5010 20000.00
#RES 0 6550 40000.00
#RES 0 7010 30000.00
#VER A 1 20250115 "Faktura kund 1001"
{
#TRANS 1510 {} 125000.00
#TRANS 3041 {} -100000.00
#TRANS 2611 {} -25000.00
}
#VER A 2 20250131 "Betalning kund 1001"
{
#TRANS 1930 {} 125000.00
#TRANS 1510 {} -125000.00
}
#VER B 1 20250201 "Hyra februari"
{
#TRANS 5010 {} 20000.00
#TRANS 2641 {} 5000.00
#TRANS 2440 {} -25000.00
}
#VER B 2 20250315 "Konsultarvode \\"Ägarbolaget AB\\"" 20250316
{
#TRANS 6550 {} 40000.00
#TRANS 2641 {} 10000.00
#TRANS 1930 {} -50000.00 20250315 "Utbetalning"
}
#VER C 1 20250325 "Löner mars"
{
#TRANS 7010 {} 30000.00
#TRANS 1930 {} -30000.00
}
#OKANDTAGG 1 2 3
"""


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "minimal.se"
    out.write_bytes(SIE.replace("\n", "\r\n").encode("cp437"))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
