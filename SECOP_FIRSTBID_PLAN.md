# SECOP II First-Bid Submitter — Implementation Plan (Python)

**Audience:** the coding agent that will build this app.
**Author's note:** every endpoint, field name, and formula below is taken from the SECOP II
e-auction SPA's own JavaScript (`subastas.secop.gov.co/eauctions.site/…`, ~2020 AngularJS 1.x)
and from a real auction HAR capture. Where a fact comes only from client code (not observed on
the wire) or only from the single captured **test** auction, it is marked. Read those markers —
they are where bugs will hide.

---

## 0. What the app does (scope)

A **single-shot, scheduled, multi-threaded first-bid submitter**. Given a JWT harvested from a
logged-in browser session, a saved auction page, one bid value, and **one operator-specified fire
instant**, it:

1. authenticates with the harvested JWT,
2. extracts the auction identifiers from the page,
3. retrieves the auction detail and item list,
4. builds one bid payload, applying the single input value to **all** items,
5. schedules the fire instant from the **manually supplied `fire_time`** on the local monotonic clock —
   server-time probes run as **diagnostics only** and never move `T` (§9),
6. at that instant, fires **N concurrent threads** in configurable roles (direct-submit,
   validate-then-submit, validate-only),
7. records the first accepted bid and logs everything in exhaustive detail.

It places **one logical first bid** (one value) and stops — no bid-down loop, no re-bidding. But *how* that bid is fired is configurable via **firing modes** (§11.4): a single wave at the open instant, or a specific number of timed attempts starting some milliseconds **before** the open, or a fully custom schedule of offsets. Every attempt carries the same value; only one can succeed (the rest bounce as duplicates or too-early).

**Non-goals:** obtaining the JWT (done manually in a browser), bidding strategy beyond "place this
value," multi-round auctions.

---

## 1. Environment, base URL, transport

| Item | Value |
|---|---|
| API host | `https://subastas.secop.gov.co` |
| API base path | `/eauctions.api/v1//` — **the double slash after `v1` is real.** The app builds `serviceBase = AppSettings.apiBaseUrl + AppSettings.apiVersion + '/'` and the observed wire paths are e.g. `/eauctions.api/v1//bid/ValidateBid`. Replicate the paths **exactly as observed**, double slash included. |
| SPA origin | `https://subastas.secop.gov.co/eauctions.site/` |
| Content-Type (POST) | `application/json;charset=UTF-8` |
| Auth | `Authorization: Bearer <JWT>` on **every** call (see §3) |

**WAF:** requests with a library default User-Agent (`python-requests/…`, `Go-http-client/…`) get
**HTTP 403**. Any browser-like UA passes. Always send a realistic `User-Agent`. (Observed on the
static host; treat the API host as the same until proven otherwise.)

**CORS** headers in responses are browser-only and irrelevant to a server-side Python client —
ignore them.

**Exact request header set to replicate** (from the HAR bid calls):

```
Authorization: Bearer <JWT>
Accept: application/json, text/plain, */*
Content-Type: application/json;charset=UTF-8       (POST only)
Origin: https://subastas.secop.gov.co
Referer: https://subastas.secop.gov.co/eauctions.site/?auctionId=<id>&languageCode=es-CO&marketCode=GovCo
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/…
Accept-Language: es-US,es-419;q=0.9,es;q=0.8,en;q=0.7
```

> The HAR is a **sanitized** Chrome export: `Authorization`, `Cookie`, and `Set-Cookie` headers were
> stripped. So the Bearer scheme is confirmed from the app's interceptor **code**, not from the wire,
> and the sticky-session cookies (§3) are known from server behavior, not captured values.

---

## 2. Provenance / how to trust this doc

- **Observed** = seen on the wire in the HAR (429 entries, one real reverse auction,
  `auctionId d6a49a23-3556-4b3d-9bb4-3e209279b9f8`, a **test** auction: `testAuction: true`).
- **Client-code-only** = read from the SPA's JS; proves what the client *sends*, never what the
  server *enforces*.
- **Inferred** = reasoned, not directly evidenced.

The captured auction produced **no successful bid** — all 8 attempts were `ValidateBid` pre-checks
that failed (see §7). So the `CreateBid` **request** shape is confidently reconstructed (same builder
feeds both endpoints) but its **success response** is not directly observed. Build defensively around
that.

---

## 3. Auth & session module

**Bearer injection (client-code-only, `app_services_authInterceptorService.js`):**
```
config.headers.Authorization = 'Bearer ' + appSettings.token;   // added to every $http request
// on response 401 → window.top.location.href = appSettings.stsLoginPageUrl  (i.e. re-login)
```
So: attach the JWT to every request; treat **401 as "token dead"** → stop, do not retry, surface for
re-harvest.

**Where the token lives in the browser (client-code-only, `app_app.js`):** the SPA reads it from an
HTML attribute `data-item-token` on element `#div-item-data`, alongside these siblings the app also
consumes — useful if you ever scrape the launch page instead of pasting the token:

`data-item-token, data-item-servicebase, data-item-serviceversion, data-item-auctionid,
data-item-marketcode, data-item-languagecode, data-item-companycode, data-item-companyname,
data-item-hourdifference, data-item-synctime, data-item-sts-loginpage-url, data-item-isowner,
data-item-decimalcases, data-item-integercases`.

For this app the token is **pasted by the user**, so scraping is optional.

**JWT claims to decode for logging (observed, from the HAR token — do not log the raw token):**

| Claim | Meaning | Use |
|---|---|---|
| `exp`, `nbf` | expiry / not-before (unix) | 24h lifetime; preflight & countdown |
| `LoggedInCompanyCode` | e.g. `900000000` | this is the `userCode` in the server audit log |
| `LoggedInCompanyName` | e.g. `UT MIN SAMLUB 2026` | logging |
| `LoggedInUserId`, `LoggedInUserName` | operator identity | logging |
| `LoginGUID` | session guid | logging/correlation |
| `aud` | `https://subastas.secop.gov.co/eauctions.site/` | sanity check |

**Preflight (do this at startup):** decode `exp`; if `exp - now < safety_margin` (e.g. 5 min), abort
with `AUTH_EXPIRING`. Then make one cheap authenticated GET (`GetAuctionSimple/{id}`) to confirm the
token is live and the auction is reachable. A 401 here means re-harvest before doing anything else.

**Sticky session (inferred, Azure Application Gateway):** the gateway sets
`ApplicationGatewayAffinity` and `ApplicationGatewayAffinityCORS` cookies for backend affinity.
**All N threads must share one cookie jar** so the burst lands on the same backend and sees consistent
state. Capture cookies from the first (preflight) response into a shared jar and reuse it everywhere.

---

## 4. Inputs / configuration

