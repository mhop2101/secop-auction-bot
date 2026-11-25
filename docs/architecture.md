# Architecture

## What runs, in order

```
  config.load / validate
        │
        ▼
  page_parse   ── saved HTML ──▶ AuctionRef {auction_id, lang, market, …}     no network
        │
        ▼
  auth         ── decode JWT claims, check exp ──▶ TokenInfo                  no network
        │
        ▼
  session      ── one cookie jar, N per-thread HttpClients
        │
        ▼
  detail       ── GetAuctionSimple + GetAuction + GetBidsTotalInfo ──▶ AuctionDetail
        │
        ▼
  payload      ── AuctionDetail + bid_value ──▶ BidPayload ──▶ bytes (built once)
        │
        ▼
  schedule     ── fire_time ──▶ T_wall ──▶ T_mono, waves                      no network
        │
        ├── clock   ── Date-header samples ──▶ ClockEstimate  (diagnostic, never moves T)
        │
        ├── gate    ── one GetAuctionSimple at T − gate_lead_ms ──▶ GateVerdict (cached)
        │
        ▼
  bidder       ── per-wave barriers, D/V/P threads, first-success ──▶ FireOutcome
        │
        ▼
  report       ── summary.json + human summary
```

`logging_setup` and `capture` sit underneath all of it: every request, every decision and every timing
goes through one queue into the run directory.

## Design decisions worth knowing

### The operator owns the fire instant

`fire_time` is required, manual, and never derived from the server. The two server clocks available
disagree by half a second, neither is tied to the auction state machine, and the `Date` header is
whole-second so the obvious estimator is biased rather than noisy. A manual `T` doesn't create
accuracy — it relocates the error onto your NTP-synced host clock, where it is a few milliseconds and
you can actually measure it. Full argument in [timing.md](timing.md).

The corollary is that the app **warns but never overrides**. If your `fire_time` disagrees with the
server's `startDate`, you get a loud log line and the app fires where you told it to.

### Wall clock once, monotonic thereafter

`schedule.arm()` converts `T_wall` to `T_mono` exactly once. Everything downstream schedules on
`time.monotonic_ns()`, so an NTP step or a DST transition mid-countdown cannot move the fire instant.
`arm()` takes injectable clocks, which is why the whole scheduling layer is unit-tested offline and
deterministically.

### Threads, not asyncio

The workload is a tiny synchronised burst of blocking HTTP calls where wall-clock release precision
matters more than throughput. `threading` plus `requests` is the simpler, better-understood tool for
that shape. An `asyncio`+`httpx` variant would be a fine alternative; the design would be identical.

Three roles:

| Role | Behaviour | Cost |
|---|---|---|
| **D** — direct submit | At the wave instant, POST `CreateBid` immediately | one round trip |
| **V** — validate then submit | POST `ValidateBid` at `release − validate_lead_ms`; if valid, POST `CreateBid` | two round trips |
| **P** — validate only | POST `ValidateBid`, record, never submit | one round trip; still writes an audit row |

D is one round trip **only because the state gate runs once ahead of `T` rather than inline**. An
inline gate would give D two round trips and erase the distinction from V entirely.

### The state gate cannot be inline

The gate *is* a `GetAuctionSimple`, and it costs a median 135.3 ms against a recommended 90 ms schedule
span. So it runs once, at `T − gate_lead_ms`, chosen so its *response* lands before release, and the
verdict is cached for the whole schedule. `gate_mode=off` skips it and relies on server-side rejection
— which is what pre-`T` waves already do anyway.

### One payload, built once, shared by every thread

`BidPayload.to_bytes()` is called once and every thread sends the identical bytes. This is what makes
`AuctionbyItem` / `AuctionbyLot` (which bid per item or per lot) genuinely out of scope rather than
merely unimplemented: they would need a payload per item, breaking the shared-bytes model, the
submission accounting, and the single first-success model at once. `payload.build_payload()` hard-aborts
on any `auctionVersionCode` other than `AuctionbyProposal`.

