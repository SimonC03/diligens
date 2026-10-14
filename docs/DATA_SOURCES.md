# Data sources

Verify endpoints against the official docs before building, and update this file. Mark
each fact as verified (with source URL) or unverified.

## SCB företagsregister (free)

- Docs: https://www.scb.se/vara-tjanster/bestall-data-och-statistik/foretagsregistret/avgiftsfria-uppgifter-i-foretagsregistret/
- OpenAPI: https://apiafr.scb.se/scalar/
- Auth: API key from the registration portal (https://registreraafr.scb.se/). The key is
  personal; never share or commit it. Env var `SCB_API_KEY`.
- Limits: max 5 000 rows per call with pagination, max 5 calls per second per key.
- Content: legal entities and workplaces, SNI code, address, employees as size class.
  Current data only, no history. Updated nightly except Saturday to Sunday.
- Use: candidate lists for screening (SNI, size class, county).

## Bolagsverket API för värdefulla datamängder (free)

- Docs: https://bolagsverket.se/apierochoppnadata/hamtaforetagsinformation/vardefulladatamangder/apiforvardefulladatamangder.5513.html
- Free, no agreement. Credentials after a customer registration (e-mail and phone).
  Env vars `BOLAGSVERKET_CLIENT_ID`, `BOLAGSVERKET_CLIENT_SECRET`, `BOLAGSVERKET_ENV`.
- REST with JSON; annual reports delivered as iXBRL.
- Lookup is by organisation number. There is no name or industry search in this API, so
  SCB provides the candidate list.
- Only digitally filed annual reports are included. Paper filed reports are missing: show
  "Saknas digitalt", never estimate.
- Endpoints seen in third party clients (verify): token
  `https://portal.api.bolagsverket.se/oauth2/token`, base
  `https://gw.api.bolagsverket.se/vardefulla-datamangder/v1`, test environment under
  `accept2`. Operations for organisations, document list and document download (zip).
- The full "API för att hämta företagsinformation" costs money and needs an agreement.
  Do not use it.

## Bolagsverket bulk file (free, no registration)

- Weekly updated zip with basic company data (`bolagsverket_bulkfil.zip`), unpacks to txt.
- Docs: https://bolagsverket.se/apierochoppnadata/nedladdningsbarafiler.2517.html
- Fallback for name and org number lookups if the API credentials are delayed.

## Not used

- Allabolag, Proff, Retriever: scraping is against their terms. Do not add.
