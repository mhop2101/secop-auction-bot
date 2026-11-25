# Evidence coverage — what the capture proves, and what it doesn't

Everything this app does traces back to one Chrome-exported HAR (`www.secop.gov.co.har`, 429 entries)
and two saved HTML pages. This page states, in one place, which claims rest on observed bytes and
which rest on reasoning, so that no section of the code or docs can quietly read as settled when it
isn't.

Read this before changing anything in `clock.py`, `bidder.py`, or `schedule.py`. Those three modules
are the ones whose design is least constrained by evidence.

## The source capture

| | |
|---|---|
| File | `www.secop.gov.co.har` — 22 MB, 429 entries, Chrome `WebInspector` export |
| Auction | `d6a49a23-3556-4b3d-9bb4-3e209279b9f8`, "SUBASTA DE PRUEBA VIGILANCIA_Subasta electrónica" |
| Nature | **A practice auction.** `testAuction: true` |
| Window | `startDate 2026-07-28T21:00:00Z` → `endDate 21:30:00Z` |
| Capture starts | 21:07:40Z — **7 m 40 s after the auction opened** |
| Sanitisation | `Authorization`, `Cookie` and `Set-Cookie` headers stripped by the exporter |
| Response statuses | `{200: 429}` — **zero** non-200 responses of any kind |

**The HAR is a credential-bearing artifact.** The bearer token was *not* redacted: it rides as a
plaintext `token=` query parameter on all 281 SignalR `poll`/`send`/`ping` URLs, alongside the
operator's name, user and company codes, and `LoginGUID`. It is in `.gitignore`, and
`logging_setup.redact_url()` exists specifically because of it.

## Endpoint coverage

Five routes were seen on the wire. Eleven more are known only from the SPA's JavaScript.

| Route | Times observed | Status |
|---|---|---|
| `GET auction/GetAuctionSimple/{id}` | 128 | **Observed** |
| `POST bid/ValidateBid` | 8 | **Observed** |
| `POST audit/SetStartBiddingAudit` | 8 | **Observed** |
| `GET Bid/GetBidsTotalInfo/{id}` | 1 | **Observed** |
| `GET audit/GetEventLogs` | 1 | **Observed** |
| `POST bid/CreateBid` | **0** | Route confirmed live 2026-07-29 (405 vs 404); **no bid ever submitted** |
| `GET auction/GetAuction/{id}/{lang}` | **0 in the HAR** | **Captured live 2026-07-29** → `tests/fixtures/getauction.json` |
| `POST bid/GetMinBid` | 0 | Client code only |
| `POST audit/SetSubmitBiddingAudit` | 0 | Client code only |
| everything else in `apiService.js` | 0 | Client code only |

`bid/CreateBid` — the call that actually places the bid — **has still never been sent**. Its request
shape is confidently reconstructed because the SPA feeds `ValidateBid` and `CreateBid` from the same
builder, and as of 2026-07-29 the *route* is confirmed to exist, so the "404 at the fire instant"
risk is retired. What remains unknown is what a successful POST returns.
`endpoints.OBSERVED_ON_WIRE` records what the capture contains per route, and `report.py` prints the
unverified ones into every run summary.

## Live probe, 2026-07-29

Three things were settled against the live service with read-only requests. None of them submitted a
bid or wrote an audit row.

**The `CreateBid` route resolves, and casing is irrelevant.** A `GET` to a POST-only route returns
405 when the path is right and 404 when it is not:

```
GET …/v1//bid/ValidateBid    -> 405   control: known-good route
GET …/v1//bid/CreateBid      -> 405   resolves
GET …/v1//bid/createbid      -> 405   routing is case-insensitive
GET …/v1//bid/CreateBidZZZ   -> 404   control: a bogus sibling really does 404
```

**`GetAuction` was captured**, and corrected the item mapping on two fields — the item GUID is `id`
(not `itemName`, which does not exist on the wire) and the lot id is the item's `biddingListId` (the
entry's `lotInternalId` reads `ALLLOTS`). The `ItemMeta` parsed from it rebuilds all 8 captured bid
bodies byte for byte, so `GetAuction → ItemMeta → wire` is now evidence end to end.

