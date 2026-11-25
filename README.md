# secop_firstbid

A scheduled, multi-threaded **first-bid submitter** for SECOP II electronic reverse auctions
(*subastas inversas*) on `subastas.secop.gov.co`.

Give it a JWT you harvested from your own logged-in browser session, a saved auction page, one bid
value, and one fire instant. At that instant it fires a configurable set of concurrent threads, records
the first accepted bid, and logs everything in enough detail to reconstruct the run to the millisecond.

It places **one logical bid** and stops. There is no bid-down loop, no re-bidding, and no strategy —
your value goes onto the wire exactly as you supplied it.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# offline: check the page parses
.venv/bin/python -m secop_firstbid parse-page --page ./beforebid.html

# offline: build the schedule and payload, print the footprint — no network, no bids
.venv/bin/python -m secop_firstbid plan --config run.json

# network, no bids: token check, auction detail, RTT and clock diagnostics
.venv/bin/python -m secop_firstbid preflight --config run.json

# the real thing
.venv/bin/python -m secop_firstbid run --config run.json --yes-i-mean-it
```

Minimal `run.json` — four keys, and the other thirty-four take their defaults:

```json
{
  "jwt": "eyJhbGciOiJSUzI1NiIs…",
  "auction_page_html_path": "./beforebid.html",
  "bid_value": "7500000",
  "fire_time": "2026-07-28T21:00:00Z"
}
```

That is `single` mode with one direct-submit thread. Everything below is what you can change.

## Read this before using it

Three things surprise everyone, and all three are consequences of the system rather than bugs in this
code.

**You are not anonymous, and a "dry run" is not invisible.** Every bid attempt — including every
`ValidateBid` pre-check, which is what `dry_run` still sends — writes a row into a server-side audit
log the auction owner can read, carrying your **public IP address** and your **company code**. This is
observed, not theorised. If you want a genuinely zero-footprint rehearsal, use `plan`, which never
touches the network. → [safety.md](docs/safety.md)

**You specify the fire instant, and the app will not second-guess you.** It does not derive `T` from
any server clock. The HTTP `Date` header is whole-second, so the obvious estimator is *biased* by about
half a second rather than merely noisy; the two server clocks available disagree with each other by
that much again; and neither is tied by any evidence to the clock that actually opens the auction. A
manual `T` doesn't create accuracy — it moves the error onto your NTP-synced host clock, where it is a
few milliseconds and you can measure it. **Sync your host before every run.** →
[timing.md](docs/timing.md)

**No bid has ever actually been submitted through this route.** `bid/CreateBid` appears zero times in
the source capture. A live probe on 2026-07-29 confirmed the route resolves and that its casing
doesn't matter, and captured the real `GetAuction` body — which corrected the item mapping on two
fields the plan had wrong. What is still assumed: what a *successful* `CreateBid` returns, how a
pre-open bid behaves, and whether the server tolerates concurrency at all.
→ [evidence.md](docs/evidence.md),
[open-questions.md](docs/open-questions.md)

## Commands

```
secop-firstbid <command> [options]
```

Four subcommands, ordered by what they cost you. With no command it prints help and exits `2`.

| Command | Network | Audit rows | What it does |
|---|---|---|---|
| `parse-page` | none | none | Resolves the auction from a saved page and prints what it found. |
| `plan` | none | none | Builds the schedule, the payload and the footprint offline. The only zero-footprint rehearsal. |
| `preflight` | yes | none | Token check, auction detail, payload, RTT and clock diagnostics. Sends no bid and no audit POST. |
| `run` | yes | yes | The full pipeline. Submits only with `--yes-i-mean-it`. |

### `parse-page` — resolve the auction offline

```bash
.venv/bin/python -m secop_firstbid parse-page --page ./beforebid.html
```

| Flag | Required | Meaning |
|---|---|---|
| `--page PATH` | yes | The saved auction HTML. Save the **DOM** (devtools "Copy outer HTML"), not view-source. |
| `--auction-id GUID` | no | Disambiguates a multi-auction page. The id must be **present on the page**, or it aborts. |

Takes no config file and writes no run directory. Prints `auction_id`, which extractor matched
(onclick or iframe), language and market with a marker when either was defaulted, the doc id,
redirect path, whether `mkey` is present, the state text, and the `Referer` it would send. Absent
state text is reported as `UNKNOWN`, which is not the same as "not finished".

### `plan` — offline rehearsal

```bash
.venv/bin/python -m secop_firstbid plan --config run.json
.venv/bin/python -m secop_firstbid plan --config run.json \
  --auction-simple ./simple.json --auction ./auction.json