### One cookie jar, one session per thread

`requests.Session` is **not thread-safe**, so each thread gets its own `HttpClient` and therefore its
own `Session`. They share one cookie jar, because the Azure Application Gateway sets affinity cookies
and the whole burst needs to land on the same backend to see consistent state.

Pre-warm fires **N concurrent** GETs, not a sequential loop. In `urllib3` there is no thread-affine
connection — a shared pool hands out whatever is free — so a sequential warm-up leaves exactly one
pooled connection and N−1 threads pay a fresh TCP+TLS handshake at the fire instant. The capture prices
that at 167–184 ms, roughly twice the entire schedule span, paid by exactly the threads meant to win a
tiebreak.

Pre-warm runs at arming, and a connection opened then does not outlive a long countdown: on
2026-11-20 the ones warmed minutes ahead were gone by `T`, so every bid reconnected inside the opening
rush. `session.keep_warm` therefore repeats the same concurrent round at `keepalive_offsets_ms` before
the first release. How long the gateway keeps an idle connection is unmeasured
([Q17](open-questions.md#q17--how-long-does-the-gateway-keep-an-idle-connection)).

Every call carries `(connect_timeout, read_timeout)`. **Nothing is ever retried**: a retry at the fire
instant lands at an unknown time and writes another buyer-visible audit row.

### One connection per shot, and nothing queued behind it

The run builds one `HttpClient` per *planned thread across the whole schedule*, not per wave, and hands
them out one per attempt. Because no two threads share a client, a thread waiting on its own response
cannot delay another thread's release: the spacing between shots is a property of the release instants
alone, and shots can be scheduled as close together as desired without one blocking the next. This is
the difference between a schedule that describes when requests *leave* and one that merely describes
when they were *queued*.

The same fact read the other way is the constraint: anything a thread sends for itself is **serial**
with its bid over that one connection — for as long as the earlier request takes, which under a
stalling server is its whole timeout. Three things have to respect it.

- **`SetStartBiddingAudit` is not on any bid's connection.** `bidder.StartAudit` sends it once per run,
  `start_audit_lead_ms` (default 60 s) before the first release, from its own thread and its own
  `HttpClient`. It was inline first (a round trip in front of every bid), then a lead ahead on the same
  connection (fine until 2026-11-20, when hung audits held 5–16 bids per run more than half a second
  late). A lead is not isolation; a separate connection is.
- **A keep-alive is on a bid's connection, and bounded.** It carries `keepalive_timeout_ms` as both
  timeouts, and `config.validate` refuses a config whose last keep-alive is less than
  `keepalive_quiet_ms` — two timeouts, a 2 s margin and the gate's lead — before the first release.
- V threads carry `validate_lead_ms` for the same reason: their `ValidateBid` occupies the connection
  their `CreateBid` needs.

### The order of a run

```
arm ── prewarm (all bid clients, at once)
    ── require_live_confirmation            refuse here, before anything is written
    ── StartAudit.start()                   own thread, own client: sleep → POST → read the row back
    ── keep_warm                            every bid client, at each keepalive offset
    ── _place_t_on_secop_clock              only with apply_clock_offset; declines if not safe
    ── run_gate                             one read at T − gate_lead_ms
    ── run_schedule                         threads sleep to their release and send bids, nothing else
    ── records.read_result                  secop_record_delay_s after the last release
    ── summary
```

`T` may be moved only between `keep_warm` and the gate, while every thread is still asleep, and no later
than `clock_window_ms + gate_lead_ms + 1000` ms before the first release.

### The run reads the server's record, not just the socket

A bid that timed out was not rejected. `records.read_result` reads `GetBidsTotalInfo`,
`GetSupplierBids` and one page of `GetEventLogs` after the schedule and puts the accepted bid, its
company and server timestamp, and the count and times of this company's processed bids, in
`summary.secop_record`. `report` prints it beside the outcome and never folds it in.

There is a floor under all of this that no configuration reaches past. A warm round trip to
`subastas.secop.gov.co` is roughly 70 ms with a few ms of run-to-run jitter, so offsets spaced closer
together than that jitter do not arrive in the order they were sent. Redundancy at one offset is
meaningful; a ladder of 1 ms steps is not.

### First success spans the whole schedule

The instant any `CreateBid` satisfies `is_success()` — `isValid is True` **and** an empty
`errorMessage`, one definition used at every site — the winner is recorded with its wave index,
scheduled offset, actual offset, role and `bidId`. In-flight requests are allowed to complete and are
logged; they just don't change the result.

`stop_on_success` then cancels unreleased waves. At the recommended schedule densities this drops zero
waves, because the fastest bid response observed anywhere is 168.5 ms against a 90 ms span. The code
logs that rather than implying a saving. See [safety.md](safety.md#stop_on_success-will-not-save-you).

### Uncertainty is represented, not smoothed over

Several places in this codebase deliberately refuse to guess:

- `parse_user_decimal` rejects `"1.234"` instead of picking a reading.
- `page_parse` raises `PARSE_AMBIGUOUS` on a multi-auction page instead of taking the first row.
- `clock.marzullo` reports a residual width instead of a bare midpoint.
- `payload` warns and logs the competing formula whenever `quantity != 1`.
- `endpoints.OBSERVED_ON_WIRE` marks which routes have actually been seen.

This is the main lesson from `PLAN_AUDIT.md`. The defects it found were nearly all places where a
plausible inference had been written down in the voice of an observation.

## Data flow of a number

Worth tracing once, because it is the highest-stakes path in the app.

```
operator: "7.813.974,32"
    │  numbers.parse_user_decimal   (comma present ⇒ Colombian; strip dots, comma → dot)
    ▼
Decimal("7813974.32")
    │  numbers.total_value(quantity, bid)     scale ADDS, no normalize
    ▼
Decimal("7813974.32000000")  ── str() ──▶  "7813974.32000000"   ← the wire string
```

and alongside it

```
initial_value × quantity ÷ bid_value  ──quantize 12 dp──▶  "50.000000000000"
    │
    └── invariant: iqup × bid_value == initial_value × quantity   (exact, in Decimal)
```

Three things break this if you touch them: using `float` anywhere, calling `.normalize()` on a wire
value, and quantizing `itemQuantityUnitPrice` to anything other than 12 decimal places.

Server messages come back the other way — `"7.813.974,32 COP"` — and go through
`numbers.parse_server_money()`. The two formats are never mixed; that's what
`numbers.round_trip_selftest()` guards.

## Where the risk actually is

| Area | Risk | Mitigation |
|---|---|---|
| `bid/CreateBid` response | route confirmed live; a *successful* reply has never been seen | `is_success` is conservative; every response logged verbatim |
| `GetAuction` item mapping | verified against one real body, one item, quantity 1 | real fixture committed; multi-item and multi-lot shapes still unseen |
| quantity factor | non-discriminating in all evidence | warning + logged alternative + a test that documents the divergence |
| pre-open behaviour | zero supporting bytes | `pre_ms` defaults to 0; `STATE_NOT_STARTED` documented as never observed |
| concurrency tolerance | zero supporting bytes | ship `single` first; `burst`/`custom` contingent on a throwaway-auction probe |
| host clock | the residual error after the design's trade | NTP checklist; the delta against the observed open is logged for calibration |

## Module dependency order

Build and read in this order — it is also the order the tests get progressively less able to help you:

```
errors ─┬─▶ models ─┬─▶ endpoints
        │           ├─▶ numbers ──▶ payload
        │           ├─▶ config
        │           ├─▶ schedule
        │           └─▶ clock
        ├─▶ page_parse
        ├─▶ auth
        └─▶ logging_setup ──▶ capture

  session ──▶ detail ──▶ gate ──▶ bidder ──▶ report ──▶ __main__
```

Everything left of `session` is pure and fully unit-testable offline. Everything from `session`
rightwards touches the network and is only really verified by a live run — which is why the
[open questions](open-questions.md) all live on that side of the line.