**The User-Agent check is real.** Same URL, same moment, no credentials, only the UA differs:
`curl` default → **403**; Chrome UA → **302**.

## Timing evidence

Measured over the 128 `GetAuctionSimple` exchanges (this is what the state gate costs, since the gate
*is* a `GetAuctionSimple`):

| statistic | `timings.wait` (ms) | total `time` (ms) |
|---|---|---|
| min | 85.7 | 90.3 |
| p10 | 91.4 | — |
| p25 | 98.9 | — |
| p50 | 128.9 | 135.3 |
| p75 | 251.9 | — |
| p95 | 595.9 | — |
| max | 1123.0 | 1138.3 |

`ValidateBid` (n=8): min 168.5, p50 614.4, max 1633.1 ms total — of which up to 1627.1 ms is `wait`.
That time is server-side validation work, **not transit**, which is why `lead_ms` must never be
derived from it: leading by ~600 ms would make the request arrive before the auction opens.

Two consequences the code encodes:

- **The state gate cannot run inline.** At a median of 135.3 ms it is 1.5×–12× the entire span of the
  recommended 90 ms burst schedule. It runs once, ahead of `T`, and its verdict is cached
  (`gate.run_gate`).
- **`stop_on_success` is inert at the recommended schedule densities.** The fastest bid-endpoint
  response anywhere in the capture is 168.5 ms; a 90 ms burst finishes firing before wave 0 can
  possibly return a verdict. `bidder.py` logs this at INFO rather than implying waves get dropped.

Only **3 of 429** entries have a non-zero `connect` time, and two of them coincide with the browser
issuing two requests in the same millisecond:

```
21:13:41.486  SetStartBiddingAudit  connect=184.168  ssl=111.155
21:27:03.011  SetStartBiddingAudit  connect=167.433  ssl= 78.309
```

A *second concurrent* request forces a fresh TCP+TLS handshake costing 167–184 ms — roughly twice the
whole recommended schedule span, and it would be paid by exactly the threads that are supposed to win
a timestamp tiebreak. This is why `session.prewarm()` fires N *concurrent* GETs rather than looping —
and, since connections warmed at arming did not survive to `T` on 2026-11-20, why `session.keep_warm()`
repeats the round shortly before the first release.

## Clock evidence

**The HTTP `Date` header is whole-second.** 429 of 429 responses carry `Date`; **0** have a fractional
part. So `Date − local_now` is a *biased* estimator with expected error around −0.5 s, not a noisy
centred one, and a median converges to the bias instead of removing it.

Recomputed here from the 128 samples, taking `t_send = startedDateTime + blocked + dns + connect +
send` and `t_recv = startedDateTime + time`, with each sample bounding the offset to
`[Date − t_recv, Date + 1s − t_send]`:

| method | result |
|---|---|
| Marzullo intersection of all 128 intervals | `[−0.023, +0.441] s`, width **464 ms**, midpoint **+0.209 s** |
| naive `median(Date − t_recv)` | **−0.458 s** |
| disagreement | **667 ms** |

`PLAN_AUDIT.md` quotes a tighter interval (`[−0.016, +0.114] s`) from the same data; the difference is
in how `t_send` is reconstructed from the HAR timings. Both agree on the point that matters: the naive
estimator lands hundreds of milliseconds away from what its own data supports, and the residual width
is wider than any schedule span the app builds. `tests/test_clock.py` pins the disagreement at
> 300 ms as a regression test.

The SignalR hub clock is a **different clock** with 100 ns resolution and a median offset of −0.547 s
over 128 `ReceiveClock` samples. Its feasible interval does not intersect the `Date` clock's. Neither
is tied by any evidence to the clock the auction state machine actually uses.