```

| Flag | Required | Meaning |
|---|---|---|
| `--config PATH` | yes | JSON config file. |
| `--set KEY=VALUE` | no | Override one config key; repeatable. See [`--set`](#--set-overrides). |
| `--auction-simple PATH` | no | A saved `GetAuctionSimple` body — carries `auctionVersionCode` and `currencyCode`. |
| `--auction PATH` | no | A saved `GetAuction` body — carries the item list. |

The two snapshot flags **go together**; one without the other is a config error. Given both, `plan`
builds the payload you would actually send. Given neither, it builds a *provisional* payload against a
stand-in item and says so loudly — the schedule and footprint are real, the bytes are not. Either flag
also accepts a run directory's `http/NNN_*.json` exchange file, not just a bare response body.

`plan` constructs no HTTP client at all, rather than merely declining to send. It still writes a run
directory (`payload.json`, `summary.json`, `events.jsonl`) so the rehearsal is reviewable.

### `preflight` — network, no bids

```bash
.venv/bin/python -m secop_firstbid preflight --config run.json
```

Takes `--config` and `--set`, nothing else. It checks the token and prints its claims and remaining
life, fetches `GetAuctionSimple` / `GetAuction` / `GetBidsTotalInfo`, builds and prints the real
payload, warns if `fire_time` disagrees with the server's `startDate` by more than
`fire_time_mismatch_warn_s`, then runs `rtt_samples` clock probes and prints the schedule span, the
elapsed-time quantiles, an **advisory** `lead_ms`, and the `Date`-header offset midpoint with its
residual width. Advisory is literal: nothing measured here moves `T`.

The `Date` header is **not** the clock the auction opens on. It comes from SECOP's web front end and
tracks real time; the application stamps its event log and its bids with another clock, which read
0.9–1.0 s behind on 2026-11-20. `preflight` cannot see that one — it sends no request the application
stamps. `clock-probe` can.

### `clock-probe` — place SECOP's log clock (one audit row, no bid)

```bash
.venv/bin/python -m secop_firstbid clock-probe --config run.json
```

Takes `--config` and `--set`. Sends **one** `SetStartBiddingAudit`, reads the event log back, finds
the row that request wrote and compares the server's timestamp on it with the instants the request
left and was answered. The result is the offset of SECOP's log clock from this host's, as an interval
no wider than that one round trip.

It writes **one buyer-visible row** ("Comenzar Lances") under your public IP and company code, and
sends no bid. `fire_time` must still be valid, so it works in the hour before the auction. Exit `6`
if the row could not be read back.

A `run` makes the same measurement from its own start audit, at no extra row, and reports it in the
summary. It moves `T` by it only with `apply_clock_offset=true`.

### `run` — the full pipeline

```bash
.venv/bin/python -m secop_firstbid run --config run.json --yes-i-mean-it
```

| Flag | Required | Meaning |
|---|---|---|
| `--config PATH` | yes | JSON config file. |
| `--set KEY=VALUE` | no | Override one config key; repeatable. |
| `--yes-i-mean-it` | no | The **only** thing that sets `confirm_live` and permits a real `CreateBid`. |

`confirm_live: true` in a config file is reported and **overruled** — arming a live bid cannot become
a property of a file somebody edited last week. Without the flag and without `dry_run`, the run does
everything up to the fire instant, warns, and then refuses to submit.

## Configuration

Config is JSON passed with `--config`; any key can be overridden with `--set`. **Unknown keys are a
hard error** with a did-you-mean suggestion — a silently ignored `bid_calue` is a bid at a default
nobody typed. `.toml` is accepted only when `tomllib` (3.11+) or `tomli` is importable; on the
supported 3.9 interpreter, JSON is the format.

Every key, its type, its default. The reasoning behind each is in
[docs/configuration.md](docs/configuration.md).

**Required**

| Key | Type | Meaning |
|---|---|---|
| `jwt` | str | Bearer token harvested from your own logged-in session. ~24 h, no non-interactive refresh. |
| `auction_page_html_path` | str | Path to the saved page. A path, not inline HTML — the real artifacts are 300–500 KB. |
| `bid_value` | str | **Per-unit** value applied to every item. Must be a quoted string: a JSON number decodes to a float. |
| `fire_time` | str | The instant `T`, ISO-8601 with an explicit offset or `Z`. A naive timestamp is an error, never a zone to guess. |

**Auction and bid**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `auction_id` | str\|null | `null` | Explicit GUID for a multi-auction page; must be found on the page. |
| `bid_is_total` | bool | `false` | **Rejected if true.** Kept for schema fidelity; semantics never settled. |

**Firing schedule**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | str | `single` | `single` \| `burst` \| `custom`. |
| `n_direct` | int | `1` | `single`: direct-submit (D) threads. |
| `n_validate_submit` | int | `0` | `single`: validate-then-submit (V) threads. |
| `n_validate_only` | int | `0` | `single`: validate-only (P) probes. **These still write audit rows.** |
| `bid_count` | int | `1` | `burst`: number of waves. |
| `pre_ms` | int | `0` | `burst`: start this many ms before `T`. |
| `interval_ms` | int | `0` | `burst`: spacing between waves. **Required when `bid_count > 1`.** |
| `wave_direct` | int | `1` | `burst`/`custom`: D threads per wave. |
| `wave_validate` | int | `0` | `burst`/`custom`: V threads per wave. |
| `wave_validate_only` | int | `0` | `burst`/`custom`: P probes per wave. |
| `schedule_offsets_ms` | list[int] | `[]` | `custom`: explicit wave offsets vs `T`, negative meaning before. |
| `v_gate_on_valid` | bool | `true` | V threads submit only if `ValidateBid` came back valid. |
| `stop_on_success` | bool | `true` | Cancel unreleased waves after a win. Inert at the recommended densities. |

**Timing**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `lead_ms` | int | `0` | Fire this early to offset transit. Set deliberately; never auto-derived. |
| `validate_lead_ms` | int | `0` | How early V threads start their `ValidateBid`. |
| `start_audit_lead_ms` | int | `60000` | How far before the **first** release the run's one `SetStartBiddingAudit` goes out, on a connection no bid uses. Early enough to be ahead of the opening rush and to be read back as a clock sample. Warns when it is inside the last connect+read timeout before the release. |
| `keepalive_offsets_ms` | list[int] | `[-45000,-30000,-15000]` | Instants, relative to the first release, at which every bid connection sends one cheap read so it is open and warm at `T`. All negative. `[]` turns it off. Refused if the last one could still be in flight at the release. |
| `keepalive_timeout_ms` | int | `3000` | Connect **and** read timeout of a keep-alive — far below a bid's, so it is over before the bid needs the connection. |
| `max_arm_ahead_s` | int | `3600` | Abort if `fire_time` is further ahead than this — a long-armed run outlives the token. |
| `fire_time_mismatch_warn_s` | int | `2` | Warn, never override, if `fire_time` differs from the server's `startDate` by more. |
| `rtt_samples` | int | `10` | Preflight RTT samples. Below 10 warns; observed range spans 90–1138 ms. |

**Clock diagnostics**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `clock_probe` | str | `date_header` | `date_header` \| `signalr` \| `both` \| `none`. Which probes to **record**. `signalr` is not implemented and raises. |
| `apply_clock_offset` | bool | `false` | Move `T` onto the clock SECOP stamps its log with, measured from this run's start audit. Off by default; warns if enabled. Declines, loudly, when the row was not read back in time, the interval is wider than `clock_max_width_ms`, or the offset is beyond `clock_window_ms`. Needs `do_start_audit`. |
| `clock_window_ms` | int | `5000` | How far from the audit a start-bidding row of your company may be stamped and still count as a candidate for "this run's row"; also the largest offset that may move `T`. |
| `clock_max_width_ms` | int | `500` | The widest feasible interval that may move `T`. |
| `clock_fallback_shift_ms` | int or null | `null` | Shift `T` by this `clock-probe` reading when the run's own reading is declined. `clock-probe` prints the value. A usable live reading wins; a gap over 100 ms is reported. |

**State gate**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `gate_mode` | str | `once_before` | `once_before` \| `off`. Never per-wave — it costs a full round trip. |
| `gate_lead_ms` | int | `1500` | How far before `T` the single gate check is issued, so its *response* lands before release. |

**Audit endpoints**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `do_start_audit` | bool | `true` | POST `SetStartBiddingAudit` **once per run** — one row, however many bids — `start_audit_lead_ms` before the first release, on its own connection. It is also the clock sample. |
| `read_secop_record` | bool | `true` | After the schedule, read SECOP's own bid list and event log and put who won, and when your bids were processed, in the summary. Three `GET`s; writes no row. |
| `secop_record_delay_s` | int | `90` | Wait this long after the last release before that read, so the server has finished processing what was sent. |
| `secop_record_attempts` | int | `4` | Read again while any of the three reads fails, up to this many times. |
| `secop_record_retry_s` | int | `60` | Seconds between those reads. |
| `do_submit_audit` | bool | `false` | POST `SetSubmitBiddingAudit` after a win. Never observed on the wire; warns if enabled. |

**Safety**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dry_run` | bool | `false` | Everything except `CreateBid`. **Not footprint-free.** Warns whenever set. |
| `confirm_live` | bool | `false` | Must be true for any real `CreateBid`. Settable only by `--yes-i-mean-it`. |
| `auth_safety_margin_s` | int | `300` | Abort `AUTH_EXPIRING` if the token has less than this left. |

