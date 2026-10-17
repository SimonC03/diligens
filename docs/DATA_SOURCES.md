# Data sources

Verify endpoints against the official docs before building, and update this file. Mark
each fact as verified (with source URL) or unverified.

## SCB företagsregister, AFR API v1 (free)

Verified 2026-10-14 against the published OpenAPI 3.0.4 spec.

- Docs: https://www.scb.se/vara-tjanster/bestall-data-och-statistik/foretagsregistret/avgiftsfria-uppgifter-i-foretagsregistret/
- Reference UI: https://apiafr.scb.se/scalar/
- Spec: https://apiafr.scb.se/swagger/v1/swagger.json (also served at `/openapi.json`).
  First step of M6: download it to `tests/fixtures/scb/openapi.json` and test the client's
  paths and models against it.
- Base URL: `https://apiafr.scb.se`. Version in the path: `/v1/...`. The spec has no
  `servers` block.
- Auth: header `X-API-Key: <key>` on every business endpoint. Key from the registration
  portal (https://registreraafr.scb.se/), personal, never shared, logged or committed.
  Env var `SCB_API_KEY`.
- Limits: max 5 calls per second per key; `limit` max 5 000. HTTP 429 when throttled, so
  back off and retry. Unavailable 30 minutes from 04:00 every night during the data update.
- Errors: `application/problem+json` (ProblemDetails: `type`, `title`, `status`, `detail`,
  `instance`). Expect 400, 401, 404, 429, 500, 503.
- Data: current state only, no history. Dates RFC 3339 in UTC.

### Pagination

Cursor based. First call without `cursorId`, then pass `pagination.nextCursorId` as
`cursorId` with the same `limit` until `pagination.hasMore` is false.
`cursorPaginationResponseData`: `hasMore` bool, `limit` int, `nextCursorId` int.

### Endpoints (JE = juridisk enhet, AE = arbetsställe)

All `GET`. Each filter endpoint takes exactly one filter; there is no combined query.

| Path | Returns |
|---|---|
| `/v1/juridiskaenheter` | `jePartialDataCollectionResponseData` (`jes`, `pagination`) |
| `/v1/juridiskaenheter/full` | `jeFullDataCollectionResponseData` |
| `/v1/juridiskaenheter/{peOrgNr}` | `jePartialData` |
| `/v1/juridiskaenheter/{peOrgNr}/full` | `jeFullData` |
| `/v1/juridiskaenheter/naringsgren/{naringsgrenKod}` | JE list, primary industry only (rangordning 1) |
| `/v1/juridiskaenheter/omsattningsklass/{omsKlKod}` | JE list by turnover class (int) |
| `/v1/juridiskaenheter/anstalldaklass/{anstKlKod}` | JE list by employee class (int) |
| `/v1/juridiskaenheter/lan/{lanKod}` | JE list by county of seat |
| `/v1/juridiskaenheter/kommun/{kommunKod}` | JE list by municipality of seat |
| `/v1/juridiskaenheter/juridiskform/{jurFormKod}` | JE list by legal form |
| `.../count` on every list path above | `countResponseData` (`count`, `path`) |
| `/v1/arbetsstallen[...]` | Same pattern for workplaces: `full`, `{cfarNr}`, `{cfarNr}/full`, `naringsgren`, `anstalldaklass`, `lan`, `kommun`, `count` |
| `/v1/kodtabeller/<table>` | Code tables, each `[{kod, klartext}]` |
| `/v1/api-info` | `apiNamn`, `senasteUppdateringsDatum` |

Code tables: `naringsgrenkoder` (also `avdelningKod`, `avdelningText`), `omsklkoder`,
`anstklkoder`, `jurformkoder`, `lankoder`, `kommunkoder`, `ftgstatkoder`, `bolstatkoder`,
`momsstatkoder`, `fskattstatkoder`, `arbgivstatkoder`, `agarkontrollkoder`, `sektorkoder`,
`privpublkoder`, `aestatkoder`, `aetypkoder`, `hjverksjekoder`, `epostsparrtypkoder`,
`reklamsparrtypkoder`, `telefonsparrtypkoder`.

### Models

- `jePartialData`: `peOrgNr`, `orgNr`, `namn`, `jurform`, `ftgStat`, `anstKl`,
  `kommunSate`, `lanSate`, `postAdress`, `primarNaringsgren`, spärr fields.
- `jeFullData` adds: `foretagsnamn`, `omsKl` (turnover class), `omsAr` (year of the turnover
  figure), `aeAnt` (number of workplaces), `naringsgrenar[]`, `agKat` (ownership),
  `bolStat`, `momsStat`, `fSkattStat`, `arbGivStat`, `sektor`, `privPubl`, `expImp`,
  `startDat`, `regDat`, `slutDat`, `epost`, `tel`.
- `naringsgrenData`: `naringsgren` (code), `avdelningsKod`, `rangordning`, `andelProcent`.
- `postAdressData`: `coAdress`, `gatuAdress`, `postNr`, `postOrt`.
- Codes in responses are strings even where the filter path takes an int (`anstKl`,
  `omsKl`). Map through the code tables, never hardcode class boundaries.

### How screening uses it

1. Call `naringsgren/{kod}` for each target SNI code, paginate fully.
2. Fetch `/full` per candidate org number (respect 5 calls/s), or the `/full` list when
   the population is large.
3. Filter in code on `omsKl`, `anstKl`, `lanSate`, `ftgStat` (active only) and `jurform`
   (aktiebolag for annual reports).
4. Hand org numbers to Bolagsverket for annual reports.

Unverified until tested with a key: the exact `naringsgrenKod` format (5 digit SNI 2007 or
SNI 2025), whether `peOrgNr` takes 10 or 12 digits and with or without hyphen, the
`omsKl` and `anstKl` class boundaries. Check the code tables first and record the answers
here.

## Bolagsverket API för värdefulla datamängder (free)

- Docs: https://bolagsverket.se/apierochoppnadata/hamtaforetagsinformation/vardefulladatamangder/apiforvardefulladatamangder.5513.html
- Free, no agreement. Credentials after a customer registration (e-mail and phone).
  Env vars `BOLAGSVERKET_CLIENT_ID`, `BOLAGSVERKET_CLIENT_SECRET`, `BOLAGSVERKET_ENV`.
- REST with JSON; annual reports delivered as iXBRL.
- Lookup is by organisation number. There is no name or industry search in this API, so
  SCB provides the candidate list.
- Only digitally filed annual reports are included. Paper filed reports are missing: show
  "Saknas digitalt", never estimate.
- Verified 2026-10-17 with our credentials (spec from the Developer Portal REST API,
  `https://portal.api.bolagsverket.se/api/am/devportal/v3/apis/ff8f9a91-1fdd-4705-8836-c1906581162f/swagger`):
  - Base `https://gw.api.bolagsverket.se/vardefulla-datamangder/v1`, the only environment
    listed ("Default"). `BOLAGSVERKET_ENV` has no effect on this API.
  - The token must request the scopes `vardefulla-datamangder:read` (data) and
    `vardefulla-datamangder:ping` (`/isalive`); without a scope the token only gets
    `default`. Tokens last 3600 s.
  - `GET /isalive` returns 200 "OK".
  - `POST /organisationer` with `{"identitetsbeteckning": "5299999994"}` (the spec's test
    number, ten digits without dash) returns 200 with `organisationer[]`. Fields include
    `organisationsnamn`, `organisationsform`, `juridiskForm`, `naringsgrenOrganisation`
    (SNI), `postadressOrganisation`, `verksamOrganisation`, `avregistreradOrganisation`,
    `verksamhetsbeskrivning`, `organisationsdatum`.
  - `POST /dokumentlista` with the same body returns 200 with `dokument[]`;
    `GET /dokument/{dokumentId}` downloads one document (annual report zip).
  - Errors use the `ApiError` schema.

### Access (credentials received 2026-10-17)

Confirmed by Bolagsverket when the credentials were issued:

- Application with access to the API `VärdefullaDatamängder`. API description on the
  Developer Portal: https://portal.api.bolagsverket.se/devportal/apis/ff8f9a91-1fdd-4705-8836-c1906581162f
  (all APIs: https://portal.api.bolagsverket.se/devportal/apis).
- Access token through OAuth2 Client Credentials Grant
  (https://datatracker.ietf.org/doc/html/rfc6749#section-4.4). Token endpoint:
  `https://portal.api.bolagsverket.se/oauth2/token`.
- Credentials arrive as an encrypted zip with two lines: client id, then client secret. The
  password comes by separate SMS. They go into `.env` only (`BOLAGSVERKET_CLIENT_ID`,
  `BOLAGSVERKET_CLIENT_SECRET`), never into the repo, logs or chat.
- An inactive account is closed after 6 months. Make at least one call before 2027-04-17,
  then keep it in use.
- Report lost or leaked credentials to Bolagsverket at once (contact form below).
- Service status: https://bolagsverket.se/driftsinformation.232.html
- News and releases (RSS): https://bolagsverket.se/apierochoppnadata/nyheterochreleaserforvaraapier/prenumererapanyheterochreleaserviarssflode.5499.html
- Questions and fault reports: https://www.bolagsverket.se/apierochoppnadata/apiforaktuellforetagsinformationbetaversion/kontaktaossomapietmedaktuellforetagsinformationbetaversionxmlochaviseringar.3996.html
- The full "API för att hämta företagsinformation" costs money and needs an agreement.
  Do not use it.

## Bolagsverket bulk file (free, no registration)

- Weekly updated zip with basic company data (`bolagsverket_bulkfil.zip`), unpacks to txt.
- Docs: https://bolagsverket.se/apierochoppnadata/nedladdningsbarafiler.2517.html
- Fallback for name and org number lookups if the API credentials are delayed.

## Not used

- Allabolag, Proff, Retriever: scraping is against their terms. Do not add.
