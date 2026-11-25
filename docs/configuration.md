# Configuration reference

Config is a JSON file passed with `--config`. Any key can be overridden on the command line with
`--set key=value`. Unknown keys are a hard error, not a warning — a typo in `fire_time` should not
silently arm a run at the wrong instant.

A minimal config:

```json
{
  "jwt": "eyJhbGciOiJSUzI1NiIs…",
  "auction_page_html_path": "./beforebid.html",
  "bid_value": "7500000",
  "fire_time": "2026-07-28T21:00:00Z"
}
```

That is a `single`-mode run with one direct-submit thread, firing at the specified instant.

> `.toml` is accepted only if `tomllib` or `tomli` can be imported. Python 3.9 ships neither, so on the
> supported interpreter **JSON is the format**.

## Required

| Key | Type | Meaning |
|---|---|---|
| `jwt` | string | Bearer token, pasted from a logged-in browser session. Lives ~24 h and cannot be refreshed non-interactively. Prefer keeping this out of a committed file. |
| `auction_page_html_path` | path | A saved SECOP page. The real artifacts are 300–500 KB, which is why this is a **file path** and not an inline string. Either supported page works — see [below](#which-page-to-save). |
| `bid_value` | string | The **per-unit** value applied to **every** item. Colombian or wire format; see [number formats](#number-formats). Zero is allowed and warned about — the server accepts it ([Q16](open-questions.md#q16--how-does-the-server-treat-a-zero-bid)); negative is rejected. |
| `fire_time` | string | The instant `T` the app fires at, ISO-8601 **with an explicit offset or `Z`**. A naive timestamp is a config error, never a zone to guess. Never derived from the server. |

## Auction identification

| Key | Default | Meaning |
|---|---|---|
| `auction_id` | *unset* | Explicit GUID override. Required when the saved page lists several auctions — the "my auctions" widget is multi-row by construction. An override must still be **found on the page**, or the run aborts. |

## The bid

| Key | Default | Meaning |
|---|---|---|
| `bid_is_total` | `false` | **Rejected if set to `true`.** Kept for schema fidelity only; its semantics were never settled and it has never been exercised. See [Q14](open-questions.md#q14--should-bid_is_total-exist). |

## Firing schedule

| Key | Default | Meaning |
|---|---|---|
| `mode` | `single` | `single` \| `burst` \| `custom`. See [timing.md](timing.md#waves). |
| `n_direct` | `1` | Direct-submit threads. `single` mode only. |
| `n_validate_submit` | `0` | Validate-then-submit threads. `single` mode only. |
| `n_validate_only` | `0` | Validate-only probe threads. `single` mode only. **These still write audit rows.** |
| `bid_count` | `1` | `burst`: number of waves. |
| `pre_ms` | `0` | `burst`: start firing this many ms **before** `T`. |
| `interval_ms` | `0` | `burst`: spacing between waves. **Required when `bid_count > 1`.** |
| `wave_direct` | `1` | `burst`/`custom`: direct submits per wave. |
| `wave_validate` | `0` | `burst`/`custom`: validate-then-submit per wave. |
| `wave_validate_only` | `0` | `burst`/`custom`: validate-only probes per wave. |
| `schedule_offsets_ms` | `[]` | `custom`: explicit wave offsets vs `T`, negative meaning before. e.g. `[-100,-50,-20,0,20,50]`. |
| `v_gate_on_valid` | `true` | Validate-then-submit threads only submit if `ValidateBid` came back valid. |
| `stop_on_success` | `true` | Cancel unreleased waves after a win. **Inert at the recommended densities** — see [safety.md](safety.md#stop_on_success-will-not-save-you). |

`burst` with `bid_count > 1` and `interval_ms < 1` is a config **error**, not a warning: every wave
would collapse onto `T`, giving you `single` mode with a bigger thread count and the maximum audit
footprint, while looking like a spread schedule.

## Timing

| Key | Default | Meaning |
|---|---|---|
| `lead_ms` | `0` | Fire this early to offset transit. Set deliberately from a low quantile of `timings.wait`; never auto-derived. |
| `validate_lead_ms` | `0` | How early validate threads start their `ValidateBid`, so their follow-up `CreateBid` lands near the wave instant. Depends on [Q3](open-questions.md#q3--is-validatebid-accepted-before-the-auction-is-started). |
| `start_audit_lead_ms` | `60000` | How far before the **first** release the run's one `SetStartBiddingAudit` is sent. It travels on a connection no bid uses, so it can never delay a bid; the lead exists to put it ahead of the opening rush, where a third to half of requests failed on 2026-11-20, and to leave time to read its row back as a clock sample. A run armed later than the lead sends it at once. Warns when it is inside the last connect+read timeout before the release. Runs of several accounts under **one company code** should use leads at least `2 × clock_window_ms + 1000` ms apart, or their rows cannot be told from one another. |
| `keepalive_offsets_ms` | `[-45000, -30000, -15000]` | Instants, relative to the first release, at which every bid connection sends one `GetAuctionSimple`, all at once. A connection that idled out since arming is re-opened here rather than by the bid at `T`. All must be negative, and the last must be at least `2 × keepalive_timeout_ms + 2000` ms (plus `gate_lead_ms` when the gate runs) before the release, or the config is refused: nothing may share a connection with its bid at the release instant. `[]` disables it. A run armed after an instant skips that round. How long this gateway keeps an idle connection is unknown ([Q17](open-questions.md#q17--how-long-does-the-gateway-keep-an-idle-connection)), so the spacing is a guess. |
| `keepalive_timeout_ms` | `3000` | Connect and read timeout of one keep-alive. Deliberately far below a bid's: a keep-alive that stalls must give the connection back long before the bid needs it. |
| `max_arm_ahead_s` | `3600` | Abort if `fire_time` is further ahead than this. A long-armed run outlives the 24 h JWT. |
| `fire_time_mismatch_warn_s` | `2` | Warn — **never override** — if `fire_time` differs from the server's `startDate` by more than this. |
| `rtt_samples` | `10` | Preflight RTT samples. Below 10 the distribution is meaningless: the observed range spans 90–1138 ms. |

## Clock diagnostics

| Key | Default | Meaning |
|---|---|---|
| `clock_probe` | `date_header` | `date_header` \| `signalr` \| `both` \| `none`. Which probes to **record**. |
| `apply_clock_offset` | `false` | Move `T` onto the clock SECOP stamps its event log and bids with. The offset is measured from this run's own start audit, read back from the log ([timing](timing.md#secops-two-clocks)). Off by default and warned about when on: it rests on two auctions of one day. The run **declines to move `T`**, and says so on stdout, in `events.jsonl` and in the summary's warnings, when the row was not read back by the last safe moment, the feasible interval is wider than `clock_max_width_ms`, the offset is beyond `clock_window_ms`, or `do_start_audit` is off. |
| `clock_window_ms` | `5000` | A start-bidding row of your company stamped within this distance of the audit is a candidate for "the row this run wrote". Also the largest offset that may move `T`. |
| `clock_fallback_shift_ms` | `null` | A `clock-probe` reading, in ms, used to move `T` when this run's own reading is declined — so a failed measurement does not leave the schedule on real time, about a second early on SECOP's clock. `clock-probe` prints the value to paste. Needs `apply_clock_offset`; must be within `clock_window_ms`. A usable live reading always wins, and a gap of more than 100 ms between the two is reported. |
| `clock_max_width_ms` | `500` | The widest feasible interval that may move `T`. One clean round trip to SECOP is tens of milliseconds wide; a stalled one, or several candidate rows, is wider and is not acted on. |

`signalr` is not implemented and raises. See
[Q13](open-questions.md#q13--can-the-signalr-handshake-be-reconstructed).

## State gate

| Key | Default | Meaning |
|---|---|---|
| `gate_mode` | `once_before` | `once_before` \| `off`. Never per-wave — it costs a full round trip. |
| `gate_lead_ms` | `1500` | How far before `T` the single gate check is issued, so its **response** lands before release. Comfortably past the observed p95. |

## Audit endpoints

| Key | Default | Meaning |
|---|---|---|
| `do_start_audit` | `true` | POST `SetStartBiddingAudit` **once per run** — one row, however many bids. The browser sends one per click; the app sent one per thread until 2026-11-20, when audits sharing their bid's connection held bids back by seconds. Whether the server needs the row at all before a bid is unknown ([Q15](open-questions.md)); bids whose own audit had failed were still processed. It is also the run's clock sample. |
| `read_secop_record` | `true` | After the schedule, read `GetBidsTotalInfo`, `GetSupplierBids` and one page of `GetEventLogs`, and put what they say in `summary.secop_record`: every accepted bid with its company and server timestamp, and how many of your bids the server processed and when. Three `GET`s, no row written. Skipped when the run sent nothing. |
| `secop_record_delay_s` | `30` | How long after the last release that read waits. On 2026-11-20 the server was still processing this app's bids 35 s after the opening; a read taken at once finds none of them. Ctrl-C during the wait skips the read and keeps the summary. |
| `do_submit_audit` | `false` | POST `SetSubmitBiddingAudit` after a win. Defaults off: that endpoint was **never observed**, and the SPA's inverted function naming suggests it is the session-open audit, not the post-submit one. |

## Safety

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `false` | Do everything **except** `CreateBid`. Not footprint-free — [see why](safety.md#dry_run-is-not-dry). |
| `confirm_live` | `false` | Must be true for any real `CreateBid`. Set it with `--yes-i-mean-it` on the command line rather than in the file, so it cannot become a habit. |
| `auth_safety_margin_s` | `300` | Abort with `AUTH_EXPIRING` if the token has less than this left. |

## HTTP

| Key | Default | Meaning |
|---|---|---|
| `connect_timeout_ms` | `3000` | TCP+TLS connect timeout. Without one, `requests` blocks forever and a single hung socket in a barrier-synchronised burst hangs the run past the auction window. |
| `read_timeout_ms` | `5000` | Response timeout. Max observed bid response is 1633 ms. |
| `user_agent` | captured Chrome UA | A library default UA is believed to draw a 403. |

**Network errors are never retried.** A retry at the fire instant lands at an unknown time and writes
another audit row.

## Output

| Key | Default | Meaning |
|---|---|---|
| `run_root` | `runs` | Root for per-run directories, `runs/<UTC-timestamp>_<run_id>/`. |

## Validation

`config.validate()` raises `SecopError(CONFIG_INVALID)` on:

- `fire_time` missing, naive, in the past (`FIRE_TIME_PAST`), or beyond `max_arm_ahead_s`
  (`FIRE_TIME_TOO_FAR`)
- no submitting threads **and** no probe threads **and** not a dry run
- `mode=burst` with `bid_count < 1`, or `bid_count > 1` with `interval_ms < 1`
- `mode=custom` with an empty `schedule_offsets_ms`
- `bid_is_total` set true
- any enum key outside its allowed set

- a `keepalive_offsets_ms` entry at or after the first release, or close enough to it that the
  keep-alive could still hold a bid's connection

and warns on:

- more than 10 audited requests
- `apply_clock_offset` enabled — and again if `do_start_audit` is off or the audit's lead is too short
  for the read-back to finish
- `start_audit_lead_ms` inside the opening rush
- `dry_run` enabled — because it still writes buyer-visible rows

A probe-only run and a dry run are both **legal** and simply fire no `CreateBid`.

## Number formats

Two formats exist and mixing them is the most expensive mistake available.

| Context | Thousands | Decimal | 7,813,974.32 |
|---|---|---|---|
| Wire / JSON body | none | `.` | `7813974.32` |
| UI and server messages (es-CO) | `.` | `,` | `7.813.974,32 COP` |

For `bid_value`, the rule is decidable and strict:

| Input | Reading |
|---|---|
| `1`, `1234567.89`, `7813974.32` | wire — accepted |
| `7.813.974,32`, `390.698.716` | Colombian — accepted |
| **`1.234`**, **`1.500`** | **rejected** |

A single dot with exactly three fractional digits is genuinely ambiguous — `1.234` is 1234 in
Colombian and 1.234 on the wire, and there is no tiebreaker. The app refuses rather than guessing.
Write `1234` or `1.2340` to disambiguate.

## Which page to save

Open the auction in your already-logged-in browser and save the DOM — devtools "Copy outer HTML", not
view-source. Both fixtures are DOM serialisations, and the parser's whitespace handling is calibrated
against that form.

| Page | Yields | Extractor |
|---|---|---|
| Opportunity list ("Oportunidades") | `auction_id`, `market`, `eauction_doc_id`, `mkey`, state text | onclick |
| Auction launch ("Integración") | `auction_id`, `lang`, `market` | iframe |

The app detects which one you gave it. There is no URL fetching and no portal session: that page lives
on `www.secop.gov.co`, a **separate application the eauctions JWT does not authenticate**, so an
unauthenticated fetch just redirects to the login. Saving the rendered HTML sidesteps a second,
separately-expiring session entirely.

Verify a page parses before you need it, with no network and no risk:

```bash
.venv/bin/python -m secop_firstbid parse-page --page ./beforebid.html
```
