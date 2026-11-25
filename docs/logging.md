# Logging and run artifacts

The goal: after any run — success or failure — you can reconstruct exactly what happened, per thread,
to the millisecond, without ever leaking the JWT.

This is deliberately over-built. The capture that this whole project is derived from is a HAR, and the
single most useful thing a run can produce is another HAR you can open in the same tools.

## Run directory

Every invocation creates `runs/<UTC-timestamp>_<run_id>/`. The `run_id` is short random hex derived
from `os.urandom` — it is an identifier, not a security token, and `logging_setup.new_run_id()` says so.

| File | Contents |
|---|---|
| `config.json` | The fully resolved config, **JWT redacted** to `first6…last6`. On `preflight` and `run` it also carries a `token_claims` block — the decoded non-secret claims (company code and name, user id, `exp`, `nbf`, `iss`, `aud`, `LoginGUID`, and the claim names present). `plan` never inspects a token, so its snapshot has no such block. |
| `events.jsonl` | One JSON object per event. The primary log. |
| `http/NNN_<role>_<thread>_<endpoint>.json` | One file per HTTP exchange, full request and response, redacted. |
| `capture.har` | A HAR 1.2 capture of **every** request and response in the run, so the session can be replayed in the same tools this project started from. |
| `timeline.jsonl` | Monotonic-clock timeline: barrier releases, each send, each receive, first success. |
| `payload.json` | Written by `plan` and `preflight`: the exact compact bytes that would go on the wire, plus the whole build report — warnings, inexact divisions and their residuals, and any non-price-list items that were excluded. |
| `summary.json` | Machine-readable outcome. |
| `run.log` | Human-readable text log, `INFO` and above. |

`runs/` is in `.gitignore`. These files contain full request and response bodies and your bid values.

## Every request is logged. No exceptions.

Not just the bid calls. There is a record — in `events.jsonl`, in an `http/NNN_*.json` file, and in
`capture.har` — for each of:

- the preflight token-validity GET
- `GetAuction`, `GetAuctionSimple`, `GetBidsTotalInfo`
- every clock probe sample
- every pre-warm request
- the single pre-`T` state gate read
- `SetStartBiddingAudit` / `SetSubmitBiddingAudit`
- every `ValidateBid` and every `CreateBid`, per thread, per wave — **including** the ones that bounce
  as `DUPLICATE` or `STATE_NOT_STARTED`
- every failed attempt, including requests that timed out or never got a response, logged with the
  request plus the exception and `status: null`

Nothing is sampled, summarised away, or dropped. If it left the process, it is in the logs.

The `corr_id`, `thread_id`, `role` and wave index are assigned **before sending**, so a request that
never returns is still fully traceable.

## Event schema

```json
{
  "ts_wall": "2026-07-28T21:00:00.123456Z",
  "ts_mono_ns": 123456789,
  "run_id": "…",
  "stage": "preflight|prewarm|schedule|build|fire|report",
  "level": "DEBUG|INFO|WARNING|ERROR",
  "event": "run_started",
  "thread_id": "D-0|V-1|P-0|main",
  "role": "D|V|P|null",
  "corr_id": "uuid per HTTP exchange",
  "detail": { }
}
```

The `event` names actually emitted, grouped by what they tell you:

| Group | Events |
|---|---|
| HTTP | `http_response`, `http_error` (one per exchange, whichever the request produced) |
| Setup | `run_started`, `token`, `auction_detail`, `item_model`, `item_model_unverified`, `bid_baseline`, `bid_baseline_unavailable`, `header_divergence` |
| Build | `payload_built` — carries the warnings, the inexact divisions and their residuals, and any excluded non-price-list items |
| Connections | `prewarm_complete`, `prewarm_error`, `keepalive_complete`, `keepalive_skipped`, `keepalive_error`, `client_reuse` |
| Clock and gate | `clock_offset`, `secop_clock`, `clock_applied`, `clock_not_applied`, `gate_scheduled`, `gate_verdict`, `gate_skipped` |
| Firing | `schedule_planned`, `start_audit`, `start_audit_error`, `fire_start`, `fire_skipped`, `fire_blocked_by_gate`, `bid_result`, `first_success`, `thread_decision`, `release_barrier_broken`, `fire_complete` |
| SECOP's record | `secop_record` — the bid list and the company's event-log rows, read back after the schedule |
| Cancellation | `stop_on_success_reach`, `stop_on_success_triggered` |
| Problems | `thread_error`, `threads_abandoned`, `error` |
| Close-out | `dry_run_footprint`, `summary` |

