# Open questions

Things this app's design depends on that nobody has fully observed. Each entry says what
is unknown, what the code currently assumes, what would settle it, and what changes if the answer is
the unfavourable one.

Keep these in the run logs. The first live auction is the only instrument that can answer most of them,
and a run that answers one is worth more than a run that places a bid without answering any.

Two live auctions on 2026-11-20 moved several of these; each entry below says what they showed. The
runs and SECOP's own records are archived under `history/`, and `history/2026-11-20-audit.md` is the
write-up.

---

## Q1 — Does the server break ties by submission time?

**Assumed:** yes — it is the premise of racing D threads at all.

**Settles it:** two `CreateBid`s from different threads, milliseconds apart, on a throwaway auction
where `myLastBid > 0`. Compare which `bidId` wins against which thread sent first.

**If no:** the whole D-thread racing model buys nothing beyond resilience against a single dropped
request. `n_direct` should drop to 1 or 2, and the audit-log footprint of a burst stops being worth
paying. This is the single highest-value question in the list.

**2026-11-20:** consistent with yes, not proof. In both auctions exactly one bid was accepted — a zero,
stamped 32 and 44 ms after `startDate` — and every later zero was answered `BidErrMin`. What is *not*
known is the order the server works in when requests arrive together: our bids were processed 4 to 35 s
after the opening, some of them having left before the accepted one was stamped. Arrival order and
processing order were not the same thing that day.

## Q2 — Does the server tolerate concurrent identical bids?

**Assumed:** later ones bounce harmlessly as duplicates.

**Settles it:** `mode=custom, schedule_offsets_ms=[0,0], wave_direct=1` on a throwaway auction.

**If it hard-rejects the burst** — or trips an anti-abuse counter — then concurrency is actively
harmful and the app should ship `single` with `n_direct=1`. Run this **before** any burst on a real
auction.

**2026-11-20:** each later identical bid that reached the server was processed and logged as an
ordinary `BidErrMin`, 13 and 14 from one host. Nothing looked like a block. But only eight or nine
connections per host got through in the seconds before each opening, however many were opened —
a per-address limit at the gateway, or plain overload; the data cannot tell which.

## Q3 — Is `ValidateBid` accepted before the auction is `Started`?

**Assumed:** unknown; `validate_lead_ms` defaults to 0 as a result.

**Settles it:** one `ValidateBid` issued while the state still reads `ReadyToStart`.

**If yes:** set `validate_lead_ms ≈ RTT` so a V thread's `CreateBid` lands near its wave's instant
rather than one round trip behind. **If no:** leave it at 0 and accept that V trails D by an RTT.

## Q4 — Does a pre-open `CreateBid` get a clean rejection, or trip something?

**Assumed:** a clean `STATE_NOT_STARTED`, treated as expected rather than a failure.

**Nothing in the capture bears on this.** It begins 7 m 40 s after the open and never returns
`ReadyToStart`. `STATE_NOT_STARTED` names a response nobody has seen.

**Settles it:** one `CreateBid` deliberately fired before `startDate` on a throwaway auction.

**If it trips an anti-abuse counter**, the entire "fire before T as insurance" rationale collapses and
`pre_ms` must go to 0. Everything about negative wave offsets rests on this being benign.

**2026-11-20:** still open, and now sharper. In the second auction every on-time bid left 0.75 to
1.95 s *before* SECOP's clock reached `startDate`. None got an answer; the server logged each as a
`BidErrMin` 11 s or more *after* the opening. So they were neither refused at once nor accepted — they
waited. Whether a pre-open bid is held and then judged, or was merely stuck behind a saturated server,
is not distinguishable from that run. A `STATE_NOT_STARTED` answer has still never been seen.

## Q5 — What does a successful `CreateBid` return?

**Assumed:** the same 22-key shape as `ValidateBid`, with `isValid: true`, an empty `errorMessage`, and
a non-zero `bidId`.

**2026-11-20:** still open. Some 55 `CreateBid` requests left this app across two auctions and not one
was answered inside its timeout, so the response shape is as unobserved as before. What *is* now known
is the server side of it: the bid list (`GetSupplierBids`) shows an accepted bid as `isValid: true`,
`errorMessage: ""`, a non-zero `bidId` and a `date`, which is at least the same vocabulary.

`CreateBid` was never called in the capture. `bidder.is_success()` is written defensively around this,
and every response is logged verbatim so the first real one is fully recoverable even if the shape
differs.

**Settles it:** the first accepted bid. Capture the body into `tests/fixtures/` immediately.