**HTTP and output**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `connect_timeout_ms` | int | `8000` | TCP+TLS connect timeout. Without one a hung socket outlives the auction window. |
| `read_timeout_ms` | int | `60000` | How long each bid waits for SECOP's answer. In the live auctions SECOP took 4–35 s to process a bid, and on 2026-11-23 none of the bids given up on after 10 s was processed. Nothing is retried after a timeout. |
| `user_agent` | str | captured Chrome UA | A library default UA is believed to draw a 403. |
| `run_root` | str | `runs` | Root for per-run directories. |

**Network errors are never retried.** A retry at the fire instant lands at an unknown time and writes
another audit row.

### What validation rejects

`fire_time` missing, naive, in the past or past `max_arm_ahead_s`; a `bid_value` that is unparseable,
non-string or negative; the ambiguous `1.234` form (write `1234` or `1.2340`); `bid_is_total`
true; any enum outside its set; `burst` with `bid_count < 1`, or `bid_count > 1` with `interval_ms < 1`;
`custom` with no offsets; a schedule with no submitters *and* no probes. Structural errors are
collected and reported together, because an operator fixing a config one exception at a time is an
operator who arms late.

It also refuses a `keepalive_offsets_ms` entry at or after the first release, or one close enough to
it that the keep-alive could still hold a bid's connection when the bid needs it.