There is no single `wave_dropped` event. A wave cancelled by `stop_on_success` shows up three ways:
`stop_on_success_triggered` when the latch fires, a `thread_decision` with `decision:
"wave_cancelled"` for each thread that never released, and `fire_complete.dropped_wave_indices` with
the full list. The summary's `waves_dropped` is the count.

Per HTTP exchange both sides are logged: `method`, `url`, `request_headers` (redacted),
`request_body`, `status`, `response_headers` (including the server `Date`), `response_body`,
`t_send_mono_ns`, `t_recv_mono_ns`, `rtt_ms`. For bid calls, `isValid`, `errorType`, `errorMessage`,
`errorMessageLocale`, `minBid`, `currentRankingPosition` and `bidId` are lifted out to the top of
`detail` so they're greppable.

Per scheduled attempt: `mode`, `wave_index`, `scheduled_offset_ms`, `actual_offset_ms` (actual send
time minus `T`), and whether the wave fired before or after `T`. A `schedule_planned` event lists every
wave up front, and `fire_complete` closes the books with the fired and dropped wave indices.

`thread_decision` records **why** each thread did what it did — "V-1 aborted: ValidateBid isValid=false
errorType=BidErrMin minBid=7.813.974,32 COP", "D-2 skipped: first_success already set". When a run
does something surprising, this is the log you read.

## Clock records

Clock probes are logged with whether they moved `T`. The `Date`-header estimate (`clock_offset`) never
does. The event-log estimate (`secop_clock`: source, candidate rows, `lo_ms`/`hi_ms`, midpoint, width,
whether it was usable and why not) moves `T` only under `apply_clock_offset`, and then exactly one of
`clock_applied` (with `shift_ms`) or `clock_not_applied` (with the reason) follows it.

Recorded: the operator-supplied `fire_time`, its parsed `T_wall`, the `T_mono` it converted to and the
instant of conversion, the server's `startDate` and the delta, `lead_ms` / `validate_lead_ms` /
`gate_lead_ms`, each wave's computed and actual release time, and per-thread send and receive deltas
from `T`.

For each `Date` sample: the raw header, the send and receive instants, and the derived interval
`[Date − t_recv, Date + 1s − t_send]`. Then the intersected offset **with its residual width**, and the
disagreement against any other source.

> A bare median of `Date − now` is never logged as "the offset". That number is a bias, not an
> estimate, and recording it as an offset is precisely how the original design went wrong.

## Redaction

One `redact()` is used by every logger path, and it is unit-tested.

- The raw JWT is never logged. It renders as `first6…last6`.
- `Authorization` becomes `Bearer <redacted>`.
- Cookie **names** are kept; values are blanked.
- **URL query parameters** named `token`, `access_token`, `jwt` or `id_token` are rewritten.

That last one is the important one and it is easy to miss. On the SignalR path the bearer token is a
query parameter — `token=<2370 chars>` on 281 URLs — not a header. A `redact()` built as a header-dict
transform lets a live 24-hour credential straight through into `capture.har`, which is the artifact
most likely to be attached to a bug report. `tests/test_redaction.py` covers a realistic SignalR URL
specifically.

Auction values — your bid, your company name — are your own data and are logged in full.

## Concurrency

**All sinks go through one queue drained by a single writer thread.** `events.jsonl`, `run.log`,
`timeline.jsonl`, `capture.har` and the `http/NNN_*.json` files are written from a synchronised burst
of threads at the fire instant; without a single writer they would interleave-corrupt exactly the
record they exist to capture.

The `NNN` counter and `corr_id` come from the same lock-protected counter, assigned at send time.
`tests/test_capture.py` runs several concurrent threads through a `RunLogger` and parses every emitted
line back to prove no record is torn.

Ordering uses `time.monotonic_ns()`; the wall clock is for display only.

## `summary.json`

