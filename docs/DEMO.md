# Demo (5 minutes)

Audience: Corporate Finance and Transaction Services advisors. Structure and instructions in
English; the lines to say and the slide are in Swedish, as presented.

Timings measured 2026-10-17 on the demo laptop: `analyze` with AI flagging 33 s,
`analyze --no-llm` 2 s, `screen` (SNI 62100, 61 annual reports) 39 s. Both commands print
"Klart på N s" at the end.

## Before the demo

The day before:

- [ ] `uv sync` and `uv run pytest` pass.
- [ ] `.env` has the Anthropic key, `SCB_API_KEY` and the Bolagsverket credentials.
- [ ] `uv run diligens generate --out data/demo/demo_bolag.se` (seed 42, so the voucher
      numbers below hold).
- [ ] Warm run into a fallback folder, and check both print well under 60 s:

```bash
uv run diligens analyze data/demo/demo_bolag.se --out output/forberedd/
uv run diligens screen --sni 62100 --oms-kl 6 --anst-kl 5 --lan 01 --out output/forberedd/
```

- [ ] Backup recording made (see below).

Fifteen minutes before:

- [ ] Not between 04:00 and 04:30: SCB is down for its nightly update.
- [ ] Excel closed (an open `dd_pack.xlsx` blocks the new one), notifications off.
- [ ] Two terminals side by side, large font, both in the repo folder. Excel ready on the
      other half of the screen.
- [ ] `output/forberedd/` open in Explorer, in case a live step fails.

## Script

| Time | Step | Do |
|---|---|---|
| 0:00 | Problem | Talk |
| 0:30 | Live analysis | Terminal 1 |
| 1:30 | Screening starts | Terminal 2, then go on |
| 1:35 | Excel walkthrough | Excel |
| 3:00 | Long list | Excel |
| 3:45 | Quality | Excel, talk |
| 4:15 | Limitations and next steps | Slide below |

### 0:00 Problem (30 s)

> En finansiell DD på ett ägarlett bolag börjar nästan alltid med en SIE-fil från Fortnox
> eller Visma och några dagar i Excel innan analysen ens har börjat. Diligens gör det
> arbetet på under en minut, och varje siffra går att spåra till ett verifikat.

### 0:30 Live analysis (60 s)

Terminal 1:

```bash
uv run diligens analyze data/demo/demo_bolag.se --out output/
```

While it runs (about 35 s):

> Alla siffror räknas i kod direkt från verifikaten. AI används bara till tre saker: att
> klassa verifikat som möjliga justeringar, att skriva frågor till säljaren och att skriva
> memotexten. Den hittar aldrig på, avrundar eller ändrar en siffra.

When it finishes, point at "Klart på N s", Justerad EBITDA and the DD questions.

### 1:30 Start the screening (5 s)

Terminal 2, then leave it running and go to Excel:

```bash
uv run diligens screen --sni 62100 --oms-kl 6 --anst-kl 5 --lan 01 --out output/
```

> Samtidigt letar vi tilläggsförvärv: bolag inom dataprogrammering i Stockholms län med 20
> till 50 MSEK i omsättning och 20 till 50 anställda. Det får gå i bakgrunden.

### 1:35 Excel walkthrough (85 s)

Open `output/dd_pack.xlsx`.

1. **Sammanfattning:** omsättning, EBITDA, justerad EBITDA.
2. **EBITDA brygga:** each normalization with category, accounts and vouchers. Set
   `Inkludera` to 0 on one row and show the adjusted EBITDA change.
   > Förslagen är förslag. Det är du som avgör vad som ingår, och allt räknas om direkt.
3. **Flaggade poster:** filter on voucher B473, the invoice of 600 TSEK booked on
   2025-12-30 for a January delivery. Show `Motivering` and `Källa`
   (`SIE verifikat B473, konto 3041`).
   > Varje flagga pekar på verifikatserie, nummer och konto. Du kan gå direkt till
   > bokföringen och kontrollera.
4. **DD frågor:** find the question that cites B473 (usually first; the order can vary
   between runs).
   > Frågorna går att skicka till säljaren som de är.

### 3:00 Long list (45 s)

The screening is done by now (about 40 s). Open `output/long_list.xlsx`.

- `Poäng` = `Profil` + `Nyckeltal`, and `Skäl` says why.
- `Källa` points at the SCB field or the iXBRL element and document at Bolagsverket.
- Rows marked "Saknas digitalt".
  > Ungefär vart tredje bolag har lämnat årsredovisningen på papper. Då visas det, och
  > ingen siffra gissas.

### 3:45 Quality (30 s)

- **Avstämning:** every computed balance against the SIE file, difference 0,00.
- The demo company has six planted anomalies with a facit. Live runs found five of six,
  with 94 % precision.
- Low confidence goes to "Kräver granskning" and is never added to adjusted EBITDA on its
  own.

> Underlaget stämmer mot bokföringen på kronan, och det som är osäkert lyfts till dig i
> stället för att räknas in.

### 4:15 Limitations and next steps (45 s)

Show the slide below.

## Slide: Begränsningar och nästa steg

**Begränsningar**

- Pappersinlämnade årsredovisningar finns inte digitalt. Ungefär vart tredje bolag i
  screeningen visas som "Saknas digitalt" och får inga nyckeltalspoäng.
- BAS-mappningen följer standardkontoplanen. Bolagsspecifika konton behöver mappas per
  bolag.
- Kunddata kräver ett avtal om nollagring hos AI-leverantören eller en lokal modell.
  Utan AI körs alla beräkningar lokalt på två sekunder.
- Screeningen bygger på SCB:s storleksklasser och senaste digitala årsredovisning, inte på
  aktuell bokföring.

**Nästa steg**

- Byråns egna mallar för DD-pack och memo.
- Koppling till datarummet så att SIE-filen läses direkt.
- Bolagsspecifik kontomappning som sparas mellan uppdrag.

## If something fails

| Problem | Do |
|---|---|
| No network or the AI call fails | `analyze ... --no-llm` (2 s). Flags and questions are then left out; say so. |
| `analyze` says the file is locked | Close Excel and run again. |
| SCB down (04:00 window) or Bolagsverket throttles | Open `output/forberedd/long_list.xlsx`. |
| Anything else | Play the backup recording. |

## Backup recording

Record one full rehearsal with the Windows Snipping Tool (Win+Shift+R, select the whole
screen, so terminal and Excel are both visible). Save it as `output/demo.mp4`; `output/` is
not committed. Watch it once to check sound and readability.

## Rehearsals

| Date | Total time | `analyze` | `screen` | Notes |
|---|---|---|---|---|
| | | | | |
