# Demo (5 minutes)

Audience: Corporate Finance and Transaction Services advisors. Structure and instructions in
English; the lines to say and the slide are in Swedish, as presented.

Timings measured 2026-10-17 on the demo laptop, wall clock: `analyze` with AI flagging 33 s,
`analyze --no-llm` about 2 s, `screen` (SNI 62100, 61 annual reports) 39 s. Both commands
end with "Klart på N s", which counts the run itself and not Python start-up, so it shows a
little less (`--no-llm` prints well under a second).

## Before the demo

The day before:

- [ ] `uv sync` and `uv run pytest` pass.
- [ ] `.env` has the Anthropic key, `SCB_API_KEY` and the Bolagsverket credentials.
- [ ] `uv run diligens generate --out data/demo/demo_bolag.se` (seed 42, so the voucher
      numbers below hold).
- [ ] Warm run with AI into a fallback folder, and check both stay well under 60 s:

```bash
uv run diligens analyze data/demo/demo_bolag.se --out output/forberedd/
uv run diligens screen --sni 62100 --oms-kl 6 --anst-kl 5 --lan 01 --out output/forberedd/
```

- [ ] Open `output/forberedd/dd_pack.xlsx` and note the adjusted EBITDA. It can differ
      between runs: the owner fee is sometimes classed as related party and then starts at
      Inkludera 0. Never quote a fixed figure from memory.
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
> arbetet på under en minut, och varje siffra går att spåra till konto och verifikat i
> bokföringen.

### 0:30 Live analysis (60 s)

Terminal 1:

```bash
uv run diligens analyze data/demo/demo_bolag.se --out output/
```

While it runs (about 35 s):

> Alla belopp räknas i kod direkt från bokföringen. AI används till tre saker: att föreslå
> vilka verifikat som kan vara justeringar, att formulera frågor till säljaren och att
> skriva memotexten. AI:n skriver aldrig en siffra; alla belopp sätts in av koden.

When it finishes, point at "Klart på N s", Justerad EBITDA and the DD questions.

### 1:30 Start the screening (5 s)

Terminal 2, then leave it running and go to Excel:

```bash
uv run diligens screen --sni 62100 --oms-kl 6 --anst-kl 5 --lan 01 --out output/
```

> Samtidigt rangordnar vi tilläggsförvärv inom dataprogrammering. Målet är Stockholms län,
> 20 till 50 MSEK i omsättning och 20 till 49 anställda. Bolag i andra län eller en
> storleksklass ifrån följer längre ner. Det får gå i bakgrunden.

### 1:35 Excel walkthrough (85 s)

Open `output/dd_pack.xlsx`.

1. **Sammanfattning:** omsättning, EBITDA, justerad EBITDA.
2. **EBITDA brygga:** each normalization with category, accounts and vouchers. Set
   `Inkludera` to 0 on the row "Engångspost: Lokalhyra" (240 TSEK) and show adjusted EBITDA
   drop by 240.
   > Justeringarna är förslag. Det är du som avgör vad som ingår, och Excel räknas om
   > direkt.
3. **Flaggade poster:** filter on voucher B473, an invoice booked on 2025-12-30 for a
   January delivery: 600 TSEK exkl. moms. Show `Motivering` and `Källa`
   (`SIE verifikat B473, konto 3041`).
   > Varje flagga pekar på verifikatserie, nummer och konto. Du kan gå direkt till
   > bokföringen och kontrollera.
4. **DD frågor:** find the question that cites B473 (usually first; the order can vary
   between runs).
   > Frågorna är formulerade för säljaren och kräver bara en genomläsning.

### 3:00 Long list (45 s)

The screening is done by now (about 40 s). Open `output/long_list.xlsx`.

- `Poäng` = `Profil` + `Nyckeltal`, and `Skäl` says why.
- `Källa` points at the SCB field or the iXBRL element and document at Bolagsverket.
- Rows marked "Saknas digitalt".
  > I den här sökningen saknade ungefär vart tredje bolag en digital årsredovisning. Då
  > visas det, och ingen siffra gissas.

### 3:45 Quality (30 s)

- **Avstämning:** each account's annual result and closing balance against the SIE file's
  #RES and #UB, difference 0,00.
- Low confidence goes to "Kräver granskning" and is never added to adjusted EBITDA on its
  own.

> Underlaget stämmer mot bokföringens saldon på kronan. I två testkörningar på det
> syntetiska demobolaget hittades fem av sex inplanterade avvikelser; den sjätte,
> privatbilen, hamnade under Kräver granskning. 94 % av de säkra flaggorna träffade en
> inplanterad post. Det är inget mått på träffsäkerhet i riktiga bokslut, men visar att det
> osäkra lyfts till dig i stället för att räknas in.

### 4:15 Limitations and next steps (45 s)

Show the slide below.

## Slide: Begränsningar och nästa steg

**Begränsningar**

- Senaste året är inte bokslutat: bokslutsposter saknas och skatten i nettoskulden är
  uppskattad.
- Ägarrelaterade kostnader återläggs i sin helhet; marknadsmässig ersättning uppskattas
  inte.
- BAS-mappningen följer standardkontoplanen. Bolagsspecifika konton behöver mappas per
  bolag.
- Kunddata kräver ett avtal om nollagring hos AI-leverantören. Utan AI körs alla
  beräkningar lokalt på några sekunder, men då finns inga förslag till justeringar, bara
  poster för granskning.
- Bolag utan digital årsredovisning får inga nyckeltalspoäng. Screeningen använder
  rörelsemarginal, inte EBITDA, och senaste digitala årsredovisning kan vara flera år
  gammal.

**Nästa steg**

- Lokal modell för kunddata som inte får lämna byrån.
- Byråns egna mallar för DD-pack och memo.
- Koppling till datarummet så att SIE-filen läses direkt.
- Bolagsspecifik kontomappning som sparas mellan uppdrag.

## If something fails

| Problem | Do |
|---|---|
| No network or the AI call fails | Do the Excel walkthrough from `output/forberedd/dd_pack.xlsx` (built with AI the day before). If a live run is needed, `analyze ... --no-llm` takes a few seconds, but the bridge then has no adjustments and every flag sits under Kräver granskning; say "Utan AI finns inga förslag till justeringar, bara poster för granskning". |
| `analyze` says the file is locked | Close Excel and run again. |
| SCB down (04:00 window) or Bolagsverket throttles | Open `output/forberedd/long_list.xlsx`. |
| Anything else | Play the backup recording. |

## Questions to be ready for

- **Rörelsekapitalet** at year end is far above the normal level, partly because the B473
  receivable (750 TSEK incl. moms) is still in it. Normalizations adjust EBITDA only, not
  working capital.
- **Avstämningen** is against the SIE file's balances, not against the annual report or
  the bank.
- **Löneposter:** the AI runs usually include one or two questions on the July payroll
  runs (holiday pay reversal). They are reasonable questions, not planted anomalies.

## Backup recording

Record one full rehearsal with the Windows Snipping Tool (Win+Shift+R, select the whole
screen, so terminal and Excel are both visible). Save it as `output/demo.mp4`; `output/` is
not committed. Watch it once to check sound and readability.

## Rehearsals

| Date | Total time | `analyze` | `screen` | Notes |
|---|---|---|---|---|
| | | | | |