```json
{
  "run_id": "…", "auction_id": "…", "auction_version": "AuctionbyProposal",
  "fire_time": "2026-07-28T21:00:00Z",
  "start_date": "2026-07-28T21:00:00Z",
  "fire_time_vs_start_date_ms": 0,
  "clock": {
    "applied": false, "shift_ms": 0.0,
    "date_header": {"offset_ms": 209, "residual_width_ms": 464, "samples": 12},
    "event_log": {"offset_ms": -945.0, "residual_width_ms": 30.0, "candidates": 1},
    "signalr": {"offset_ms": null, "samples": 0},
    "disagreement_ms": null, "host_ntp": "…"
  },
  "gate": {"mode": "once_before", "issued_offset_ms": -1500, "state": "ReadyToStart", "rtt_ms": 121},
  "mode": "burst",
  "schedule_planned": [{"wave": 0, "offset_ms": -60, "direct": 1, "validate": 0, "validate_only": 0}],
  "waves_planned": 4, "waves_fired": 4, "waves_dropped": 0,
  "submissions_planned": 4, "submissions_sent": 4,
  "audited_requests": 5,
  "outcome": "BID_PLACED",
  "winner": {"thread": "D-1", "role": "D", "wave": 2, "scheduled_offset_ms": 0,
             "actual_offset_ms": 6, "bid_id": 12345, "server_date": "…", "rtt_ms": 180},
  "bid_value": "1", "items_count": 1, "total_value": "1.000000",
  "my_last_bid": "0.00", "last_bid": "0.00",
  "secop_record": {
    "read_at": "…", "reference": "2026-07-28T21:00:00+00:00", "reference_source": "startDate",
    "company_code": "…", "my_last_bid": "0.0", "last_bid": "0.0",
    "bids": [{"bid_id": 1, "company": "…", "own": false, "date": "…", "after_start_ms": 44.4,
              "total_value": "0.000000000000", "is_valid": true}],
    "own_bid_rows": {"count": 14, "first": "…", "first_after_start_ms": 11580.0,
                     "last": "…", "last_after_start_ms": 35280.0, "first_description": "…"},
    "own_start_rows": 1, "auction_start_row_after_start_ms": 159.0,
    "event_log_rows": 61, "event_log_truncated": false, "unread": []
  },
  "attempts": [],
  "failure_taxonomy": ["BID_ERR_MIN", "STATE_NOT_STARTED"],
  "unverified_routes": ["bid/CreateBid", "auction/GetAuction"],
  "config_redacted": {},
  "started_at": "…", "ended_at": "…"
}
```

`submissions_planned` and `submissions_sent` are distinct fields on purpose — the difference is what
`stop_on_success` actually saved you, which at the recommended schedule densities is zero.

`audited_requests` is the number that matters for footprint, and it counts **requests rather than
threads**: a validate-then-submit thread sends both `ValidateBid` and `CreateBid`, validate-only probe
threads still write rows, and `do_start_audit` adds one for the whole run. All three are invisible to
the naive submission count. It is a projection of what the configuration would send — see
[safety.md](safety.md#count-the-footprint-before-you-fire) for the formula.

`clock.event_log` is where SECOP's application clock sat against this host's, from the start audit's
own row (`null` offset when the row was not read back). `clock.shift_ms` is how far `T` was moved onto
it — zero unless `clock.applied` is true.

`secop_record` is what the server wrote down, read back `secop_record_delay_s` after the last release:
every bid in the bid list with its company, its server timestamp and whether it is yours; how many of
your company's bids the server processed and when; and `unread`, naming any of the three reads that
failed. Every `*_after_start_ms` is measured from `reference` on SECOP's own clock. It is `null` for a
run that sent nothing, or with `read_secop_record` off. It is reported **beside** `outcome` and never
changes it: `outcome` is what this process saw.

`unverified_routes` lists the endpoints the run used that have never been observed on the wire, so a
404 at the fire instant is diagnosable from the summary alone.

**`outcome` is `BID_PLACED` if any wave succeeded**, regardless of how many others tagged
`STATE_NOT_STARTED` or `DUPLICATE`. Those are the designed behaviour of a spread schedule, not
failures. Only `ALL_REJECTED` and `ERROR` mean nothing landed.

**`NO_VERDICT` means the run cannot say.** Bids were sent and none was answered — every one timed out
or failed to connect. A timeout is not a rejection; the server may process the request afterwards, and
`secop_record` is where that shows.

## Failure taxonomy

Every failure is tagged with exactly one value:

`AUTH_EXPIRED`, `AUTH_EXPIRING`, `STATE_NOT_STARTED`, `STATE_PAUSED`, `STATE_FINISHED`,
`STATE_UNKNOWN`, `BID_ERR_MIN`, `VALIDATION_FAILED`, `WAF_BLOCK`, `RATE_LIMIT`, `NETWORK`,
`MALFORMED_PAYLOAD`, `DUPLICATE`, `CLOCK_SYNC_FAILED`, `PARSE_FAILED`, `PARSE_AMBIGUOUS`,
`CONFIG_INVALID`, `FIRE_TIME_PAST`, `FIRE_TIME_TOO_FAR`, `UNSUPPORTED_AUCTION_VERSION`, `OK`,
`UNKNOWN`.

One classifier function maps HTTP status plus response body to one value, and the raw evidence is
logged alongside the tag. Unknown `errorType` values are never mapped away — they are logged verbatim
so a new server behaviour shows up as itself rather than as `UNKNOWN`.

`classify()` also takes the run state, because `DUPLICATE` and `BID_ERR_MIN` are byte-identical on the
wire and only local send ordering separates them.
