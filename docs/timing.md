# Timing: the fire instant, the clock, and the schedule

The single most important design decision in this app is that **you specify the fire instant and the
app never second-guesses you.** This page explains why, and what the app does with the clock instead.

## The operator owns `T`; the server owns the window

`fire_time` is required, absolute, and must carry an explicit UTC offset or `Z`. The app converts it to
the monotonic clock once, at arm time, and everything after that is monotonic arithmetic.

It does **not** derive the fire instant from any server clock on its own. Server-time probes still run,
but they are diagnostics: measured, logged, compared, and never used to move `T` unless you explicitly
set `apply_clock_offset`, which defaults to `false`. What that switch does, and what it rests on, is
[SECOP's two clocks](#secops-two-clocks) below.

## Why not derive `T` from the server

Deriving `T` from the server was the original design. It does not survive the evidence.

**The HTTP `Date` header is whole-second.** All 429 captured responses carry `Date`; none has a
fractional part. `Date − local_now` is therefore a *biased* estimator with an expected error of about
−0.5 s, not a noisy centred one — and **a median converges to a bias instead of removing it.** The
`RTT/2` correction everyone reaches for fixes about 68 ms of a ~500 ms error.

Measured over the 128 captured `GetAuctionSimple` exchanges:

| method | result |
|---|---|
| naive `median(Date − t_recv)` | **−0.458 s** |
| Marzullo intersection of the same data | `[−0.023, +0.441] s`, midpoint **+0.209 s** |
| disagreement | **667 ms** |

Applying the naive value would fire roughly half a second late. A `T−60 / −30 / 0 / +30 ms` schedule
would land at about `T+390 … T+480 ms`, and **no wave would land before `T`** — which nullifies the
entire reason for firing early in the first place.

Even under ideal uniform-phase sampling, the residual width of a whole-second estimator is 687 ms at
3 samples, 315 ms at 10, and 184 ms at 30 — all wider than a 90 ms schedule span. "Measure it a few
more times" cannot reach the precision the schedule needs at any practical sample count.

**And there are two server clocks that disagree.** The `Date` clock's feasible interval and the SignalR
hub clock (median −0.547 s, 100 ns resolution) do not intersect. Neither is tied by any evidence to
the clock that decides when `auctionStateCode` flips.

A manually specified `T` doesn't magically create accuracy. **It relocates the error to your host
clock, where you can control and measure it.** That is the whole argument.

### So sync your host

```bash
sudo sntp -sS time.apple.com      # macOS
chronyc tracking                   # Linux
```

A normally-synced machine sits within single-digit milliseconds of UTC — an order of magnitude better
than anything derivable from this API. Do this before every run. `report.py` records the host's NTP
status in the summary when it can obtain it.

## What the clock probes are for

`clock.py` runs anyway, because the probes are how you find out *afterwards* why a bid landed where it
did.

Each `Date` sample gives an honest bound rather than a point estimate. If the response carries
`Date: …:07:40 GMT`, the true server instant is somewhere in `[07:40, 07:41)`, and it happened between
your send and receive instants. That yields

```
offset ∈ [Date − t_recv,  Date + 1s − t_send]
```

`clock.marzullo()` intersects those intervals across samples and reports the **midpoint and the
residual width**. The width is the honest error bar, and it is the number that matters — a midpoint
without it is exactly the false precision this design exists to avoid.

`clock.clock_guard()` compares that width against the schedule span and tells you when the schedule is
meaningless. There is deliberately **no** function anywhere in the module that returns a bare median as
"the offset"; `tests/test_clock.py` asserts as much.

The SignalR hub clock is the only sub-second server time available, and it is **not implemented** —
`clock.probe_signalr()` raises `NotImplementedError`. The handshake was never captured and
`connectionToken` cannot be derived from anything available. See
[open-questions.md](open-questions.md#q13--can-the-signalr-handshake-be-reconstructed).

## Arming: wall clock once, monotonic thereafter

```
T_wall = parse(fire_time)
T_mono = time.monotonic_ns() + (T_wall − now_wall)      # converted ONCE, at arm time
```

After that conversion the run schedules exclusively on `time.monotonic_ns()`, which makes it immune to
NTP steps and DST transitions during the countdown. `schedule.arm()` takes injectable `now_wall` and
`now_mono_ns` so all of this is unit-tested offline and deterministically.

Two gates at arm time:

- `T_wall` in the past → `FIRE_TIME_PAST`.
- `T_wall` further ahead than `max_arm_ahead_s` (default 3600) → `FIRE_TIME_TOO_FAR`. You almost
  certainly typed the wrong day, and a run armed for hours will outlive the 24-hour JWT anyway.

And one cross-check that is deliberately **not** a gate: if `T_wall` differs from the server's
`startDate` by more than `fire_time_mismatch_warn_s`, the app warns loudly and logs both — then honours
your `fire_time`. The operator is the authority. The entire point of this design is that a wrong
`startDate`, or a wrong clock offset, cannot silently move your bid.

## Waves

A run's schedule is an ordered list of waves. Each wave is
`{offset_ms, direct, validate, validate_only}`, with `offset_ms` measured against `T` and negative
meaning before `T`.

| `mode` | schedule | use it for |
|---|---|---|
| `single` (default) | one wave at `offset_ms = 0`, composed from `n_direct` / `n_validate_submit` / `n_validate_only` | a precise open snipe |
| `burst` | `bid_count` waves, `offset_ms(i) = −pre_ms + i·interval_ms` | "fire N bids starting X ms before the open" |
| `custom` | `schedule_offsets_ms` verbatim | an arbitrary spread across the boundary |

`burst` with `bid_count=4, pre_ms=60, interval_ms=30` gives exactly `[−60, −30, 0, +30]`.

Each wave releases at `T_mono + offset_ms − lead_ms`. Threads sleep coarsely to about 2 ms out, then
busy-wait to the instant.

> **Naming.** `offset_ms` is a schedule position relative to `T`. `clock_offset` is the measured
> server-versus-local difference. They are unrelated quantities and an earlier draft of the spec called
> both "offset" one clause apart in the same formula. The code never uses the bare word.

### Why fire before `T`

Your host clock and the server's may differ, and the server may open a hair early. A pre-`T` wave is
insurance: if the auction isn't open yet the server should reject it cleanly, and if the server opened
earlier than you estimated, that early wave is the one that lands first.

**This rests on an assumption nobody has tested.** No pre-open request exists anywhere in the capture,
so `STATE_NOT_STARTED` names a response that has never been observed. See
[Q4](open-questions.md#q4--does-a-pre-open-createbid-get-a-clean-rejection-or-trip-something). If a
pre-open attempt trips an anti-abuse counter instead of bouncing harmlessly, `pre_ms` must go to zero.

## SECOP's two clocks

The first two live runs fired on an NTP-synced host at the instant the auction was due to open, and
were a second early by the only clock that counted.

SECOP shows two clocks to a client:

| Clock | Where you see it | On 2026-11-20 |
|---|---|---|
| The web front end's | the HTTP `Date` header on every response | tracked real time |
| The application's | `registerDate` on every event-log row; `date` on every bid | **0.9–1.0 s behind** real time, in two auctions an hour apart |

The accepted bid in each auction is stamped **32 ms** and **44 ms** after `startDate` — on the
application's clock. That is the clock the opening is observed on, and `preflight`'s `Date` probe does
not see it at all. The auction page does: its countdown follows the application's clock.

**How it is measured.** The run's start audit writes an event-log row, and the application stamps that
row to a tenth of a microsecond. The row was written between the instant the request left and the
instant its answer arrived, so

```
stamp − t_recv  ≤  (SECOP clock − host clock)  ≤  stamp − t_send
```

an interval exactly as wide as that one round trip — tens of milliseconds from a host near SECOP when
the server is answering promptly. `records.clock_from_start_audit` reads the log back, finds the row,
and reports the interval, its midpoint and its width. `clock-probe` does the same on demand.

**Whose row is it?** The log names a company, not a session. If several of your company's start-bidding
rows fall within `clock_window_ms` of the audit — two accounts starting together — nothing says which
is this run's, and the interval becomes the *hull* of all of them: wider, still true. With one audit
per thread, as the app sent them until 2026-11-20, fifteen rows fell inside one request's window and
the hull was three seconds wide. One audit per run, and leads staggered between runs of one company,
is what makes the row identifiable.

**What `apply_clock_offset` does with it.** With the switch on, and only then, the run moves `T` by
minus the midpoint — SECOP 945 ms behind means every release happens 945 ms later — at a point where
every thread is still asleep. It declines, and says so, when:

- the row had not been read back by the last moment `T` could safely move
  (`clock_window_ms + gate_lead_ms + 1000` ms before the first release),
- no candidate row was found, or the audit itself was not answered with a 200,
- the interval is wider than `clock_max_width_ms`, or the offset is beyond `clock_window_ms`.

A declined move leaves `T` exactly where `fire_time` put it — unless `clock_fallback_shift_ms` is set,
in which case `T` is moved by that `clock-probe` reading instead. Leaving it unset means a failed
measurement fires on real time, about a second early by SECOP's clock as it ran on 2026-11-20, so set
it: run `clock-probe` a few minutes before and paste the value it prints. After a move, every offset the run reports
is measured from the moved `T`, and `summary.clock.shift_ms` records the distance.

**What this does not establish.** Two auctions on one day. Whether the offset is the same tomorrow is
unknown ([Q11](open-questions.md#q11--which-clock-does-the-auction-state-machine-use)); the 2026-11-11 runs hint at a larger one. Whether a bid that
arrives before the application's `T` is refused or held is unknown
([Q4](open-questions.md#q4--does-a-pre-open-createbid-get-a-clean-rejection-or-trip-something)). And
arriving on time was **not sufficient** on 2026-11-20: one bid left within roughly 50 ms of the
application's `T` and was still not processed for at least 3.9 s, because the server was saturated.
Timing is necessary; it is not the whole of it.

## `lead_ms`

Defaults to **0**, and is never auto-derived.

With a manual `T` on an NTP-synced host there is nothing to lead against except transit. Set it
deliberately from a low quantile of `timings.wait` across at least 10 preflight samples —
`clock.recommended_lead_ms()` computes an advisory value, and the app logs it without applying it.

Two ways to get this wrong:

- **One sample.** The observed distribution spans 90.3 → 1138.3 ms. A single draw varies by roughly
  half a second between runs minutes apart.
- **Using `ValidateBid` RTT.** Its time is almost entirely server-side work (1627 of 1633 ms on the
  slowest sample), not transit. Leading by it would make your request arrive *before* the open and be
  rejected.

Total elapsed time also includes client-side queueing, which is not propagation either.

## What an offset actually positions, and what may travel with a bid

An offset positions the moment a request **leaves this host**, not the moment it arrives. Arrival is
the offset plus one-way transit, and the verdict comes back one more transit after that. Against
`subastas.secop.gov.co` a warm round trip measures ~70 ms, so a shot fired at `T − 30 ms` arrives
around `T + 5 ms` and is answered around `T + 40 ms`. Lead against the *one-way* leg; the return leg
only decides when you learn the outcome.

That only holds if the request really does leave at the offset. Each thread has exactly one pooled
connection, so anything it sends first is serial with its bid, and the bid's *actual* departure is the
offset plus however long that earlier request takes — including, when the server stalls, its entire
timeout.

`SetStartBiddingAudit` showed both halves of that. Issued inline right before the submission it added
a full round trip — 168.5 ms fastest, 614.4 ms median — to every offset. Moved a fixed lead ahead of
each thread's release, it was harmless for as long as the server answered; on 2026-11-20 it did not,
and audits that hung for 3 to 10 s held their bids for exactly that long:

| Auction | Host | Bids that left more than 0.5 s late |
|---|---|---|
| 1 | server | 6 of 21 |
| 1 | laptop | 16 of 21 |
| 2 | server | 5 of 14 |
| 2 | laptop | 10 of 14 |

So the audit is no longer on any bid's connection. It is **one request per run**, sent
`start_audit_lead_ms` (default 60 s) before the first release on a client of its own.

The one thing that still shares a bid's connection is the **keep-alive**, on purpose: a connection
opened at arming is dead by `T`, and something has to re-open it before the opening rush. Each
keep-alive carries `keepalive_timeout_ms` as both its connect and its read timeout, and the config is
refused unless the last one is at least `2 × keepalive_timeout_ms + 2000` ms (plus the gate's lead)
before the first release. It cannot be in flight when the bid needs the connection.

Two ways to still get this wrong:

- **Adding a request to a fire thread.** It goes on another client. "Earlier on the same connection"
  is the design that failed.
- **Reading offsets as arrival times.** They are send times. A "fire at `T`" schedule arrives late by
  one one-way transit, every time.

Below a few milliseconds, spacing stops meaning anything: run-to-run jitter on this path is of that
order, so shots spaced 1 ms apart do not arrive in the order they were sent. Fire redundant shots at
one offset instead of building a ladder finer than the network resolves.

## The state gate runs once, ahead of `T`

The gate *is* a `GetAuctionSimple`, and it costs a median of 135.3 ms (min 90.3, max 1138.3) against a
recommended schedule span of 90 ms — between 1.5× and 12× the entire schedule.

An inline gate would give direct-submit threads **two** round trips, which is precisely what
distinguishes a validate-then-submit thread from a direct one. The role model would collapse. Worse,
in `single` mode a gate can only read `Started` if issued *after* `T`, so the bid would necessarily
land at `T` plus one gate RTT.

So:

| `gate_mode` | behaviour |
|---|---|
| `once_before` (default) | one `GetAuctionSimple` at `T − gate_lead_ms` (default 1500 ms, comfortably past p95) so its *response* lands before release. The verdict is cached for the whole schedule. `Finished` / `Canceled` / `Deprecated` aborts the run. `ReadyToStart` proceeds — that is the expected pre-open reading, not a failure. |
| `off` | fire and let the server reject. Legitimate: the server is the authority anyway, and pre-`T` waves already rely on exactly this. |

Never per wave, and never again after the schedule starts — the gate's whole job is to be one cheap
read whose answer is already in hand when the first wave releases.

Note that `auctionExecutionStateCode` was `Running` on 128 of 128 responses, including all 125 *after*
the auction flipped to `Finished`. A condition of `!= "Paused"` is permissive past the end of the
auction and contributes almost nothing.

## What the run records

Enough to reconstruct the timing to the millisecond afterwards: the supplied `fire_time`, its parsed
`T_wall`, the `T_mono` it converted to and the instant of that conversion, the server's `startDate` and
the delta against it, `lead_ms` / `validate_lead_ms` / `gate_lead_ms`, every wave's computed and actual
release time, and per-thread send and receive deltas from `T`.

Clock probes are logged with whether they moved `T`: every `Date` sample with its interval, the
intersected offset with its residual width, the event-log estimate with its candidates and width
(`secop_clock`), and `clock_applied` or `clock_not_applied` with the reason.

After a real open, the numbers worth writing down are in `summary.secop_record`: the accepted bid's
company and server timestamp, and when the server processed each of yours, all measured from
`startDate` on SECOP's own clock. Those calibrate the next run; the state flip does not — the
"Subasta en curso" row was written 351 ms *before* `startDate` in one auction and 159 ms *after* it,
later than the accepted bid, in the other.