**This is why `fire_time` is operator-supplied and mandatory.** A manual `T` does not create accuracy;
it relocates the error to your host clock, where NTP puts you within single-digit milliseconds and you
can actually measure it. `apply_clock_offset` exists and defaults to `false`.

**Two real opens have now been observed (2026-11-20), and they add a third clock.** The application
stamps its event-log rows and its bids with a clock that is neither of the above: it read 0.9–1.0 s
*behind* NTP in both auctions, where `Date` tracked real time. The accepted bids carry stamps 32 and
44 ms after `startDate` on it. It is measured the way a `Date` sample is — a server stamp bracketed by
a request's send and receive instants — except that the stamp has sub-millisecond digits, so the
interval is one round trip wide instead of a second plus one. See
[timing.md](timing.md#secops-two-clocks). Two auctions on one day is thin: the switch stays off by
default and the run declines to act on a measurement it cannot stand behind.

## Auction state evidence

`auctionStateCode` across the 128 polls: `Finished` ×125, `Started` ×3.
`auctionExecutionStateCode`: `Running` ×**128** — including all 125 responses *after* the auction had
already flipped to `Finished`. A gate condition of `!= "Paused"` is therefore permissive past the end
of the auction and contributes almost nothing.

The close lag is bracketed, not measured: `Started` at 21:30:02.506Z, `Finished` at 21:30:11.502Z,
against a nominal `endDate` of 21:30:00Z. The poll cadence is a median of exactly 3.000 s (3.0 ×54,
2.999 ×29, 3.001 ×24), so the flip is bounded to `(+2.5 s, +11.5 s]`. This describes the **close**,
about which there is data — not the **open**, about which there is none.

## The audit log — why "dry run" is not free

`GetEventLogs` (`recordsTotal: 23`) returns rows like:

```
Logs_Search_NewBid       2026-07-28T21:19:31.4447569Z  ip 203.0.113.10  userCode 900000000
  "Error cuando presentado el lance. Problema: El valor total tiene que ser inferior…"
Logs_Search_StartBidding 2026-07-28T21:19:31.3257602Z  ip 203.0.113.10  userCode 900000000
  "Comenzar Lances"
```

`grep -c CreateBid` over the whole 22 MB HAR is **0**. Those rows can therefore only have come from
`ValidateBid` and `SetStartBiddingAudit`. So:

> A "probe-only" or `dry_run` rehearsal on a live auction writes rows **labelled as failed bid
> submissions**, under your public IP and company code, into a permanent event log the auction owner
> can read.

`config.validate()` emits this as a warning on every dry run, and `config.audit_footprint()` counts
every `ValidateBid` and `CreateBid` plus the run's one start audit — not just the submissions.
A `burst` of 4 with `wave_direct=1` writes **5** audited rows, not 4.

The `userCode` in those rows, `900000000`, is the JWT's `LoggedInCompanyCode`. There is no anonymity
here.

## The bid rejections

Eight `ValidateBid` attempts, four distinct values: `10` ×4, `1` ×2, `200` ×1, and `7813974.32` ×1.
All eight returned `isValid: false`, `errorType: "BidErrMin"`, `minBid: "7.813.974,32 COP"`.

The fourth value is the probative one: **`7813974.32` is exactly the `minBid` the server had been
returning, and it was rejected with a byte-identical error.** That kills the reading in which `minBid`
is a floor you simply have to clear.

The likely cause is that `GetBidsTotalInfo` reported `myLastBid: 0.0` / `lastBid: 0.00` — the company
had no standing bid for the server to decrement from — and the server's own message names the
baseline: *"el valor total tiene que ser inferior al valor de su último lance."* This is **inferred**,
not observed; the contemporaneous supporting evidence is `auctionTotalBids: 0` / `supplierTotalBids: 0`
in all 8 responses. It is why the app only ever logs a rejection and never adjusts a value or retries.

**That inference did not survive.** On 2026-11-20 the one accepted bid in each of two live auctions
was a first bid — a zero — from a company with no standing bid, so "no standing bid of yours" is not
what rejects a bid. The eight rejections above, on an auction with no bids at all, are therefore
**unexplained**; the auction's state at the time is the obvious thing nobody checked. It is a clean
example of this project's recurring defect: a cause read off the data, never tested against an
outcome, and repeated until a live auction contradicted it.

## What the capture contains none of

Every item here is a design assumption with zero supporting bytes. They are the reason
`SECOP_FIRSTBID_PLAN.md` §15 exists and the reason the build order ships `single` mode before
`burst`/`custom`.

- **Any successful bid.** `CreateBid` was never called. Its success response shape is assumed.
- **The auction opening.** The capture begins 7 m 40 s after `startDate`; `ReadyToStart` was never
  returned; the `ReadyToStart → Started` transition was never seen.
- **Any pre-open request.** So `STATE_NOT_STARTED` — which the whole "fire before T as insurance"
  rationale depends on being a clean rejection — names a response nobody has ever seen.
- **Any concurrency in bidding.** The capture shows sequential ~3 s polling and nothing else. Whether
  the server tiebreaks by arrival time, and whether it tolerates a burst of identical bids, are both
  unanswered — and they decide whether the threading model buys anything at all.
- **Any 429 or rate limit.** Nobody has escalated deliberately, so the threshold is unknown. (The
  403-on-library-UA half of this was settled on 2026-07-29 — see the live probe above.)
- **The login / token mint flow.** The JWT's `iss` is
  `https://community.secop.gov.co/STS/users/issue.aspx` (WS-Federation, password, 24 h). There is no
  non-interactive refresh path, which is the binding constraint on this whole approach.
- **The `GetAuction` body.** Every item field name in `detail.parse_items()` comes from the SPA's
  JavaScript, not from a response. `tests/fixtures/getauction_synthetic.json` is synthetic and marked
  as such.
- **A second bidder**, and any auction with more than one item or a quantity other than 1.

## The quantity problem

Every captured bid has exactly one item at `quantity == "1.000000"`. The documented formula
`itemQuantityUnitPrice = initialValue × quantity ÷ bidValue` reproduces all 8 wire values byte for
byte — but so do `initialValue ÷ bidValue`, `initialValue × quantity² ÷ totalValue`, and
`initialValue ÷ (bidValue × quantity)`. With quantity fixed at 1, **the quantity factor is
non-discriminating**: the capture cannot tell these apart.

What is actually proven is the narrower `itemQuantityUnitPrice × bidValue = initialValue`.

`numbers.check_invariant()` says so in its docstring, `payload.build_payload()` emits a loud warning
on any item with `quantity != 1` and logs the competing formula's value beside its own, and
`tests/test_numbers.py` contains an explicit test demonstrating that the two formulas diverge at
`quantity = 2`. Do not remove that test to make the suite tidier; it is the marker for a real unknown.

Relatedly, `SECOP_FIRSTBID_PLAN.md` Appendix A lists `initialUnitPrice 390698716` on the *item* line.
No item-level field carrying that value exists anywhere in the HAR — all 130 occurrences are
`"startPrice": 390698716.0`, an *auction*-level field. It is derived, and it coincides with
`startPrice` only because there is a single item at quantity 1.

## Provenance vocabulary

`PLAN_AUDIT.md` observed that three labels (Observed / Client-code-only / Inferred) were not enough,
because the cases that actually caused defects were:

- **Observed but non-discriminating** — the quantity invariant: 8/8 consistent, zero discriminating power.
- **Observed output of an unobserved process** — the SignalR `connectionToken`, present on every poll
  URL but mintable only by a handshake that was never captured.
- **Asserted from outside this capture** — the 403-on-library-UA claim; all 429 captured responses
  were 200. (Since confirmed by a live probe, which is how that label is meant to be discharged:
  go and look, then relabel.)
- **Derived and coincidentally equal** — Appendix A's `initialUnitPrice`.

When you add a claim to these docs, say which of those it is. "I can see the value" and "I know how to
obtain the value" are different statements, and conflating them is what produced most of the defects
the audit found.