It **warns** — and proceeds — on a zero `bid_value`, a planned footprint above 10 audited requests, `apply_clock_offset`,
a `start_audit_lead_ms` inside the opening rush, `dry_run`, `do_submit_audit`, `rtt_samples` below 10,
and on `confirm_live` itself. A probe-only run and a dry run are both legal.

### `--set` overrides

Repeatable, and wins over the file. Unknown keys are fatal here too.

```bash
--set dry_run=true --set mode=burst --set bid_count=3 --set interval_ms=25
--set schedule_offsets_ms=-100,-50,-20,0,20    # list[int]: comma-separated, no brackets
--set auction_id=null                          # optional strings: null/none/empty clears
```

Booleans accept `true/yes/on/1` and `false/no/off/0`. Integers are base-10. Strings are taken
verbatim. `confirm_live` is the exception to the whole mechanism: `--set confirm_live=true` still
leaves it false, because only `--yes-i-mean-it` sets it.

## Web app

The same bot, driven from a logged-in web page on the server: paste the auction page and the
token, set the opening and the offsets, preflight, probe SECOP's clock, arm, and read the result.
It runs these same commands underneath and adds nothing to how a bid is fired.

```bash
bash deploy/install.sh          # on the server: certificate, login, systemd service
```

See [docs/webapp.md](docs/webapp.md).

### `record` — read SECOP's result (no row)

```bash
.venv/bin/python -m secop_firstbid record --config run.json [--host-address 203.0.113.7] [--out record.json]
```

Reads the bid list and the whole event log and prints who holds the accepted bid and when SECOP
processed your bids. With `--host-address`, it counts this host's bids apart from your company's
bids from other addresses. Three kinds of `GET`; nothing is written.

## Exit codes

