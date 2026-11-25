# The SECOP II e-auction API

Reference for the HTTP surface this app talks to. Every fact is marked **Observed** (seen on the wire
in the capture), **Client code** (read out of the SPA's JavaScript — proves what the browser *sends*,
never what the server *enforces*), or **Inferred**.

The SPA itself is an AngularJS 1.x app last modified around September 2020, served from
`https://subastas.secop.gov.co/eauctions.site/`.

## Transport

| | |
|---|---|
| API host | `https://subastas.secop.gov.co` |
| Base path | `/eauctions.api/v1//` — **the double slash after `v1` is real** |
| Auth | `Authorization: Bearer <JWT>` on every v1 call |
| POST content type | `application/json;charset=UTF-8` |

The double slash comes from the SPA computing `serviceBase = apiBaseUrl + apiVersion + '/'`. It is
present on **146 of 146** v1 requests in the capture. `endpoints.url()` does not normalise it, and you
must not "fix" it.

The slash count is **not** uniform across the service. The SignalR routes are
`/eauctions.api/signalr/signalr/poll` (single) but `/eauctions.api//signalr/signalr/send` and
`…//signalr/signalr/ping` (double). Do not normalise those either — but see
[SignalR](#signalr-not-implemented), which this app does not implement.

## Authentication

**Observed** via header-size accounting; **Client code** for the mechanism.

The SPA's `authInterceptorService` attaches `Authorization: Bearer <token>` to every `$http` call and,
on a 401, redirects to the STS login page. This app treats **401 as "token dead"** — it stops, does not
retry, and tells you to re-harvest.

The token is RS256, issued by `https://community.secop.gov.co/STS/users/issue.aspx` (WS-Federation,
password login), with a 24-hour lifetime (`exp − nbf = 86400`). It is injected server-side into the
launch page as the `data-item-token` attribute on `#div-item-data`.

**There is no non-interactive refresh path.** This is the binding constraint on the entire approach:
a run depends on a manually harvested token good for one session of at most 24 hours.

### Claim names are URIs

Only `iss`, `aud`, `nbf`, `exp`, `unique_name`, `authmethod` and `auth_time` are bare keys. All 15
`LoggedIn*` / `LoginGUID` claims are prefixed with `http://www.nextway.com/NextWay.STS.Claim/`, so
`claims["LoggedInCompanyCode"]` raises `KeyError`. Use `auth.claim()`, which tries both forms.

| Claim | Example | Use |
|---|---|---|
| `exp`, `nbf` | unix seconds | preflight and countdown — **the only operationally load-bearing claims** |
| `aud` | `https://subastas.secop.gov.co/eauctions.site/` | sanity check |
| `iss` | `https://community.secop.gov.co/STS/users/issue.aspx` | where the token is minted |
| `LoggedInCompanyCode` | `900000000` | **this is the `userCode` in the server's audit log** |
| `LoggedInCompanyName` | `UT MIN SAMLUB 2026` | logging |
| `LoggedInUserId`, `LoggedInUserName` | | logging |
| `LoggedInUserCode` | `3212081` | distinct from `LoggedInCompanyCode`; don't confuse them |
| `LoginGUID` | | correlation |

### Sticky session

**Inferred.** An Azure Application Gateway sets `ApplicationGatewayAffinity` and
`ApplicationGatewayAffinityCORS` cookies for backend affinity. All threads share one cookie jar so a
concurrent burst lands on the same backend and sees consistent state. `session.py` captures the jar
from the preflight response and reuses it everywhere.

## Headers

The captured `ValidateBid` request carries 16 headers. Marked **(L)** = your HTTP library emits it and
you should set nothing.

```
Accept: application/json, text/plain, */*
Accept-Encoding: gzip, deflate, br, zstd                       (L)
Accept-Language: es-US,es-419;q=0.9,es;q=0.8,en;q=0.7
Connection: keep-alive                                          (L)
Content-Length: <n>                                             (L)
Content-Type: application/json;charset=UTF-8                    POST only
Host: subastas.secop.gov.co                                     (L)
Origin: https://subastas.secop.gov.co                           POST only
Referer: …/eauctions.site/?auctionId={id}&languageCode={lang}&marketCode={market}
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36
sec-ch-ua: "Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "macOS"
```

`Origin` is **POST-only** — present on all 287 POSTs, absent from all 142 GETs.

The client-hint (`sec-ch-ua*`) and fetch-metadata (`Sec-Fetch-*`) headers are exactly the surface a
UA-sniffing WAF cross-checks against the `User-Agent` you are spoofing, which is why `endpoints.py`
sends all six rather than a plausible-looking subset.

Endpoint-conditional extras:

- `GetAuctionSimple` and `Bid/GetBidsTotalInfo` also send `Cache-Control: max-age=0` and
  `If-Modified-Since: Mon, 26 Jul 1997 05:00:00 GMT`.
- `X-Requested-With: XMLHttpRequest` appears on SignalR and `GetEventLogs` **only**. Do not send it on
  bid calls.
- `SetStartBiddingAudit` uses `Content-Type: application/json;charset=utf-8` — note the **lowercase**
  `utf-8`, unlike every other POST — with `Content-Length: 0` and an empty body.

**WAF.** Requests with a library default User-Agent (`python-requests/…`) get HTTP 403. This is
**asserted from outside this capture** — all 429 captured responses are 200 and no library UA was ever
sent. Treat it as a reason to send a realistic UA, not as a measured fact.

## Endpoints

### `GET auction/GetAuctionSimple/{id}` — **Observed** (128×)

Auction configuration, state and timing. Returns 59 keys. The ones this app reads:

`auctionVersionCode, auctionTypeCode, currencyCode, roundTypeCode, decimalNum, totalNum,
minimumBidType, startPrice, startDate, endDate, auctionStateCode, auctionExecutionStateCode,
allowBidsBellowBestBid, testAuction` (note the doubled `l` in `Bellow` — that is the server's spelling).

`auctionStateCode ∈ {ReadyToStart, Started, Finished, Canceled, Paused, Deprecated}`.
`auctionExecutionStateCode` was `Running` on **128 of 128** responses, including all 125 after the
auction had flipped to `Finished` — so it is very nearly useless as a gate condition.

`timeZoneCode` is `null` in the capture, which is why there is nothing to infer a zone from and why
`fire_time` must carry an explicit offset.

This is also the state gate, and at a median 135.3 ms it is expensive — see
[evidence.md](evidence.md#timing-evidence).

### `GET auction/GetAuction/{id}/{lang}` — **Observed** (2026-07-29)

The item list. Returns `biddingList[]`; entries with `containtItems` (the server's spelling) carry
`biddingPriceListItems[]` and `biddingQuantitativeItems[]`, which the SPA flattens and sorts by
`displayItemOrder`.

The body is committed as `tests/fixtures/getauction.json` (the `bidders` array — 42 other companies
— was emptied first). It **corrected the client-code-derived mapping on two fields**:

| Bid-item field (wire) | Source field | Note |
|---|---|---|
| `itemId` | **`id`** | *not* `itemName` — that field does not exist on the wire at all |
| `quantity` | `itemQuantityStr` | keep the **string** — it carries the 6-dp form `"1.000000"` |
| `itemUnit` | `itemUnit` | e.g. `UN` |
| `itemInternalReference` | `itemInternalReference` | non-price-list items use `itemDescription` |
| `lotInternalId` | **the item's `biddingListId`** | the *entry's* `lotInternalId` reads `ALLLOTS`, a marker, not a lot id |
| `lotName` | entry `lotName` ‖ `groupReference` | |
| (formula input) | `itemInitialUnitPriceStr` | per-unit initial price |
| `minBidDifference` | `minBidDifference` | per-item minimum decrement |

The `itemName` error has a tidy explanation: `auctionService.js` renames `id` while flattening
`biddingList` into `self.Items`, so reading the SPA's builder described its *internal model* rather
than the response.

A price-list item carries exactly these keys:

```
id  internalId  biddingListId  displayItemOrder  hasDecimalPlace  itemDescription
itemInitialPrice  itemInitialPriceStr  itemInitialUnitPrice  itemInitialUnitPriceStr
itemInternalReference  itemQuantity  itemQuantityStr  itemUnit  minBidDifference  minBidDifferenceStr
```

**Verified end to end:** the `ItemMeta` parsed from this body, run through `payload.build_payload`,
reproduces all 8 captured `ValidateBid` bodies byte for byte.

Only one auction has been seen, and it has a single item at quantity 1. A body with several items,
several lots, or a chapter is still unobserved.

### `GET Bid/GetBidsTotalInfo/{id}` — **Observed** (1×)

Note the **capital `B`**. Returns `{auctionId, myLastBid, lastBid, score}`. In the capture:
`myLastBid: 0.0, lastBid: 0.00, score: 0.0`. That was once read as the reason all eight test bids were
rejected; it is not one — on 2026-11-20 the accepted bids were placed from the same reading. After
those auctions it still read `myLastBid: 0.0, lastBid: 0.0` with a standing bid of zero, so `lastBid`
cannot tell "no bid" from "a bid of 0".

### `GET bid/GetSupplierBids/{id}/{companyCode}/{lastBidId}` — **Observed 2026-11-20**

Not in the original capture; the auction page requests it, and a read with `lastBidId = 0` after an
auction answered 200 with a JSON **list** of bids. Each carries `bidId`, `company` (a company code),
`date` (the application's timestamp, seven fractional digits), `totalValue` / `totalValueStr`,
`isValid`, `errorMessage`, `auctionTotalBids`, `supplierTotalBids`, `currentRankingPosition`, and
`lots[].items[]` with per-item rankings. A supplier's token was shown **another company's** accepted
bid, which makes this the record of who won and when. `records.read_result` reads it after every run.

One browser request to this route took 36 s; it is slow under load.

### `POST bid/ValidateBid` — **Observed** (8×)

Pre-check. Takes the full bid object, same builder as `CreateBid`.

**This writes to the audit log.** It is not a free probe. See
[evidence.md](evidence.md#the-audit-log--why-dry-run-is-not-free).

### `POST bid/CreateBid` — route confirmed, **no submission ever made**

The real submission. Same request object as `ValidateBid`.

**The route resolves.** Confirmed 2026-07-29 without sending a bid: a `GET` to this POST-only route
answers **405**, while `bid/CreateBidZZZ` answers 404 and `bid/createbid` also answers 405 — so the
path is right and routing is case-insensitive. That retires the "404 at the fire instant" risk.

**The server processes what this app sends.** On 2026-11-20 some 55 `CreateBid` requests left the app
across two auctions; each one that reached the server left a `Logs_Search_NewBid` row with the
ordinary rejection text. So the path, the headers and the payload shape are accepted as a bid.

**What a POST returns is still unknown.** Not one of those requests was answered inside its timeout.
The response is assumed to match the `ValidateBid` shape because the SPA feeds both from one builder.
`OBSERVED_ON_WIRE` stays `False`: no response from this route has ever been seen.

### `POST audit/SetStartBiddingAudit?auctionId={id}` — **Observed** (8×)

Empty body, `Content-Length: 0`. Response is 200 with an empty body.

In the browser it fires **once per bid attempt**, concurrently with `ValidateBid` at Δ ≤ 1 ms (8/8) —
not once per session. The app sends it **once per run**, on a connection no bid uses: one per thread
shared each bid's connection and delayed bids by seconds on 2026-11-20. It writes a
`Logs_Search_StartBidding` row ("Comenzar Lances") stamped by the application clock, which is also
what the app reads back to place that clock. Whether a bid needs the row at all is
[Q15](open-questions.md).

### `POST audit/SetSubmitBiddingAudit?auctionId={id}` — **Client code**, never observed

Appears in **0 of 429** entries. Worse, the SPA's function names are inverted relative to the
endpoints they post to — `auditStartBidding()` posts `SetSubmitBiddingAudit` and vice versa — so this
is probably the *session-open* audit, not the post-submit one. `do_submit_audit` therefore defaults to
**false**. Wire things up by endpoint, never by the JS function name.

### `GET audit/GetEventLogs?auctionId={id}&…` — **Observed** (1×)

The server-side audit log, DataTables-shaped. Every row carries your **public IP** and company code.
This is the endpoint that proves the footprint of everything else.

## The bid object

Identical for `ValidateBid` and `CreateBid`. **Key order matters for fidelity** — this is the observed
wire order and `models.BidPayload.to_dict()` reproduces it.

```json
{
  "auctionId": "d6a49a23-3556-4b3d-9bb4-3e209279b9f8",
  "currency": "COP",
  "Date": "2026-07-28T21:19:31.751Z",
  "ranking": -1,
  "items": [
    {
      "itemId": "8552d67b-29ce-4b30-877b-e361fd876072",
      "quantity": "1.000000",
      "isPriceListItem": true,
      "isChanged": true,
      "itemInternalReference": "1",
      "lotInternalId": "CO1.OTLN.74514427",
      "lotName": "",
      "itemUnit": "UN",
      "totalValue": "1.000000",
      "bidValue": "1",
      "itemQuantityUnitPrice": "390698716.000000000000",
      "bidded": true
    }
  ],
  "totalValue": "1.000000",
  "languageCode": "es-CO"
}
```

Things that are easy to get wrong and are load-bearing:

- **Capital `D` in `Date`** at the top level, while the response echoes a lowercase `date`. The server
  ignores what you send (it echoes `"0001-01-01T00:00:00"`), but send a plausible value anyway. The
  format is JavaScript's `toISOString()`: exactly **3** fractional digits and a `Z`. Python's
  `isoformat()` gives 6 digits and `+00:00`, which is why `payload.js_iso_now()` exists.
- **`ranking` is a JSON number (`-1`). Every money and quantity field is a JSON string.**
- `totalValue` precedes `bidValue` in the item object.
- **Never strip trailing zeros.** Decimal scale *adds* on multiply, and that is what reproduces the
  wire: `Decimal("1.000000") × Decimal("10")` → `"10.000000"`, and
  `× Decimal("7813974.32")` → `"7813974.32000000"`.
- `itemQuantityUnitPrice` is quantized to exactly **12** decimal places. It is an *inverse ratio*, not
  a unit price: `initialValue × quantity ÷ bidValue`. Its product with `bidValue` is therefore
  invariant, which is worth remembering when a bid-value change appears to have no effect on it.

`auctionVersionCode` decides the payload shape. `AuctionbyProposal` — the captured case and the only
one this app supports — sends **one** `CreateBid` whose `items[]` holds every price-list item.
`AuctionbyItem` and `AuctionbyLot` bid per item or per lot; `payload.build_payload()` hard-aborts on
them rather than guessing.

## Bid responses

22 keys, observed on `ValidateBid`; `CreateBid` is **assumed** to match.

```
internalId, auctionId, company, date, totalScore, priceListScore, totalValue, totalSumUnitPrice,
auctionTotalBids, supplierTotalBids, bidId, totalValueStr, totalSumUnitPriceStr,
currentRankingPosition, isValid, errorMessage, errorMessageLocale, minBid, errorType, lots, items,
globalRanking
```

**Success predicate**, one definition used everywhere in this codebase
(`bidder.is_success`): `isValid is True and errorMessage is empty`. The SPA uses the same conjunction.
Two of the three places the plan stated it dropped the `errorMessage` half; they are reconciled here.

Known error values: `errorType: "BidErrMin"`, `errorMessageLocale:
"BidWarn_Total_MinBid_Diff_Desc"`. Any new code is logged verbatim rather than mapped away.

`minBid` comes back in **Colombian** format with a currency suffix — `"7.813.974,32 COP"` — while
everything you *send* is in wire format. `numbers.parse_server_money()` converts;
`numbers.parse_user_decimal()` handles operator input. Never mix the two.

### A duplicate is indistinguishable from a rejection

A post-win bid that bounces because it is not lower returns **the same status and a byte-identical
body** as a genuine `BidErrMin` rejection. No classifier can separate them from `(status, body)` alone.
`errors.classify()` therefore takes a `RunState` and checks `sent_after_first_success` *before*
`errorType`. Local ordering is the only disambiguator that exists.

## SignalR (not implemented)

The hub carries only `Connect(auctionId)` and `RequestClock`/`ReceiveClock`. **Bidding does not go
through it**; it is plain REST. The hub clock is the only sub-second server time available, which is
the sole reason it is interesting.

This app does not implement it, and `clock.probe_signalr()` raises `NotImplementedError` saying so.
The blocker is concrete: the handshake was **never captured** — `negotiate`, `start` and `abort` have
zero hits across all 429 URLs — and `connectionToken` (182 chars, URL-encoded) is already minted on
the first captured request. It cannot be derived from anything available and would have to be reversed
out of `jquery.signalR` 2.x first.

If you ever build it, the capture does pin down the rest: the path slash counts differ per endpoint
and must not be normalised; POST bodies are `application/x-www-form-urlencoded` with the JSON under
`data=`, **not** the JSON content type used everywhere else; auth is the `token=` query parameter and
**not** Bearer; each poll carries a `messageId` cursor echoed back in the response's top-level `C`
field; 15 of 142 polls return an empty `M` keepalive, which is not a failure; and one response
delivered the same `ReceiveClock` twice, so you must dedupe.

**The `token=` query parameter is the raw JWT in plaintext.** Anything that logs a SignalR URL leaks a
live 24-hour credential. `logging_setup.redact_url()` exists for this.