The app takes a config (CLI flags, env, or a JSON/TOML file). Fields:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `jwt` | string | yes | Bearer token, pasted from a logged-in browser. |
| `auction_page_html_path` | path | yes | **File** holding a saved SECOP page (§5). The real artifacts are 300–500 KB — too large to paste into a config string. Accepts either supported page (§5.1). |
| `auction_id` | GUID | no | Explicit override, required when the page holds several auctions (§5.5). Must still be present on the page. |
| `bid_value` | string (decimal) | yes | The **per-unit** value applied to **every** item. Accept Colombian or wire format; normalize (§8.1). |
| `bid_is_total` | bool | no (default false) | If true, `bid_value` is a per-item **total**, so `bidValue_i = bid_value ÷ quantity_i` — a **division**, not a reciprocal (do not read §8.3's "inverse ratio" here; that is a different quantity). ⚠ **Unresolved:** this contradicts §7 ("never modifies the value"), makes `bidValue` differ per item, and inherits §8.4's undefined rounding for non-terminating quotients. Every captured item has `quantity == 1`, so it has never been exercised. Leave `false` until §15 Q12 is settled. |
| `n_direct` (D) | int | `single` | Direct-submit threads. Unused in `burst`/`custom`. |
| `n_validate_submit` (V) | int | no (default 0) | Validate-then-submit threads. |
| `n_validate_only` (P) | int | no (default 0) | Validate-only probe threads. |
| `v_gate_on_valid` | bool | no (default true) | V threads submit only if `ValidateBid.isValid`. |
| **`fire_time`** | **ISO-8601 with explicit offset or `Z`** | **yes** | **The instant `T` the app fires at, on the local clock (§9.2). Naive timestamps are a config error. Never derived from the server.** |
| `max_arm_ahead_s` | int | no (default 3600) | Abort if `fire_time` is further ahead than this — a long-armed run outlives the 24 h JWT (§9.2). |
| `fire_time_mismatch_warn_s` | int | no (default 2) | Warn (never override) if `fire_time` differs from `startDate` by more than this (§9.2). |
| `apply_clock_offset` | bool | no (**default false**) | Shift `T` by the measured server offset. Off by default — the offset is diagnostics (§9.3). |
| `clock_probe` | enum `date_header`\|`signalr`\|`both`\|`none` | no (default `date_header`) | Which server-time probes to **record**. Does not affect `T` unless `apply_clock_offset` (§9.3). |
| `lead_ms` | int | no (**default 0**) | Fire this early to offset transit. Set deliberately from a low quantile of `timings.wait`; not auto-derived (§9.5). |
| `validate_lead_ms` | int | no (default 0) | How early V/P threads start their `ValidateBid` (§9.2, §11.4). |
| `gate_mode` | enum `once_before`\|`off` | no (default `once_before`) | State gate strategy. Never per-wave — it costs a full RTT (§9.4). |
| `gate_lead_ms` | int | no (default 1500) | How far before `T` the single gate check is issued, so its *response* lands before release (§9.4). |
| `mode` | enum `single`\|`burst`\|`custom` | no (default `single`) | Firing schedule (§11.4). |
| `bid_count` | int | `burst` | Number of scheduled bid waves. |
| `pre_ms` | int | `burst` (default 0) | Start firing this many ms **before** T. |
| `interval_ms` | int | `burst` (required when `bid_count > 1`) | Spacing between consecutive waves. |
| `wave_direct` | int | `burst`/`custom` (default 1) | Direct submits fired per wave. |
| `wave_validate` | int | `burst`/`custom` (default 0) | Validate-then-submit fired per wave. |
| `wave_validate_only` | int | `burst`/`custom` (default 0) | Validate-only probes per wave. Completes the §11.4 wave schema. |
| `schedule_offsets_ms` | list[int] | `custom` | Explicit wave offsets vs T (negative = before T), e.g. `[-100,-50,-20,0,20,50]`. |
| `stop_on_success` | bool | no (default true) | Cancel not-yet-fired waves once a bid is accepted (cuts audit/WAF noise). |
| `do_start_audit` | bool | no (default true) | POST `SetStartBiddingAudit` before firing (mimics browser). |
| `do_submit_audit` | bool | no (default true) | POST `SetSubmitBiddingAudit` after a win. |
| `dry_run` | bool | no (default false) | Do everything **except** `CreateBid` (validate + log only). |
| `connect_timeout_ms` | int | no (default 3000) | TCP+TLS connect timeout. `requests` blocks forever without one. |
| `read_timeout_ms` | int | no (default 5000) | Response timeout. Max observed bid response 1633 ms. Network errors are **not** retried. |
| `auth_safety_margin_s` | int | no (default 300) | Abort `AUTH_EXPIRING` if `exp − now` is below this (§3). |
| `user_agent` | string | no | Defaults to the literal captured Chrome UA (§1). |

In `single` mode the `n_direct`/`n_validate_submit`/`n_validate_only` (D/V/P) counts define the one
wave's composition; in `burst`/`custom` modes each wave is composed from
`wave_direct`/`wave_validate`/`wave_validate_only` (see §11.4).

Total `CreateBid` submissions across a run = **Σ waves × (direct + validate)** — this is what the WAF and
the server audit log see (§11.3).

**Validation of config:**
- `fire_time` present, timezone-explicit, in the future, within `max_arm_ahead_s`.
- Submitters: error only when `dry_run == false` **and** submitters == 0 **and** `n_validate_only == 0`
  — i.e. a probe-only or dry run is legal and simply fires no `CreateBid`. Submitters are `D + V`
  (`single`) or `wave_direct + wave_validate` (`burst`/`custom`).
- `burst`: `bid_count >= 1`; if `bid_count > 1` then `interval_ms >= 1` — otherwise
  `offset_ms(i) = −0 + i·0 = 0` for every `i` and you silently get `bid_count` simultaneous waves at `T`
  with maximum audit footprint, which is `single` with a larger D wearing a disguise.
- `custom`: `schedule_offsets_ms` non-empty.
- Warn when the total submission count is large (WAF/rate-limit + audit-log noise; §1, §11.3).

---

## 5. Stage 1 — Extract auction identifiers (from a saved HTML page)

**Source:** a saved SECOP page **HTML**, given to the app as `auction_page_html_path` (a file — the real
artifacts are 300–500 KB, far past any sane "paste into a config string"). No URL fetching, no portal
cookies. You open the page in your already-logged-in browser, save it, and point the app at the file.

**Why HTML-only:** that page lives on `www.secop.gov.co` — the Vortal portal, a **separate app the
eauctions JWT does not authenticate.** An unauthenticated fetch `302`-redirects to the WS-Federation
login. Pasting the rendered HTML sidesteps that entirely, so the app never has to hold a second
(separately-expiring) portal session.

### 5.1 Which page — and how it must be saved

**Two pages work, by two different mechanisms. Detect which one you were given; do not assume.**

| Page | `<title>` | Fixture | Yields | Extractor |
|---|---|---|---|---|
| Opportunity list ("Oportunidades") | `Oportunidades` | `beforebid.html` | `auction_id`, `market`, `eauction_doc_id`, `mkey`, state text | **A — onclick** |
| Auction launch ("Integración") | `Integración` | `bid.html` | `auction_id`, `lang`, `market` | **B — iframe** |

`bid.html` contains **none** of the `RedirectToEAuction` / `externalId` / `docUniqueIdentifier` /
`CO1.EAREQ` tokens — extractor A finds nothing there, which is why B is not optional.

> **Save method matters.** Both fixtures are **DOM serializations** (`data-scribe-recorder-ready="true"`,
> pretty-printed one attribute per line), not raw view-source. Every pattern below therefore uses
> `\s*\+\s*`, **never** a literal `' + '`. Require a DOM save ("Copy outer HTML" / devtools save), and
> keep the fixtures byte-identical to what the parser is tested against.

### 5.2 Extractor A — the `onclick` is *not* a URL

This is the trap. The opportunity page carries a **JavaScript string-concatenation expression**, not a
query string ([`beforebid.html:3822`](beforebid.html)):

```
onclick="javascript:getAction('/CO1BusinessLine/Tendering/WidgetAuction/RedirectToEAuction' + '?'
  + 'docUniqueIdentifier=' + 'CO1.EAREQ.937510' + '&amp;' + 'market=' + 'GovCo'
  + '&amp;' + 'externalId=' + 'd6a49a23-3556-4b3d-9bb4-3e209279b9f8' + '&mkey=…',true);"
```

Feeding that to `urllib.parse` **does not work** — `parse_qs` applies `unquote_plus`, so every `+`
operator decodes to a space and the keys come back as `"'   'externalId"`, `"'   'market"`, … .
`params.get('externalId')` is `None`. Verified against the fixture.

**So: HTML-unescape, then de-concatenate, then parse.** In that order.

```python
DECONCAT = re.compile(r"'\s*\+\s*'")
ANCHOR   = re.compile(r"RedirectToEAuction[A-Za-z]*'.*?,\s*true\s*\)\s*;", re.S)

txt = html.unescape(raw_html)                 # &amp; -> &   (must precede the split)
for a in ANCHOR.findall(txt):
    joined = DECONCAT.sub('', a)              # collapse the JS concatenation
    qs     = joined.split('?', 1)[1].split("',true)")[0].rstrip("'")
    params = urllib.parse.parse_qs(qs)        # now a real query string
```

yields `{'docUniqueIdentifier': ['CO1.EAREQ.937510'], 'market': ['GovCo'],
'externalId': ['d6a49a23-…'], 'mkey': ['5f1742a6_…']}`.

`ANCHOR` matches **two** anchors (`RedirectToEAuctionIFrame` also contains `RedirectToEAuction`). Both
carry the same `externalId`; only the plain one carries `docUniqueIdentifier`. Collect all candidates,
then prefer the one that has a `docUniqueIdentifier`.

### 5.3 Extractor B — the launch page's iframe *is* a real URL

[`bid.html:3315`](bid.html) has an ordinary query string — plain `urlparse` is correct here:

```
<iframe id="iframeEauction"
  src="https://subastas.secop.gov.co/eauctions.site/?auctionId=d6a49a23-…&amp;languageCode=es-CO&amp;marketCode=GovCo#/home/priceList">
```

Match `src` on `iframe#iframeEauction` (or any `src` containing `eauctions.site/?`), unescape, `urlparse`,
`parse_qs` → `auctionId` / `languageCode` / `marketCode`. This is also the **only** place `lang` and
`market` are carried in their API-side spelling, so it doubles as the cross-check on §1's Referer.

### 5.4 Fallbacks — concatenation-aware, and a hard prohibition

```python
FB_EXT = re.compile(r"externalId=(?:'\s*\+\s*')?([0-9a-fA-F-]{36})")
FB_DOC = re.compile(r"docUniqueIdentifier=(?:'\s*\+\s*')?(CO1\.EAREQ\.\d+)")
```

The optional `(?:'\s*\+\s*')?` group is what makes these work on *both* the concatenated and the plain
form. Without it — i.e. the naive `externalId=([0-9a-f-]{36})` — they match **zero bytes** on the only
pages we have.

> **Never fall back to "find a GUID."** `beforebid.html` holds **75 GUID occurrences across 25 distinct
> GUIDs**; the target appears **twice — tied for least frequent** — and is 72nd in document order. Both
> "first GUID" and "most frequent GUID" select a context-menu id. A bare-GUID fallback does not degrade
> gracefully, it silently bids on the wrong thing.

Likewise **do not relax the `CO1\.EAREQ\.` anchor**: `docUniqueIdentifier` occurs **21×** in
`beforebid.html`, and 20 of those are the *opportunity* dossier (`CO1.OPDOS.31397606`). The `EAREQ`
constraint is the only thing discriminating the auction from its parent dossier.

### 5.5 Ambiguity, state, and checks

- **Multi-row pages are the normal case.** The grid ids are indexed
  (`incWidgetAuctiongrdEAuctionGrid_tr0`, `…_0`) — it is a "my auctions" widget, multi-row by
  construction. If extractor A yields **more than one distinct `externalId`**, do **not** guess: abort
  with `PARSE_AMBIGUOUS`, list every candidate (GUID + doc id + row index + state text), and require the
  operator to disambiguate via an explicit `auction_id` config override. An override, when supplied, must
  still be *found* on the page — otherwise abort.
- **Validate** `auction_id` is a GUID. Default `lang` to `es-CO` and `market` to `GovCo` only if neither
  extractor yielded them, and log that the default was used.
- `lang` from the opportunity page: `<select name="languageSelector"><option value="es-CO" …>`
  ([`beforebid.html:4456`](beforebid.html)) — this is the one original §5 instruction that worked
  verbatim. Fallback `…/es-CO.json` translation paths (68 hits, no competing locale).
- `mkey` is **session-scoped** (bound to the rendered page + Vortal session) and is **not** used to call
  the eauctions API — across all 429 HAR entries `mkey`, `externalId`, `CO1.EAREQ` and
  `docUniqueIdentifier` appear **0 times**. Capture for logs; never depend on it. Same for
  `eauction_doc_id`: log-only, never fatal. What the parser produces is a *relative path*, so call it
  `redirect_path`, not `redirect_url`.
- **State text** is at `td[id$='thEAuctionStateCol']` (`beforebid.html:3874`) — the value there is
  `Terminado`. Absent entirely from `bid.html`, so on that page the check must yield `STATE_UNKNOWN`,
  **not** "not finished". Warn loudly on a finished auction; this is a *convenience* check only — §9.3's
  server-side state read is the real guard. Note the shipped fixture is itself a finished auction.
- Log the resolved `AuctionRef`, including which extractor (`onclick` | `iframe` | `fallback`) produced it.

**Output:** `AuctionRef { auction_id, lang, market, eauction_doc_id?, mkey?, redirect_path?, source_extractor, state_text? }`.

> **Acceptance test** (`test_page_parse.py`, both fixtures — this is the milestone-5 gate):
> `beforebid.html` → `auction_id=d6a49a23-3556-4b3d-9bb4-3e209279b9f8`, `market=GovCo`,
> `eauction_doc_id=CO1.EAREQ.937510`, `lang=es-CO`, `state_text=Terminado`, `source_extractor=onclick`;
> `bid.html` → same `auction_id`, `lang=es-CO`, `market=GovCo`, `source_extractor=iframe`,
> `eauction_doc_id=None`, `state_text=None`. Plus negative tests: the naive
> `externalId=([0-9a-f-]{36})` regex asserted to return `[]`, and a synthetic two-auction page asserted
> to raise `PARSE_AMBIGUOUS`.

---

## 6. Stage 2 — Retrieve auction detail & build the item model

### 6.1 Calls (all GET, Bearer)

| Endpoint | Purpose | Notes |
|---|---|---|
| `/eauctions.api/v1//auction/GetAuctionSimple/{id}` | config + state + timing | **Observed** full field list below. |
| `/eauctions.api/v1//auction/GetAuction/{id}/{lang}` | the item list (`biddingList`) | **Not captured** — shape is client-code-only; reconstruct from `auctionService.js`. |
| `/eauctions.api/v1//Bid/GetBidsTotalInfo/{id}` | `{auctionId, myLastBid, lastBid, score}` | Tells you if a baseline bid exists (the 0/1/2 trap, §7). Note capital `B` in `Bid`. |

**`GetAuctionSimple` fields (observed):** `id, customerId, auctionVersionCode, auctionEvaluationTypeCode,
auctionTypeCode, minimumBidType, auctionStateCode, auctionExecutionStateCode, currencyCode,
roundTypeCode, title, requestInformation, comments, originalStartDate, originalEndDate, startDate,
endDate, startPrice, procedureType, showBidderNameForOwner, showRanking, showBidderNameForOtherBidders,
showBestBidsForAllBids, showBestBidsForSelfBids, canSupplierSeeTotalParticipants, allowBidsBellowBestBid,
showFinalRank, initialValueDifferentPerBidder, extraTimeAutomaticAllow, activateExtraTimeWinner,
extraTimePeriod, extraTimeMaximumBids, startingExtraTimeMaximumBids, bidsInExtraTime, extraTimeManualAllow,
extraTimeLastMinutesStartPeriod, pauseAllow, testAuction, testAuctionRealValues, totalNum, decimalNum,
bidderInitialBidShowBeforeStart, hideBidderCurrentBid, noValueRank, scoreCalcFormula, pauseOptions,
timeZoneCode, createdOnUtc, createdBy, updatedOnUtc, updatedBy`, plus arrays
`auctionFinisheds, bidders, biddingsList, chatHistories, evaluationRules, eventHistories, users,
auctionBidderLots` (empty in the simple view).

Capture at minimum: `auctionVersionCode, auctionTypeCode, currencyCode, roundTypeCode, decimalNum,
totalNum, minimumBidType, startPrice, startDate, endDate, auctionStateCode, auctionExecutionStateCode,
allowBidsBellowBestBid, testAuction`.

### 6.2 The item model (client-code-only)

`GetAuction` returns `biddingList[]`; each entry with `containtItems` carries
`biddingPriceListItems[]` and/or `biddingQuantitativeItems[]`. In `auctionService.js` these are
flattened into `self.Items`, sorted by `displayItemOrder`, tagged `isPriceListItem`/`isChapter`, with
`parent`/`lotId` set and `lotName ← lotName || groupReference`.

**To build one bid item, pull these fields from each price-list item** (mapping verified in
`createBidObjectItem` ~L329 and the `itemBid` builder ~L3775–3790 of `priceListController.js`):

| Bid-item field (wire) | Source field in GetAuction | Note |
|---|---|---|
| `itemId` | **`itemName`** | Holds a GUID despite the name. |
| `quantity` | `itemQuantityStr` | Keep the string (carries the 6-dp form, e.g. `"1.000000"`). |
| `itemUnit` | `itemUnit` | e.g. `UN`. |
| `itemInternalReference` | `itemInternalReference` | (For non-price-list items the code uses `itemDescription`.) |
| `lotInternalId` | `lotInternalId` ‖ `biddingListId` | Fallback if empty. |
| `lotName` | `lotName` ‖ `groupReference` (bidList level) | Fallback if empty. |
| `initialValue` (for the formula, not sent as-is) | `itemInitialUnitPriceStr` | Per-unit initial price. |
| `minBidDifference` | `minBidDifference` | Per-item minimum decrement (for §7 reasoning/logging). |
| `isPriceListItem` | `true` for these | — |

**`auctionVersionCode` decides payload shape** (`auctionVersions = ["AuctionbyProposal","AuctionbyLot",
"AuctionbyItem"]`):
- **AuctionbyProposal** (the captured case): **one** `CreateBid` whose `items[]` holds **all**
  price-list items. This is the primary target and matches "apply the value to all items."
- **AuctionbyItem** / **AuctionbyLot**: the SPA bids per item / per lot. For v1, support Proposal fully;
  for byItem/byLot, either (a) still send all items in one `items[]` if the server accepts it, or
  (b) loop one `CreateBid` per item/lot. **Flag this as an explicit branch and log the version.**

**Output:** `AuctionDetail { config fields…, items: [ItemMeta…] }` where
`ItemMeta { item_id, quantity: Decimal, item_unit, item_internal_reference, lot_internal_id, lot_name,
initial_value: Decimal, min_bid_difference: Decimal, is_price_list_item }`.

---

## 7. The value is sent as-is (no floor, no retry, no adjustment)

**The app sends your value exactly as you gave it, once per configured attempt.** It never modifies it,
never computes a "lowest legal" number, never enforces a floor, and never retries with a different
value. Value in → payload → sent. That is the whole behavior; there is no decrement logic in the app.

The only thing to be aware of is that the **server** can reject your value on its own. In the capture,
bids of 1/10/200 came back with `errorType: "BidErrMin"` because `GetBidsTotalInfo` showed
`myLastBid: 0` — the company had no standing bid for the server to decrement from. That is a server-side
rule, not something the app can or should work around.

So the app's only job around this is **logging** (not retrying): capture `myLastBid`/`lastBid` at
startup, and if a send is rejected, record `errorType` / `errorMessage` / `minBid` verbatim so you can
see *why* it was rejected. Nothing is retried or altered — the rejection is simply reported.

---

## 8. Stage 3 — Number handling & payload construction (highest-stakes)

### 8.1 Two number formats — never mix them

| Context | Thousands | Decimal | Example (7,813,974.32) |
|---|---|---|---|
| **Wire / JSON body** (what you send) | none | dot `.` | `7813974.32` |
| **UI & server *messages*** (Colombian es-CO) | dot `.` | comma `,` | `7.813.974,32 COP` |

- **Sending:** wire format — JSON **strings**, dot decimal, no thousands separator. (Observed:
  `"10.000000"`, `"7813974.32000000"`.)
- **Reading a server message** (e.g. `minBid`): normalize before math — strip trailing ` COP`, remove
  every `.`, replace `,`→`.`. So `"7.813.974,32 COP"` → `Decimal("7813974.32")`.
- **Reading user input:** if it contains a comma or a dot-as-thousands, normalize the same way; if it's
  already plain (`1`, `2`, `1234567.89`), pass through.

**Round-trip self-test (ship it as a unit test):** take the server `minBid` string → normalize →
re-format to Colombian → assert equal to original. If it fails, the parser is wrong; refuse to bid.

### 8.2 Use `decimal.Decimal`, never float

All arithmetic in `decimal.Decimal` with a wide context (the app's own JS uses BigDecimal with
`setDecimalSettings Int:18/Dec:6`; auction `decimalNum: 2`, `totalNum: 20`, `roundTypeCode: "Up"`).
Set e.g. `getcontext().prec = 50`. Float will introduce `…0001` drift that can trip validation.

### 8.3 The bid payload (`getBitModel`, `priceListController.js` ~L1847; item builder ~L329)

**Top-level object:**

| Field | Value | Kind |
|---|---|---|
| `auctionId` | the GUID | from ref |
| `currency` | `currencyCode` (e.g. `"COP"`) | from metadata |
| `Date` | current time ISO-8601 UTC (`new Date()`) | constant-ish; **server ignores it** (echoes `date: "0001-01-01T00:00:00"`). Send a plausible value. |
| `ranking` | `-1` | literal |
| `items` | array, one per price-list item, **all set to the input value** | computed |
| `totalValue` | sum of item `totalValue`s | computed |
| `languageCode` | `lang` (e.g. `"es-CO"`) | from ref |

**Per item (`createBidObjectItem`):**

| Field | Value | Kind |
|---|---|---|
| `itemId` | `ItemMeta.item_id` | metadata |
| `quantity` | `ItemMeta.quantity` (string, e.g. `"1.000000"`) | metadata |
| `isPriceListItem` | `true` | literal |
| `isChanged` | `true` | literal (we're changing every item) |
| `itemInternalReference` | `ItemMeta.item_internal_reference` | metadata |
| `lotInternalId` | `ItemMeta.lot_internal_id` | metadata |
| `lotName` | `ItemMeta.lot_name` (may be `""`) | metadata |
| `itemUnit` | `ItemMeta.item_unit` | metadata |
| `bidValue` | the input value (same for all items) | **user input** |
| `totalValue` | `quantity × bidValue` | computed |
| `itemQuantityUnitPrice` | `initialValue × quantity ÷ bidValue` | computed (**inverse ratio**, see below) |
| `bidded` | `true` | literal |

### 8.4 The two formulas and their exact string form

- `totalValue = quantity × bidValue`.
  BigDecimal/`Decimal` **scale adds** on multiply, and you must **not** strip trailing zeros — that
  reproduces the observed strings. `Decimal("1.000000")*Decimal("10")` → `"10.000000"`;
  `Decimal("1.000000")*Decimal("7813974.32")` → `"7813974.32000000"`. So `str(Decimal)` *is* the wire
  string. (Do the same for top-level `totalValue` = sum of item totals.)
- `itemQuantityUnitPrice = initialValue × quantity ÷ bidValue`, **quantized to 12 decimal places**
  (observed: `"39069871.600000000000"`, `"390698716.000000000000"`, `"1953493.580000000000"`,
  `"50.000000000000"` — all 12 dp). Zero special-case: if `initialValue == 0` or `bidValue == 0`, set it
  to `0`. Rounding mode for non-terminating divisions is **uncertain** (`roundTypeCode: "Up"` may apply)
  — for whole-number bid values like 1 or 2 the division is exact, so prefer those; if a division is
  inexact, log a warning and quantize with a chosen mode (document it).

  **Invariant to sanity-check** (proven across all 8 HAR bids): `itemQuantityUnitPrice × bidValue =
  initialValue × quantity` (a constant = the item's initial total). Assert this after building each
  item; a mismatch means a formatting/precision bug.

### 8.5 Worked example — captured auction, `bid_value = 1`

```
item[0] = {
  "itemId": "8552d67b-29ce-4b30-877b-e361fd876072",
  "quantity": "1.000000",
  "isPriceListItem": true,
  "isChanged": true,
  "itemInternalReference": "1",
  "lotInternalId": "CO1.OTLN.74514427",
  "lotName": "",
  "itemUnit": "UN",
  "bidValue": "1",
  "totalValue": "1.000000",
  "itemQuantityUnitPrice": "390698716.000000000000",
  "bidded": true
}
body = {
  "auctionId": "d6a49a23-3556-4b3d-9bb4-3e209279b9f8",
  "currency": "COP",
  "Date": "<now ISO8601 UTC>",
  "ranking": -1,
  "items": [ item[0] ],
  "totalValue": "1.000000",
  "languageCode": "es-CO"
}
```
Serialize once, as bytes, and reuse the **identical** payload across all threads.

---

## 9. Stage 4 — Fire time, clock, state gate, scheduling

**The operator owns the fire instant; the server owns the window.** The app fires at an instant **you
specify**, measured on the **local** clock. It does *not* derive the fire instant from a server clock.
Server-time probes still run, but they are **diagnostics only** — measured, logged, compared, and never
used to shift `T` unless you explicitly opt in.

### 9.1 Why the fire time is manual

Deriving `T` from the server was the original design and it does not survive the evidence.

- **The HTTP `Date` header is whole-second.** 429/429 captured responses carry `Date`; **none** has a
  fractional part. So `Date − local_now` is a *biased* estimator (expected error ≈ −0.5 s), not a noisy
  centred one — **a median converges to the bias instead of removing it**, and `RTT/2` corrects ~68 ms of
  a ~500 ms error. Measured over the 128 captured `GetAuctionSimple`: prescribed estimator median
  **−0.458 s**, while a rigorous interval intersection of the *same* data gives offset ∈
  **[−0.016, +0.114] s**. Applying the naive value would fire ~0.45 s late — every wave of a
  `T−60 … T+30 ms` schedule would land at ≈ `T+390 … T+480 ms`, and *no* wave would land before `T`.
- **The two server clocks disagree.** The `Date` clock's feasible interval and the SignalR hub clock
  (128 `ReceiveClock` samples, median **−0.547 s**, 100-ns resolution) **do not intersect**. Neither is
  tied by any evidence to the clock the auction state machine actually uses.
- **We have never observed an auction open.** The capture starts 7 m 40 s *after* `startDate`, and all
  128 state polls run *after* `endDate`. `ReadyToStart` was never returned. There is no evidence about
  what happens at `T` at all.

A manually specified `T` doesn't magically create accuracy — **it relocates the error to your host clock,
where you can actually control and measure it.** Sync the host with NTP (`chronyc tracking` /
`sntp -sS`); a normally-synced machine is within single-digit ms of UTC, which is an order of magnitude
better than anything derivable from this API. That is the whole argument.

### 9.2 Specifying the fire time

`fire_time` is **required**. It is an absolute instant with an **explicit** UTC offset or `Z` — a naive
local timestamp is a config error, not something to guess a zone for. (`GetAuctionSimple.timeZoneCode`
is `null` in the capture, so there is nothing to infer from.)

```
fire_time = "2026-07-28T21:00:00Z"            # or "2026-07-28T16:00:00-05:00"
T_wall    = parse(fire_time)                   # the target instant, host clock
T_mono    = time.monotonic_ns() + (T_wall − now_wall)   # convert ONCE, at arm time
```

- Convert wall → monotonic **once**, at arm time, then schedule exclusively on
  `time.monotonic_ns()`. This makes the run immune to NTP steps and DST during the countdown.
- `wave_fire_mono = T_mono + wave.offset_ms − lead_ms` (§11.4). Sleep on the monotonic clock to ~2 ms
  out, then busy-wait the remainder.
- **Sanity gate at arm time:** if `T_wall` is in the past, abort (`FIRE_TIME_PAST`). If it is more than
  `max_arm_ahead_s` away (default 3600), abort — you almost certainly typed the wrong day, and a run
  armed for hours will outlive the 24 h JWT.
- **Cross-check, never override:** compare `T_wall` against `GetAuctionSimple.startDate`. If they differ
  by more than `fire_time_mismatch_warn_s` (default 2), **warn loudly and log both** — but honour
  `fire_time`. The operator is the authority; the point of this design is that a wrong `startDate` or a
  wrong clock offset cannot silently move your bid.
- **V/P threads** start their `ValidateBid` at `wave_fire_mono − validate_lead_ms` so their subsequent
  `CreateBid` still lands near their wave's instant (they carry an extra round trip). Whether
  `ValidateBid` is accepted *before* `auctionStateCode == "Started"` is still an **open question**
  (§15 Q5) — if it is, set `validate_lead_ms ≈ RTT`; if not, leave 0 and accept that V lands ~1 RTT
  behind D.

### 9.3 Clock probes — diagnostics, not control

Run them anyway; they are how you find out afterwards *why* a bid landed where it did.

- **`date_header`:** on every response, record `Date`, `t_send_mono`, `t_recv_mono`. Compute the honest
  interval per sample — `offset ∈ [Date − t_recv, Date + 1 − t_send]` — and **intersect** across samples
  (Marzullo). Log the midpoint **and the residual interval width**. Never take a bare median: with
  second-granular timestamps that number is a bias, not an estimate.
- **`signalr` (optional, unbuilt):** the hub clock has 100-ns resolution and is the only sub-second
  server time available — but **this path is research, not a spec.** The handshake was **not** captured
  (`negotiate`/`start`/`abort` have zero hits across all 429 URLs) and `connectionToken` cannot be
  derived from anything in these files; it must be reversed out of `jquery.signalR` 2.x first. Also, if
  you build it: the paths' slash counts differ (`/eauctions.api/signalr/signalr/poll` **single**,
  `//signalr/signalr/send` and `//signalr/signalr/ping` **double** — do not normalize), the POST bodies
  are `application/x-www-form-urlencoded` with the JSON under `data=` (**not** §1's JSON content type),
  auth is the `token=` query param and **not** Bearer, each poll carries a `messageId` cursor echoed back
  in the response's top-level `C` field, 15 of 142 polls return an empty `M` keepalive (not a failure),
  and one response delivered the same `ReceiveClock` twice (dedupe). See §15 Q11.
- **Report, don't act.** Log `clock_offset_date_header`, `clock_offset_signalr`, their residual widths,
  and their disagreement. Emit `WARN` when the disagreement exceeds the schedule span (it did in the
  capture: ~0.5 s against a 90 ms span). Apply an offset to `T` **only** if `apply_clock_offset=true`,
  which is **off by default** and exists solely so you can experiment once you have ground truth from a
  real open.

### 9.4 State gate — once, ahead of `T`, never inline

`auctionStateCode ∈ ["ReadyToStart","Started","Finished","Canceled","Paused","Deprecated"]`;
`auctionExecutionStateCode` was `Running` on **128/128** captured responses — including all 125 *after*
the auction flipped to `Finished`, so `!= "Paused"` is permissive past the end of the auction and
contributes almost nothing.

**The gate cannot be inline.** It *is* a `GetAuctionSimple`: observed latency min **90.3** / median
**135.3** / max **1138.3** ms (n=128) — 1.5×–12× the entire span of a recommended schedule (§11.4's
`pre_ms=60, interval_ms=30` spans 90 ms). Gating immediately before each `CreateBid` would give D threads
**two** round trips, which is exactly what §11.1 says distinguishes V from D — the role model collapses.
Worse, in `single` mode a gate can only read `Started` if issued *after* `T`, so the bid necessarily
lands at `T +` one gate RTT (≥ 90 ms, the fastest response of any kind in the capture).

So, by `gate_mode`:

| `gate_mode` | Behaviour |
|---|---|
| **`once_before`** (default) | Issue **one** `GetAuctionSimple` at `T − gate_lead_ms` (default 1500, ≳ p95 RTT) so its *response* lands before release. Cache the verdict for the whole schedule. If it reads `Finished`/`Canceled`/`Deprecated`, **abort the run**. If `ReadyToStart`, proceed — that is the expected pre-open reading, not a failure. |
| **`off`** | Fire the schedule and let the server reject (`STATE_NOT_STARTED`). Legitimate: the server is the authority anyway, and §11.4 already relies on exactly this for pre-`T` waves. |

Never gate per wave. A post-run `GetAuctionSimple` is recorded for the log, not for control.

**Known state lag (observed, precision-bounded):** the server was still `Started` at 21:30:02.506Z and
`Finished` at 21:30:11.502Z against a nominal `endDate` of 21:30:00Z. The 3.000 s poll cadence brackets
the flip to **(+2.5 s, +11.5 s]** — the plan previously said "~9–11 s", which read the poll grid as the
event. Do **not** treat this lag as usable slack; and note it describes the *close*, about which we have
data, not the *open*, about which we have none.

### 9.5 RTT — measured for budgeting, not for `T`

Take **N ≥ 10** preflight `GetAuctionSimple` samples and use a **low quantile of `timings.wait`** (not
total elapsed, and never a single sample). Observed distribution: min 90.3 / p25 104.5 / median 135.3 /
p75 281.4 / p95 ≈ 665 / max 1138.3 ms — one sample is a random draw across a 12× range, so a
single-sample `lead_ms` varies by ~half a second between runs minutes apart.

- `lead_ms` defaults to **0**. With a manual `T` and an NTP-synced host there is nothing to lead against
  except transit; set it deliberately (a low quantile of `wait` is the defensible choice) and log it.
- Do **not** derive `lead_ms` from `ValidateBid` RTT. Its time is almost entirely server-side work
  (1633.1 ms total / 1627.1 ms `wait` on the slowest sample), not transit — leading by it would make the
  request arrive *before* the open and be rejected.
- Total elapsed also includes client-side `blocked` queueing (max 235.6 ms observed), which is not
  propagation either.
- Log the full distribution. Refuse to run (`CLOCK_SYNC_FAILED`) if median RTT exceeds the schedule span
  — the schedule is meaningless at that point.

---

## 10. Stage 5 — API surface (complete)

All under `…/eauctions.api/v1//`. From `apiService.js` cross-checked with the HAR.

| Function (`apiService.js`) | Method | Path | Body / params | Notes |
|---|---|---|---|---|
| `submitBid` (~L586) | POST | `bid/CreateBid` | full bid object | **the real submission** |
| `validateBid` (~L602) | POST | `bid/ValidateBid` | full bid object | pre-check; same object |
| `getMinBid` (~L762) | POST | `bid/GetMinBid` | bid object | server's min bid |
| `GetAuctionSimple` (~L52) | GET | `auction/GetAuctionSimple/{id}` | — | state/config |
| `getAuctionInfo` (~L11) | GET | `auction/GetAuction/{id}/{lang}` | — | item list |
| `GetBidsTotalInfo` (~L68) | GET | `Bid/GetBidsTotalInfo/{id}` | — | `myLastBid/lastBid/score` |
| `getBids` | GET | `bid/GetBids/{id}/{lastBidId}` | — | history |
| `getSupplierBids` | GET | `bid/GetSupplierBids/{id}/{companyCode}/{lastBidId}` | — | own history |
| `getGraphBids` | GET | `bid/GetGraphBids/{id}` | — | chart data |
| `auditStartBidding` (~L650) | POST | `audit/SetSubmitBiddingAudit?auctionId={id}` | null | **name/endpoint inverted** |
| `auditSubmitBidding` (~L661) | POST | `audit/SetStartBiddingAudit?auctionId={id}` | null | **name/endpoint inverted** |
| `AcknowledgeMessage` | POST | `audit/SetAcknowledgeMessage?auctionId={id}&objectId={oid}` | null | — |
| `GetAcknowledgeMessageWarningBids` | GET | `audit/GetAcknowledgeMessageWarningBids?auctionId={id}` | — | — |
| `GetEventLogs` | GET | `audit/GetEventLogs?…datatables…` | — | server audit log (has your IP) |
| `reportAuctionFinished` | POST | `Output/ReportAuctionFinished/{id}` | — | — |
| `getSystemHealth` | GET | `common/GetSystemHealth/` | — | liveness |

> **Naming inversion (confirm in code):** the JS function `auditStartBidding()` actually POSTs
> `SetSubmitBiddingAudit`, and `auditSubmitBidding()` actually POSTs `SetStartBiddingAudit`. Wire up by
> **endpoint**, not by JS function name. The browser fires **`SetStartBiddingAudit`** when the bidder
> opens/starts bidding (seen in HAR paired with `ValidateBid`) and **`SetSubmitBiddingAudit`** on submit.

**`ValidateBid` / `CreateBid` response fields (observed on ValidateBid; CreateBid assumed same shape,
client-code-only):**
`internalId, auctionId, company, date, totalScore, priceListScore, totalValue, totalSumUnitPrice,
auctionTotalBids, supplierTotalBids, bidId, totalValueStr, totalSumUnitPriceStr, currentRankingPosition,
isValid, errorMessage, errorMessageLocale, minBid, errorType, lots, items, globalRanking`.

Success signal used by the SPA (`priceListController.js` ~L2633): treat **`isValid == true` and empty
`errorMessage`** as success; capture `bidId`. Known `errorType`: `"BidErrMin"`; known
`errorMessageLocale`: `"BidWarn_Total_MinBid_Diff_Desc"`. Log any new codes verbatim.

---

## 11. Stage 6 — Concurrency & thread roles

### 11.1 Model
Use **`threading`** (not asyncio): the workload is a tiny synchronized burst of blocking HTTP calls
where wall-clock release precision matters more than throughput, and `requests` is battle-tested. (An
`asyncio`+`httpx` variant is acceptable if the implementer prefers; the design is identical.)

**Roles** (from the agreed design):
- **D — direct-submit:** at `T`, POST `CreateBid` immediately. **One RTT — and this is only true because
  the state gate runs once, ahead of `T`, rather than inline (§9.4).** An inline gate would make D two
  RTTs and erase the distinction from V. **These win any timestamp tiebreak** (⚠ assumes the server
  tiebreaks by arrival time — §11.3 Q1, unverified).
- **V — validate-then-submit:** at `T − validate_lead`, POST `ValidateBid`; if `isValid` (and
  `v_gate_on_valid`), immediately POST `CreateBid`. Two RTTs; safety net; self-aborts on invalid.
- **P — validate-only:** POST `ValidateBid`, record, never submit. Live probe (harvests `minBid`,
  `currentRankingPosition`).

### 11.2 Mechanics
- **Pre-build** the exact payload bytes once; every thread sends identical bytes.
- **Release barrier:** create all threads, each blocks on a `threading.Barrier` (or an `Event` +
  monotonic spin) so D threads unblock together at `T`.
- **Pre-warm connections:** before `T`, open and keep-alive the TCP+TLS connection each thread will use
  (e.g. a warm-up GET) so the first bid isn't paying handshake latency.
- **Shared sticky cookie:** all threads share the cookie jar from preflight (§3). If using per-thread
  `Session` objects for isolation, still inject the same `ApplicationGatewayAffinity` cookies.
- **First-success detection:** a thread-safe flag; the first `CreateBid` returning `isValid==true` sets
  it and records `bidId` + winning role + monotonic timestamp. Other in-flight requests are allowed to
  complete and are logged, but their outcome doesn't change the result.
- **Duplicate handling:** once one bid lands, later identical bids bounce (equal/not-lower). Treat those
  as **expected**, tag `DUPLICATE`, not failure.
- **Post-win:** optionally POST `SetSubmitBiddingAudit` (once).

### 11.3 Sizing & safety
Keep `D+V+P` **modest** (start 2–5). A concurrent burst is outside anything observed (the capture only
ever showed ~2.5 s polling); large N risks WAF 403 / rate-limit and writes **N rows** to the auction's
server-side audit log under your IP and company code. Log the chosen N and the rationale.

**Open questions that decide whether threading pays off** (log them, resolve on first live run):
1. Does SECOP break ties by submission time? If not, D-thread racing buys little beyond resilience.
2. Does the server tolerate concurrent identical bids, or hard-reject the burst?
3. Is `ValidateBid` accepted before `Started` (governs `validate_lead`)?

### 11.4 Firing modes & schedule

The **thread roles above are the *what*; the firing schedule is the *when*.** A run's schedule is an
ordered list of **waves**, each wave = `{ offset_ms (relative to T; negative = before T), direct,
validate, validate_only }`. The app derives the schedule from `mode` (§4):

| Mode | Schedule built | Use it for |
|---|---|---|
| **`single`** (default) | one wave at `offset_ms=0`: `{direct:D, validate:V, validate_only:P}` | Precise open snipe. Original behavior. |
| **`burst`** | `bid_count` waves, first at `T − pre_ms`, spaced `interval_ms`: `offset_ms(i) = −pre_ms + i·interval_ms` for `i∈0..bid_count−1`; each wave fires `wave_direct` + `wave_validate` | "Fire a specific number of bids starting X ms before the open." |
| **`custom`** | `schedule_offsets_ms` verbatim; each offset fires `wave_direct` + `wave_validate` | Arbitrary spread across the boundary. |

**Timing a wave:** `T_mono` is the monotonic conversion of the operator-supplied `fire_time` (§9.2).
Each wave releases at `wave_fire_mono = T_mono + wave.offset_ms − lead_ms`. Each wave has its own release
barrier; monotonic sleep to ~2 ms out, then a sub-ms busy-wait for the final approach. Pre-build the
payload bytes once and reuse across every wave and thread.

> **Naming:** `wave.offset_ms` (schedule position relative to `T`) and `clock_offset` (§9.3, the measured
> server-vs-local difference) are unrelated quantities. The original draft called both "offset" one
> clause apart in this formula. Use `clock_offset` for the latter, everywhere.

**Why fire *before* T (the point of `pre_ms` / negative offsets):** your host clock and the server's may
differ, and the server may open a hair early. A pre-T wave is **insurance**:
- If the auction is not yet `Started`, the server is expected to reject it → tag `STATE_NOT_STARTED`,
  **expected, not a failure**. (⚠ **Never observed** — the capture contains no pre-open request at all.
  §15 Q9. Treat the clean-rejection assumption as unproven.)
- If the server opened earlier than your estimate, that early wave is the one that lands first.

**The state gate is not applied per wave** — it costs a full `GetAuctionSimple` round trip (median 135 ms
against a 90 ms schedule span) and would give D threads two RTTs, collapsing the D/V distinction. It runs
**once**, at `T − gate_lead_ms`, and its verdict is cached for the entire schedule (§9.4). `gate_mode=off`
skips it and relies on server-side rejection, which is what pre-T waves already do.

**First-success spans the whole schedule.** The instant any `CreateBid` returns `isValid == true`,
record the winner: **wave index, its scheduled `offset_ms`, actual send offset from T, role, `bidId`**.
If `stop_on_success` (default), cancel every not-yet-released wave — this directly cuts the audit-log
and WAF footprint. Bids that land after the winner bounce as `DUPLICATE` (expected). Pre-T bids that were
too early are `STATE_NOT_STARTED` (expected).

**Safety scales with total submissions** = `Σ waves × (direct + validate)`. Each is a row in the
auction's server-side audit log under your public IP and company code, and a WAF/rate-limit data point.
Keep schedules tight and small; log the **full planned schedule** and the total up front, and log every
wave dropped by `stop_on_success`.

**Recommended shapes:**
- *Precise open snipe:* `single`, `D=3, V=1`.
- *Clock-uncertain open:* `burst`, `bid_count=4, pre_ms=60, interval_ms=30, wave_direct=1` → bids at
  `T−60, T−30, T, T+30` ms.
- *Wide net across the boundary:* `custom`, `schedule_offsets_ms=[-100,-50,-20,0,20,50,100]`,
  `wave_direct=1`.

**Interaction with `validate_lead_ms`:** within any wave, `validate` threads still start their
`ValidateBid` at `wave_fire_local − validate_lead_ms` so their `CreateBid` lands near that wave's time.

---

## 12. Logging & observability (build this first-class)

Goal: after any run — success or failure — you can reconstruct exactly what happened, per thread, to the
millisecond, without ever leaking the JWT.

### 12.1 Run directory
Create `runs/<UTC-timestamp>_<run_id>/` per invocation. `run_id` = short random hex (derive from
os.urandom; do not use it for security). Contents:

| File | Content |
|---|---|
| `config.json` | full resolved config, **JWT redacted** to `first6…last6`, plus decoded non-secret claims (company code/name, user id, exp, LoginGUID). |
| `events.jsonl` | one JSON object per event (see schema) — the primary log. |
| `http/` | one file per HTTP exchange: `NNN_<role>_<thread>_<endpoint>.json` with full request/response (redacted). |
| `capture.har` | **required** — a HAR-style capture of **every** request/response in the run, so the whole session can be replayed/inspected in the same tools this project started from. |
| `timeline.jsonl` | monotonic-clock timeline: barrier release, each send, each recv, first-success. |
| `summary.json` | machine-readable outcome (see below). |
| `run.log` | human-readable text log (same events, `INFO`+). |

### 12.1a Log ALL requests — no exceptions

**Every HTTP request the app makes is logged in full, without exception** — not just the bid calls. This
means a request/response record (in `events.jsonl`, an `http/NNN_*.json` file, and `capture.har`) for
each of:

- **preflight** — the token-validity GET (`GetAuctionSimple`);
- **detail** — `GetAuction`, `GetAuctionSimple`, `GetBidsTotalInfo`;
- **clock probes** — every `Date`-header sample (and, if `clock_probe` includes `signalr`, the SignalR
  poll/send/ping traffic and each `RequestClock`). ⚠ **The JWT rides as a `token=` query parameter on
  every SignalR URL** — `redact()` must scrub URL query params, not just header dicts (§12.4);
- **pre-warm / keep-alive** — any warm-up request used to open connections before T;
- **state gate** — the single pre-`T` `GetAuctionSimple` (§9.4) and the post-run state read;
- **audit** — `SetStartBiddingAudit` / `SetSubmitBiddingAudit`;
- **the bids** — every `ValidateBid` and every `CreateBid`, per thread, per wave (including the ones that
  bounce as `DUPLICATE` or `STATE_NOT_STARTED`);
- **any error/retry-free failed attempt** — including requests that timed out or never got a response
  (log the request plus the exception, with `status: null`).

No request is sampled, summarized-away, or dropped. If it left the process, it is in the logs. Assign the
`corr_id` (and `thread_id`/`role`/`wave`) *before* sending so even a request that never returns is
traceable.

### 12.2 `events.jsonl` record schema
```
{
  "ts_wall": "2026-07-28T21:00:00.123456Z",   // wall clock
  "ts_mono_ns": 123456789,                     // monotonic, for ordering/deltas
  "run_id": "…",
  "stage": "preflight|extract|detail|schedule|build|fire|collect|report",
  "level": "DEBUG|INFO|WARN|ERROR",
  "event": "http_request|http_response|clock_offset|state_gate|payload_built|thread_start|
            thread_decision|bid_result|first_success|duplicate|error|summary",
  "thread_id": "D-0|V-1|P-0|main",
  "role": "D|V|P|null",
  "corr_id": "uuid per HTTP exchange",
  "detail": { … event-specific … }
}
```

**Per HTTP exchange, log both sides** (`detail`): `method`, `url`, `request_headers` (redacted),
`request_body`, `status`, `response_headers` (incl. server `Date`), `response_body`,
`t_send_mono_ns`, `t_recv_mono_ns`, `rtt_ms`. For bid calls also lift out `isValid`, `errorType`,
`errorMessage`, `errorMessageLocale`, `minBid`, `currentRankingPosition`, `bidId`.

**Per scheduled attempt, also log** (so timing can be reconstructed to the millisecond): `mode`,
`wave_index`, `scheduled_offset_ms` (planned offset vs T), `actual_offset_ms` (actual send time minus T
on the synced clock), and whether the wave fired before/after `T` and whether the state gate was applied.
Emit a `schedule_planned` event at start listing every wave, and a `wave_dropped` event for each wave
cancelled by `stop_on_success`.

**Decision log** (`thread_decision`): why each thread did what — e.g. "V-1 aborted: ValidateBid
isValid=false errorType=BidErrMin minBid=7.813.974,32 COP", "D-2 skipped: first_success already set".

**Clock/timing:** log the operator-supplied `fire_time`, its parsed `T_wall` and the `T_mono` it was
converted to (plus the conversion instant), `startDate` and the `fire_time`-vs-`startDate` delta,
`lead_ms`, `validate_lead_ms`, `gate_lead_ms`, each wave's computed release time, its actual release
time, and per-thread send/recv deltas from `T`.

**Clock probes are logged as diagnostics and clearly marked as not having moved `T`** (unless
`apply_clock_offset`): every `Date` sample with its `[Date − t_recv, Date + 1 − t_send]` interval, the
intersected `clock_offset_date_header` **with its residual width**, `clock_offset_signalr` if probed, the
disagreement between them, and the host's own NTP status if obtainable. Never log a bare median of
`Date − now` — it is a bias, and recording it as "the offset" is how the original design went wrong.

### 12.3 `summary.json`
```
{
  "run_id": "…", "auction_id": "…", "auction_version": "AuctionbyProposal",
  "fire_time": "2026-07-28T21:00:00Z",        // operator-supplied T (§9.2)
  "start_date": "2026-07-28T21:00:00Z",       // server's, for comparison only
  "fire_time_vs_start_date_ms": 0,
  "clock": {                                   // diagnostics — did NOT move T unless applied
    "applied": false,
    "date_header": {"offset_ms": 49, "residual_width_ms": 130, "samples": 12},
    "signalr":     {"offset_ms": -547, "samples": 0},
    "disagreement_ms": null, "host_ntp": "…"
  },
  "gate": {"mode": "once_before", "issued_offset_ms": -1500, "state": "ReadyToStart", "rtt_ms": 121},
  "mode": "burst", "schedule_planned": [ {"wave": 0, "offset_ms": -60, "direct": 1, "validate": 0,
                                          "validate_only": 0}, … ],
  "waves_planned": 4, "waves_fired": 4, "waves_dropped": 0,
  "submissions_planned": 4, "submissions_sent": 4,   // Σ waves × (direct + validate), planned vs actual
  "outcome": "BID_PLACED | ALL_REJECTED | NOT_FIRED | ERROR",
  "winner": {"thread": "D-1", "role": "D", "wave": 2, "scheduled_offset_ms": 0, "actual_offset_ms": 6,
             "bid_id": …, "server_date": "…", "rtt_ms": 180},
  "bid_value": "1", "items_count": 1, "total_value": "1.000000",
  "my_last_bid": "0.00", "last_bid": "0.00",
  "attempts": [ {thread, role, wave, scheduled_offset_ms, actual_offset_ms, status, isValid,
                 errorType, errorMessage, minBid, rtt_ms}… ],
  "failure_taxonomy": ["BID_ERR_MIN", "STATE_NOT_STARTED", …],
  "config_redacted": { … }, "started_at": "…", "ended_at": "…"
}
```

### 12.4 Redaction rules (enforce centrally)
- **Never** log the raw JWT, full `Authorization` header, or full cookies. Redact JWT to
  `first6…last6`; redact `Authorization` to `Bearer <redacted>`; redact cookie *values*, keep names.
- Provide one `redact(obj)` used by every logger path; unit-test it against a sample request.
- The auction values themselves (bid, company name) are the user's own and may be logged.

### 12.5 Stack
`logging` + a JSON formatter (or `structlog`). One `QueueHandler`/`QueueListener` so concurrent threads
don't interleave-corrupt lines. Use `time.monotonic_ns()` for ordering, wall clock only for display.
Log levels: `DEBUG` (everything incl. bodies) to files, `INFO` to console.

### 12.6 Failure taxonomy enum (tag every failure)
`AUTH_EXPIRED, AUTH_EXPIRING, STATE_NOT_STARTED, STATE_PAUSED, STATE_FINISHED, BID_ERR_MIN,
VALIDATION_FAILED, WAF_BLOCK (403 w/ WAF signature), RATE_LIMIT (429), NETWORK (timeout/conn reset/TLS),
MALFORMED_PAYLOAD (4xx w/ schema error), DUPLICATE (expected bounce after a win), CLOCK_SYNC_FAILED,
PARSE_FAILED (page/identifiers), UNKNOWN`. Map HTTP status + response body → one enum value in a single
classifier function; log the raw evidence alongside the tag.

> **Expected outcomes are not failures.** In `burst`/`custom` modes, `STATE_NOT_STARTED` on a pre-T wave
> and `DUPLICATE` after a win are the *designed* behavior — tag them, count them, but classify the run's
> `outcome` as `BID_PLACED` if any wave succeeded. Only tag a run `ALL_REJECTED`/`ERROR` when no wave landed.

---

## 13. Project structure

```
secop_firstbid/
  __main__.py            # CLI entry; parse config; orchestrate stages
  config.py              # Config dataclass + loading/validation
  models.py              # AuctionRef, AuctionDetail, ItemMeta, BidPayload, BidResult (dataclasses)
  auth.py                # JWT decode, preflight, header/cookie assembly
  session.py             # per-thread Sessions sharing one cookie jar, pool sizing, timeouts, pre-warm
  page_parse.py          # Stage 1: extractors A (onclick) + B (iframe), fallbacks, ambiguity abort (§5)
  detail.py              # Stage 2: GetAuctionSimple / GetAuction / GetBidsTotalInfo → AuctionDetail
  numbers.py             # Decimal helpers: parse Colombian↔wire, format, formulas, round-trip test
  payload.py             # Stage 3: build items (apply value to all), invariant check, serialize bytes
  schedule.py            # Stage 4a: fire_time -> T_wall -> T_mono, wave schedule, monotonic release
  clock.py               # Stage 4b: DIAGNOSTIC probes only (Date interval intersection, signalr)
  gate.py                # Stage 4c: the single pre-T state read + cached verdict (§9.4)
  bidder.py              # Stage 5-6: thread roles D/V/P, barrier, fire, first-success, collect
  endpoints.py           # exact path builders (the // after v1), header sets
  logging_setup.py       # run dir, JSON formatter, queue handler, redaction, taxonomy classifier
  capture.py             # capture.har + timeline.jsonl writers (own the NNN counter; via QueueListener)
  report.py              # summary.json + human summary
  errors.py              # FailureTaxonomy enum + classifier
tests/
  test_numbers.py        # round-trip, formula, invariant (uses the captured auction values)
  test_payload.py        # golden payload vs the §8.5 example
  test_page_parse.py     # BOTH fixtures: beforebid.html (onclick) + bid.html (iframe); §5.5 acceptance
  test_schedule.py       # fire_time parsing (tz-explicit required), T_mono conversion, wave offsets,
                         #   burst formula -> [-60,-30,0,+30], past/too-far-ahead aborts
  test_clock.py          # Date-interval intersection vs the 128 captured samples; asserts a bare
                         #   median is NOT used; residual width reported
  test_detail.py         # §6.2 ItemMeta mapping against a captured GetAuction body (fixture needed)
  test_redaction.py      # incl. a real SignalR poll URL — the JWT is a query param, not a header
```

### Key dataclasses
```
Config(...)                       # §4
AuctionRef(auction_id, lang, market, eauction_doc_id, mkey?)
ItemMeta(item_id, quantity: Decimal, item_unit, item_internal_reference,
         lot_internal_id, lot_name, initial_value: Decimal, min_bid_difference: Decimal,
         is_price_list_item: bool)
AuctionDetail(version_code, type_code, currency, round_type, decimal_num, total_num,
              minimum_bid_type, start_price: Decimal, start_date, end_date,
              state_code, exec_state_code, allow_bids_below_best: bool, test_auction: bool,
              items: list[ItemMeta])
BidItem(...wire fields...); BidPayload(...top-level...) -> to_bytes()
BidResult(thread_id, role, status, is_valid, bid_id, error_type, error_message,
          min_bid, ranking, t_send_mono, t_recv_mono, rtt_ms, raw)
```

---

## 14. Build order (milestones for the coding agent)

1. **numbers.py + tests** — Decimal parse/format, the two formulas, invariant, round-trip. This is the
   riskiest logic; nail it first against the §8.5 golden values.
2. **models.py, config.py, endpoints.py** — types and exact paths.
3. **auth.py + session.py** — JWT decode, preflight, shared session, sticky cookies, redaction hooks.
4. **logging_setup.py + errors.py** — get exhaustive logging in place *before* any live calls, so the
   first real request is fully captured.
5. **page_parse.py + tests** — extractors A and B, concatenation-aware fallbacks, ambiguity abort. The
   §5.5 acceptance test against **both** fixtures is the gate; it must pass before anything downstream.
6. **detail.py + test_detail.py** — the three GETs → `AuctionDetail`; log `myLastBid`/`lastBid` and the
   §7 warning. Capture a real `GetAuction` body into `tests/fixtures/` on first contact — §6.2's mapping
   is client-code-only and this is the only thing that will verify it (§15 Q7).
7. **payload.py + tests** — build all-items payload; assert invariant; golden-match §8.5.
8. **schedule.py + test_schedule.py** — `fire_time` → `T_wall` → `T_mono`, wave offsets, release. This is
   now pure local-clock arithmetic with no network dependency, so it is fully unit-testable.
9. **clock.py + gate.py** — diagnostic probes (interval intersection, logged not applied) and the single
   pre-`T` state read. SignalR probe optional/later and explicitly unbuilt (§9.3).
10. **bidder.py** — build the firing schedule from `mode` (§11.4: `single`/`burst`/`custom` → list of
    waves), then per-wave roles, barrier, pre-warm, fire, schedule-wide first-success, `stop_on_success`
    cancellation, collect. Start with `dry_run` (validate-only) end-to-end, then enable `CreateBid`.
    Implement `single` first; add `burst`/`custom` once the single wave is proven.
11. **report.py** — summary.json + human summary.
12. **Dry-run rehearsal** on a live auction with `D=0,V=0,P=1` (probe only) to validate identifiers,
    payload acceptance, the `fire_time` path end-to-end, the clock diagnostics, and the `minBid` the
    server returns — **before** ever firing a real `CreateBid`.

---

## 15. Consolidated open questions (resolve on first live run; keep them in the logs)

1. **Decrement semantics** — is a single low bid legal in one step (`≤ lastBid − minDiff`), and is the
   baseline your last bid or the best? (`myLastBid==0` blocks everything — §7.)
2. **`CreateBid` success shape** — never observed; confirm it matches `ValidateBid`.
3. **Concurrency tolerance** — does the server accept a burst of identical bids or reject/duplicate them?
4. **Tiebreak by time?** — decides whether D-thread racing has value.
5. **Pre-start `ValidateBid`?** — governs `validate_lead_ms`.
6. **WAF on the API host** — confirm a browser UA is sufficient and a small burst isn't throttled.
7. **`GetAuction` real shape** — the item field names in §6.2 are client-code-inferred; verify against a
   live response and adjust the mapping if the API differs from what the SPA consumes.
8. **Rounding mode** for inexact `itemQuantityUnitPrice` divisions. Note the original mitigation was
   wrong: *integrality* is irrelevant, **divisibility** is what matters. `itemQuantityUnitPrice`
   terminates iff `bidValue` divides `initialValue × quantity` to ≤ 12 dp — and for
   `initialValue = 390698716`, six of the first twelve integers (3, 6, 7, 9, 11, 12) are
   non-terminating. Compute the residual at build time and **WARN** with it.
9. **Pre-open bids** — does a `CreateBid` fired before `Started` just get a clean `STATE_NOT_STARTED`
   rejection, or does an early/rapid attempt trip any anti-abuse counter? This decides how aggressive
   `pre_ms`/`interval_ms` can safely be. **Nothing in the capture bears on this** — it begins 7 m 40 s
   after the open and never returns `ReadyToStart`.
10. **Rate-limit threshold** — how many submissions in how short a window before `RATE_LIMIT`/`WAF_BLOCK`?
    This bounds `bid_count` and the schedule density.
11. **SignalR handshake** — `negotiate` was never captured and `connectionToken` cannot be derived from
    anything we hold. Required before the sub-second `signalr` clock probe (§9.3) is buildable at all.
12. **`bid_is_total` semantics** — keep or delete? It contradicts §7, makes `bidValue` per-item, and
    inherits Q8's rounding problem. Never exercised: every captured item has `quantity == 1`.
13. **Which clock does the auction state machine use?** The `Date` clock and the SignalR hub clock have
    non-intersecting feasible offsets (~0.5 s apart). Neither is evidenced as the one that decides when
    `auctionStateCode` flips. Until this is answered, `apply_clock_offset` should stay `false` and the
    operator-supplied `fire_time` remains the only defensible source of `T` (§9.1).
14. **Host-clock accuracy at `T`** — with a manual `fire_time` the residual error is host-vs-server. Log
    NTP status before each run; on the first live open, record the delta between your `T` and the
    observed state flip. That number is what actually calibrates future runs.

---

### Appendix A — Reference values from the captured auction
```
auctionId            d6a49a23-3556-4b3d-9bb4-3e209279b9f8
eauction doc id      CO1.EAREQ.937510      opportunity   CO1.OPDOS.31397606
market / lang        GovCo / es-CO
versionCode          AuctionbyProposal     typeCode  Reversed   evaluation  BestPrice
startPrice           390698716.0 COP       decimalNum 2   roundTypeCode Up   totalNum 20
minimumBidType       1                     minBid returned 7.813.974,32 COP  (= 2% of startPrice)
window               2026-07-28T21:00:00Z → 21:30:00Z  (test auction; testAuction=true)
item                 id 8552d67b-29ce-4b30-877b-e361fd876072  qty 1.000000  unit UN
                     lotInternalId CO1.OTLN.74514427  initialUnitPrice 390698716
GetBidsTotalInfo     myLastBid 0.0  lastBid 0.00  score 0.0   (why all 8 test bids failed)
```

### Appendix B — What the source capture does and does not prove
- **Proven from the wire:** `ValidateBid` request/response shape, headers, SignalR clock, audit logging
  with IP, the decrement error, `GetAuctionSimple`/`GetBidsTotalInfo` shapes.
- **From client code only:** the Bearer scheme, `CreateBid` being the real submit and its response
  shape, the `GetAuction` item field names, the payload builder formulas.
- **Not captured at all:** any *successful* bid, the login/token-mint flow, the full `GetAuction` body,
  `Authorization`/`Cookie` headers (stripped by the sanitized HAR).
```