| Code | Name | When |
|---|---|---|
| `0` | OK | Won, or nothing was meant to be submitted (dry run, probe-only). |
| `1` | ERROR | Unhandled failure, or interrupted. |
| `2` | CONFIG | Invalid config, fire time past or too far, unsupported auction version, no command given. |
| `3` | PARSE | Page parse failed or was ambiguous; malformed payload. |
| `4` | AUTH | Token expired, or inside `auth_safety_margin_s`. |
| `5` | ALL_REJECTED | Bids were sent and at least one was answered, and every answer was a bounce — validation, duplicate, or an auction state that refuses them. |
| `6` | NETWORK | Network failure, rate limit, WAF block, clock sync failure. Includes the run whose every bid timed out — outcome `NO_VERDICT`: nothing was answered, so nothing is known from the run itself. Read the `SECOP says` lines. |

A run that submitted nothing because the state gate stopped it exits with the gate's own reason,
which for the `STATE_*` failures is `5`.

## What a run leaves behind

Every invocation except `parse-page` — including the offline `plan` — creates
`<run_root>/<UTC-timestamp>_<run_id>/`:

| File | Contents |
|---|---|
| `config.json` | The fully resolved config, JWT redacted to `first6…last6`, plus decoded token claims on `preflight` and `run`. |
| `events.jsonl` | One JSON object per event. The primary log. |
| `http/NNN_<role>_<thread>_<endpoint>.json` | One file per HTTP exchange, full request and response, redacted. |
| `capture.har` | HAR 1.2 of every request in the run, openable in the same tools this project was derived from. |
| `timeline.jsonl` | Monotonic timeline: barrier releases, each send, each receive, first success. |
| `payload.json` | The exact compact wire bytes plus the build report. Written by `plan` and `preflight`. |
| `summary.json` | Machine-readable outcome. |
| `run.log` | Human-readable text log, `INFO` and above. |

Every request is logged, including the ones that timed out or never returned — `corr_id`, `thread_id`,
`role` and wave index are assigned *before* sending. Nothing is sampled or dropped.
→ [logging.md](docs/logging.md)

## How it works

```
saved HTML ──▶ AuctionRef ──▶ GetAuctionSimple / GetAuction / GetBidsTotalInfo ──▶ AuctionDetail
                                                                                        │
                                              bid_value ────────────────────────────────┤
                                                                                        ▼
fire_time ──▶ T_wall ──▶ T_mono ──▶ waves ──▶ [barrier] ──▶ D/V/P threads ──▶ BidPayload bytes
                                                                 │
                                                                 ▼
                                                        first success ──▶ summary.json
```

The wall-clock instant is converted to the monotonic clock exactly once, at arm time, so an NTP step
or a DST change mid-countdown can't move your bid. Each wave releases on `time.monotonic_ns()`:
coarse sleep to about 2 ms out, then a busy-wait.

Three thread roles: **D** submits directly at the wave instant (one round trip), **V** validates then
submits (two round trips, self-aborting), **P** validates only and never submits. The state gate runs
**once** ahead of `T` rather than inline — it costs a median 135 ms against a 90 ms schedule span, and
inline it would give D threads two round trips and erase the distinction from V.

**One thread and one warm connection per planned shot.** The run builds one `HttpClient` per
thread — each with its own single-connection pool, all sharing one cookie jar so the gateway's affinity
cookies keep the burst on one backend — and `prewarm` opens them all concurrently at arming.
Nothing is shared on the send path, so a thread blocked on its own response never delays another
thread's release: schedule spacing is a property of the release instants alone, and shots may be spaced
as closely as you like without one waiting on the last.

A connection opened at arming does not survive a long countdown — on 2026-11-20 the ones warmed minutes
earlier were gone by `T`. `keep_warm` therefore re-reads every bid connection at `keepalive_offsets_ms`
before the first release, so each is re-opened *before* the seconds in which SECOP starts refusing
connections rather than by the bid itself.

The corollary of one connection per thread is that anything a thread sends *for itself* **is** serial
with its bid. So the fire threads send nothing but bids. `SetStartBiddingAudit` used to ride on each
bid's connection, first inline and then a fixed lead ahead; on 2026-11-20 audits that hung for their
whole timeout took their thread's bid with them, and 5 to 16 bids per run left more than half a second
late. It is now **one request per run, on a connection of its own**. A keep-alive does use a bid's
connection, which is why it carries a short timeout of its own and why the config is refused if the
last one could still be in flight at the release. V threads have `validate_lead_ms` for the same
reason: their `ValidateBid` occupies the connection their `CreateBid` needs.