## Q6 — Is `bid/CreateBid` even the right path and casing? — ANSWERED 2026-07-29

**The route exists, and its casing does not matter.** Established without submitting anything, by
sending a `GET` to the POST-only route and reading the status:

```
GET …/v1//bid/ValidateBid    -> 405    control: known-good route from the capture
GET …/v1//bid/CreateBid      -> 405    the route resolves
GET …/v1//bid/createbid      -> 405    routing is case-insensitive
GET …/v1//bid/CreateBidZZZ   -> 404    control: a bogus sibling really does 404
GET …/v1//bid/GetMinBid      -> 405    the route resolves
```

405 means the path resolved and only the method was wrong, and the 404 on a deliberately bogus
sibling proves the two outcomes are distinguishable. A wrong-path 404 at the fire instant is no
longer a risk. The probe costs one GET and writes no audit row, so it is worth repeating whenever
the route set is in doubt.

**Still open:** what a *successful* `CreateBid` returns — that is Q5, and it needs a real POST.

## Q7 — What does `GetAuction` actually return? — ANSWERED 2026-07-29

**Captured.** The real body is committed as `tests/fixtures/getauction.json` (practice auction,
`bidders` array emptied first). It corrected the plan on two fields:

| Wire field | §6.2 said | The response actually has |
|---|---|---|
| `itemId` | `itemName` | **`id`** — there is no `itemName` field anywhere in the body |
| `lotInternalId` | item/entry `lotInternalId` ‖ `biddingListId` | the item's **`biddingListId`**; the *entry's* `lotInternalId` reads **`ALLLOTS`**, a marker, not a lot id |

Both mistakes were fatal in opposite ways: the first made `parse_items` raise `PARSE_FAILED` on
every real response, and the second would have silently shipped `lotInternalId: "ALLLOTS"` on the
wire. `itemQuantityStr`, `itemUnit`, `itemInternalReference` and `itemInitialUnitPriceStr` were all
correct as documented.

The likely reason for the `itemName` error: `auctionService.js` renames `id` while flattening
`biddingList` into `self.Items`, so the plan's client-code reading described the SPA's *model*
rather than the wire.

**Verified end to end:** the `ItemMeta` parsed from this live body, fed through `payload.build_payload`,
reproduces all 8 captured `ValidateBid` bodies byte for byte. `GetAuction → ItemMeta → wire` is now
evidence rather than inference.

## Q8 — Is the quantity factor in `itemQuantityUnitPrice` real?

**Assumed:** `initialValue × quantity ÷ bidValue`.

Every captured item has `quantity == 1`, so the factor is non-discriminating: three other formulas
reproduce all 8 wire values byte for byte. What is actually proven is only
`itemQuantityUnitPrice × bidValue = initialValue`.

**Settles it:** any auction with an item at `quantity ≠ 1`. `payload.build_payload()` already logs the
competing formula's value beside its own, so a single such run answers it from the logs.

**Until then**, treat any `quantity ≠ 1` auction as unsupported in practice, even though the code will
build the payload and warn.

## Q9 — What rounding mode does the server use for inexact divisions?

**Assumed:** `ROUND_HALF_UP`, and it is a parameter rather than a constant.

`itemQuantityUnitPrice` terminates within 12 dp only when `bidValue` divides `initialValue × quantity`.
Integrality is irrelevant — **divisibility** is what matters. For `initialValue = 390698716`, six of
the first twelve integers (3, 6, 7, 9, 11, 12) do not terminate.

`numbers.item_quantity_unit_price()` returns an `is_exact` flag and the residual;
`payload.build_payload()` warns when a division was inexact.

**Settles it:** submit an inexact value and compare the server's echoed item against what you sent.
**Until then**, prefer bid values that divide exactly — `numbers.divides_exactly()` tells you which do.

## Q10 — Where is the rate limit, and does the API host sniff User-Agent?

**Half answered on 2026-07-29: the User-Agent check is real, and now directly observed.** Same URL, same moment, no credentials
involved, only the UA differs:

```
curl default UA  -> HTTP 403
Chrome UA        -> HTTP 302   (the ordinary unauthenticated redirect)
```

So the 403 is not a rumour inherited from the static host — sending a realistic `User-Agent` is
load-bearing, which is why `endpoints.py` pins the whole client-hint and fetch-metadata block rather
than a plausible-looking subset.

**Still open: the rate limit.** No 429 has ever been observed and nobody has escalated deliberately.

**Assumed:** a burst of 2–5 is tolerated.
The WAF claim is asserted from outside this capture entirely.