**The run reads the result from SECOP, not from the socket.** A bid that times out has not been
rejected — the server processed every one of ours that left the machine, seconds later. After the
schedule the run reads the bid list and the event log back and prints who holds the accepted bid and
when your own bids were handled.

Below the network floor, spacing stops being real. A round trip to `subastas.secop.gov.co` is ~70 ms
warm, so offsets closer together than the run-to-run jitter (a few ms) will not arrive in the order
they were sent. Redundant shots at one offset are honest; 1 ms ladders are not.

Three firing modes: `single` (one wave at `T`), `burst` (`bid_count` waves starting `pre_ms` before
`T`), and `custom` (explicit offsets). → [architecture.md](docs/architecture.md)

## What it doesn't do

- **Obtain the JWT.** That needs an interactive password login through WS-Federation and there is no
  non-interactive refresh path. You harvest it by hand; it lasts 24 hours.
- **Fetch the auction page.** That page is on a different application which your eauctions token does
  not authenticate. You save the HTML yourself.
- **Choose or adjust your bid.** No floor calculation, no decrement logic, no retry with a different
  number.
- **Support `AuctionbyItem` or `AuctionbyLot`.** v1 is `AuctionbyProposal` only; anything else
  hard-aborts rather than guessing.
- **SignalR.** Bidding is plain REST. The hub only carries clock sync, and its handshake was never
  captured.

## Documentation

| | |
|---|---|
| [docs/evidence.md](docs/evidence.md) | **Start here.** What the source capture proves and what it doesn't. |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, and why each design decision was made. |
| [docs/configuration.md](docs/configuration.md) | Every config key. |
| [docs/operations.md](docs/operations.md) | Runbook: cold start → live bid, and how to read the result. |
| [docs/timing.md](docs/timing.md) | The fire instant, the clock, the wave schedule. |
| [docs/safety.md](docs/safety.md) | Footprint, credentials, and what a run costs you. |
| [docs/api.md](docs/api.md) | The SECOP II e-auction HTTP surface. |
| [docs/logging.md](docs/logging.md) | Run directory layout, event and summary schemas, redaction rules. |
| [docs/open-questions.md](docs/open-questions.md) | The unknowns the design depends on, and what the live auctions of 2026-11-20 showed. |
| [AGENTS.md](AGENTS.md) | Conventions and invariants for anyone changing the code. |

The specification is `SECOP_FIRSTBID_PLAN.md`; `PLAN_AUDIT.md` is the audit that corrected it. The
audit is worth reading even if you never touch the code — its account of how a plausible-looking
inference gets written down in the voice of an observation is the most useful thing in the repository.

## Development

```bash
.venv/bin/python -m pytest -q
```

Requires **Python 3.9+** and `requests`. Nothing else at runtime — the JWT is decoded by hand because
we read claims and never verify signatures. Tests are offline; the fixtures under `tests/fixtures/` are
real captured responses with the token scrubbed.

The tests are not decoration. They pin the payload formulas against all eight real captured bid bodies
byte for byte, assert that the naive HTML regex still matches zero bytes, and assert that a bare median
of `Date − now` differs from the honest interval estimate by more than 300 ms. Each of those encodes a
defect that was found and fixed. → [AGENTS.md](AGENTS.md)

## Credentials and artifacts

`www.secop.gov.co.har`, `beforebid.html`, `bid.html` and `runs/` are all in `.gitignore` and should
stay there.

The HAR in particular is a **credential-bearing artifact**: the bearer token was not redacted by the
export — it rides as a plaintext `token=` query parameter on 281 SignalR URLs, alongside the
operator's name, user and company codes. That's also why `redact()` in this codebase rewrites URL
query parameters and not just header dictionaries.

## Scope

This automates a bid you are entitled to place, on an auction you are registered for, using credentials
you obtained by logging in yourself. It does not obtain credentials, evade access controls, or defeat
any protection mechanism.

Two design choices exist to keep it that way: `fire_time` is manual, so the operator stays the
decision-maker, and `confirm_live` is off by default, so a real submission is always a deliberate act.
Both are worth keeping.
# secop-auction-bot-