**Settles it:** deliberate escalation on a throwaway auction. **Until then**, keep `D+V+P` in the 2–5
range and treat any 403 or 429 as a hard stop rather than something to retry through.

## Q11 — Which clock does the auction state machine use?

**2026-11-20: largely answered.** Bids and event-log rows are stamped by an application clock that is
not the `Date` clock; it read 0.9–1.0 s behind NTP in two auctions an hour apart, and the accepted bids
are stamped 32 and 44 ms after `startDate` on it. The state-flip row ("Subasta en curso") is written
by something else — 351 ms before `startDate` in one auction, 159 ms after it and *after* the accepted
bid in the other — so it is not the gate. The app now measures the application clock from its own
start audit ([timing.md](timing.md#secops-two-clocks)) and moves `T` onto it only on request.

**Still open:** whether that offset is stable from day to day (the 2026-11-11 runs suggest it was
larger), and whether it drifts within the hour.

**Before that — assumed:** unknown, and therefore not used at all.

The `Date` clock and the SignalR hub clock have **non-intersecting** feasible offsets, roughly 0.5 s
apart. Neither is tied by any evidence to whatever decides when `auctionStateCode` flips.

This is why `fire_time` is operator-supplied and `apply_clock_offset` defaults to `false`.

**Settles it:** observe one real open. Record your `T`, the local instant the state actually flips to
`Started`, and both clock diagnostics. That delta is the only thing that can calibrate future runs.
`report.py` records all of it on every run for exactly this purpose.

## Q12 — How accurate is your host clock at `T`?

With a manual `fire_time`, the residual error is host-versus-server. That is the trade the design
makes deliberately: it does not remove error, it relocates it somewhere you can measure and fix.

**Do this before every run:** `sntp -sS time.apple.com` on macOS, `chronyc tracking` on Linux. A
normally-synced machine is within single-digit milliseconds of UTC, which is an order of magnitude
better than anything derivable from this API.

## Q13 — Can the SignalR handshake be reconstructed?

**Blocked.** `negotiate`, `start` and `abort` have zero hits across all 429 URLs, and
`connectionToken` is already minted on the first captured request. It would have to be reversed out of
`jquery.signalR` 2.x.

Required before the sub-second hub clock probe is buildable at all. `clock.probe_signalr()` raises
`NotImplementedError` rather than pretending.

Worth noting the prize is modest: a second clock that disagrees with the first by 0.5 s does not tell
you which one the auction uses. Q11 is the question; this is only one possible instrument.

## Q14 — Should `bid_is_total` exist?

**Currently rejected.** The field is kept for schema fidelity but `config.validate()` errors if you set
it, because its semantics were never settled: it would make `bidValue` differ per item, it contradicts
the rule that the app never modifies your value, and it inherits Q9's rounding problem. It has never
been exercised — every captured item has `quantity == 1`, where it is a no-op.

**Resolve by deleting it**, or by defining `bidValue_i = bid_value ÷ quantity_i` with an explicit scale
and rounding mode and amending the "never modifies the value" rule to match.

---

## Q15 — Does a bid need a `SetStartBiddingAudit` before it, and how close?

**Assumed:** no. The audit is a log write, not a precondition.

This is load-bearing twice over. The audit left the release instant, then left the bid's connection
altogether: it is now **one request per run**, sent `start_audit_lead_ms` (default 60 s) before the
first release. The browser's pairing — one `Logs_Search_StartBidding` beside each
`Logs_Search_NewBid`, 8/8 in the capture — is no longer reproduced; a run of five bids writes one
start row and five bid rows.

**2026-11-20:** bids were processed whose own thread's audit had failed — 13 bids against 8 answered
audits on one host in the first auction — so a per-bid pairing is not required for a bid to be
*processed*. Every account had at least one start row somewhere before its bids, though, so "no audit
at all" is untested, and none of those bids was accepted, so nothing is known about what an *accepted*
bid needs.

**Settles it:** a `CreateBid` that would otherwise be accepted, on a throwaway auction, once with the
single early audit and once with `do_start_audit=false`.

**If it does matter:** the plausible mechanisms are a server-side session the audit opens and that
expires, or a bid refused outright without a preceding StartBidding. The first would show as a
rejection that a shorter `start_audit_lead_ms` cures; the fix is to bound the lead, never to put the
audit back on a bid's connection.

---

## Q16 — How does the server treat a zero bid? — ANSWERED 2026-11-20 (it accepts one)

**It accepts it.** In each of two live auctions the one accepted bid was a zero, from another company
(`totalValue: 0.0`, `isValid: true`). Every later zero, ours included, was answered `BidErrMin`: a zero
cannot beat a standing zero by the minimum difference. On these auctions the first zero in wins and
ends the bidding.

`bid_value` of zero is allowed and warned about, and goes out as typed: `"0"` stays `"0"`, `"0.0"`
stays `"0.0"`, with `itemQuantityUnitPrice` as `"0.000000000000"`. The server processed our zeros sent
as `"0"` as ordinary bids, so the wire form is at least parseable; whether `"0"` or `"0.0"` matters for
an *accepted* one is still unobserved, because none of ours was first.

**What it commits to** is a question for whoever signs the offer, not for this app: on a reverse
auction the lowest value wins, and an accepted zero is a bid of zero.

## Q17 — How long does the gateway keep an idle connection?

**Assumed:** less than a countdown, more than 15 s.

Connections opened at arming — up to 17 minutes ahead on 2026-11-20 — were gone by the fire instant, so
each bid's first request had to reconnect inside the opening rush, where a third to half of connection
attempts failed. `keep_warm` now re-reads every bid connection at 45, 30 and 15 s before the first
release. Those figures are a guess: nothing has measured the idle timeout, and nothing has yet shown
that a connection read 15 s earlier is still open at `T`.

**Settles it:** in any run, compare each bid's `rtt_ms` and the `timeline.jsonl` send instants against
a run with `keepalive_offsets_ms=[]`; a bid on a live connection has no connect+TLS in front of it
(167–184 ms in the capture). Or simply hold one connection idle and time how long it takes to be
closed.

**If 15 s is too long:** move the last keep-alive closer and lower `keepalive_timeout_ms` to keep the
config legal — the last one must stay `2 × keepalive_timeout_ms + 2000` ms clear of the release.

## Q18 — How does the server order work that arrives together?

**Assumed:** nothing. Unknown, and probably the question that decides who wins.

On 2026-11-20 the accepted bids were stamped 32 and 44 ms after `startDate`. One of ours left within
roughly 50 ms of the same instant and was not processed for at least 3.9 s; the rest took up to 35 s.
The server was saturated, and something determined which request it served first. Candidates: one
company's requests handled one after another (ours would then have queued behind our own); a
connection or session established earlier being served sooner; plain chance.

**Settles it:** nothing cheap. The useful experiment is the cheap one's opposite — *fewer* requests:
one host, one account, three bids. If the first processed bid then lands in milliseconds rather than
seconds, our own volume was the queue.

## How to actually make progress on these

The build order in `SECOP_FIRSTBID_PLAN.md` §14 ships `single` mode first, then makes `burst` and
`custom` contingent on **Q1 and Q2** — because a `D=0, V=0, P=1` rehearsal is a single sequential
`ValidateBid` and is structurally incapable of answering either.

The recommended sequence on a **throwaway** auction where `myLastBid > 0`:

1. `preflight` — answers **Q7** (capture the `GetAuction` body) and gives you real RTT and clock
   diagnostics.
2. One real `CreateBid`, any value — answers **Q6** and **Q5**.
3. `mode=custom, schedule_offsets_ms=[0,0], wave_direct=1` — answers **Q2**, and **Q1** if the server
   accepts both.
4. One `CreateBid` before `startDate` — answers **Q4**.
5. Only then consider `burst` on anything that matters.

Every one of those runs writes rows into a buyer-visible audit log under your public IP and company
code. "Throwaway auction" is not a figure of speech — see
[evidence.md](evidence.md#the-audit-log--why-dry-run-is-not-free).

## Q19 — Does SECOP drop a request whose client stopped waiting?

**Assumed:** no — on 2026-11-20 SECOP processed bids 4 to 35 s after this app had given up on
them at 10 s.

**2026-11-23 says otherwise, or says something else:** seven bids left one host within 0.7 ms
of their slots, on connections a keep-alive had used 15 s earlier, and each ended in a 10 s
read timeout. SECOP's event log holds no row from that host's address for any of them, while
it logged 47 bids typed by hand under the same company from a browser, the first 22 s after
the opening. Two explanations fit and the data cannot separate them: SECOP abandons a request
once its client closes the connection, or requests from one login are serialised and these
were lost behind the manual ones (the same account was bidding by hand at the same time).

**What changed:** `read_timeout_ms` is 60 s, so this app no longer closes the connection first,
and the record read reports this host's bids apart from everyone else's (`host_bid_rows`).

**Settles it:** one run with the long timeout and an account nobody uses by hand during the
auction. If this host's bids then appear in the log, one of the two was the cause; a second run
with manual bidding on the same account tells which.
