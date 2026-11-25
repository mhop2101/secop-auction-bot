# Operations runbook

End-to-end, from a cold start to a live bid. Read [safety.md](safety.md) first if you haven't — several
steps here have a permanent, attributable footprint.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The test suite runs entirely offline. If it doesn't pass, stop — the failing tests are the ones that
encode the formulas and the parser, and a green suite is the only offline evidence you have that the
payload will be correct.

## 1. Sync your host clock

```bash
sudo sntp -sS time.apple.com      # macOS
chronyc tracking                   # Linux
```

Do this every time. A normally-synced machine sits within single-digit milliseconds of UTC, and every
measurement the app makes is made against it.

An accurate host clock is necessary and **not sufficient**: the auction opens on SECOP's application
clock, which read 0.9–1.0 s behind real time on 2026-11-20. Step 6b measures it.

## 2. Save the auction page

In the browser where you are already logged in, open the auction and save the DOM — devtools →
**Copy outer HTML**, or "Save as" from the elements panel. **Not** view-source.

Both supported pages are DOM serialisations with one attribute per line, and the parser's whitespace
handling is calibrated against exactly that form.

| Page | Title | Gives you |
|---|---|---|
| Opportunity list | `Oportunidades` | auction id, market, doc id, mkey, state text |
| Auction launch | `Integración` | auction id, language, market |

Either works; the app detects which one it got.

There is no URL fetching here on purpose. That page lives on `www.secop.gov.co` — the Vortal portal, a
**separate application that the eauctions JWT does not authenticate** — so an unauthenticated fetch just
redirects to the WS-Federation login. Saving the rendered HTML means the app never has to hold a
second, separately-expiring session.

Verify it parses, offline and with no risk:

```bash
.venv/bin/python -m secop_firstbid parse-page --page ./beforebid.html
```

If the page lists several auctions you get `PARSE_AMBIGUOUS` with every candidate listed. That is the
correct behaviour — the "my auctions" widget is multi-row by construction and there is no rule for
which row is "the" one. Pick the right GUID and pass it as `auction_id`; it must still be present on
the page or the run aborts.

## 3. Harvest the JWT

From the same logged-in browser session, take the token the SPA is using. It lives in the launch page
as `data-item-token` on `#div-item-data`, and is also visible on any request the app makes.

It is RS256, minted by `community.secop.gov.co/STS/users/issue.aspx` via an interactive password login,
and lives **24 hours**. There is no non-interactive refresh: a 401 just redirects to the login page.
This is the binding constraint on the whole approach — every run depends on a token you harvested by
hand.

Put it somewhere that won't be committed. The app reads it from the config file; keep that file out of
the repository, or supply it with `--set jwt=…` from a password manager or environment variable.

## 4. Write the config

```json
{
  "jwt": "eyJhbGciOiJSUzI1NiIs…",
  "auction_page_html_path": "./beforebid.html",
  "bid_value": "7500000",
  "fire_time": "2026-07-28T21:00:00Z",
  "mode": "single",
  "n_direct": 2,
  "n_validate_submit": 1
}
```

`fire_time` must carry an explicit offset or `Z`. A naive timestamp is a config error — `timeZoneCode`
is `null` on the server, so there is nothing to infer a zone from and the app will not guess.

Full key reference: [configuration.md](configuration.md).

## 5. Plan — offline, free, do this every time

```bash
.venv/bin/python -m secop_firstbid plan --config run.json
```

Parses the page, builds the waves, builds the payload, and prints the schedule and the footprint. **No
network, no bids, no audit rows.** Check three things:

- The payload is what you expect — especially `bidValue` and `totalValue`.
- The wave offsets are what you expect.
- `audited_requests` is a number you are willing to have logged against your company.

## 6. Preflight — the first thing that touches the network

```bash
.venv/bin/python -m secop_firstbid preflight --config run.json
```

Decodes the token and checks `exp`, makes one authenticated `GetAuctionSimple`, fetches the detail and
item list, takes RTT samples and records clock diagnostics. No bids.

What to look at:

- **Token lifetime.** Comfortably more than `auth_safety_margin_s`. A 401 here means re-harvest before
  doing anything else.
- **`myLastBid` / `lastBid`.** `myLastBid: 0` is the normal state before your first bid and predicts
  nothing: the accepted bid in both auctions of 2026-11-20 was placed from it. `lastBid: 0` is
  ambiguous — nobody has bid, or somebody has bid zero.
- **The RTT distribution.** If the median exceeds your schedule span, the schedule is meaningless.
- **The clock residual width.** Same test.
- **`auctionStateCode`.** `ReadyToStart` before the open is correct and expected.

**Capture the `GetAuction` body** if this auction looks unlike the committed one. The mapping was
verified on 2026-07-29 against `tests/fixtures/getauction.json`, but that is a single auction with a
single item at quantity 1 — a body with several items, several lots or a chapter is still unobserved,
and the mapping is what gates the correctness of every payload.

## 6b. Place SECOP's clock (optional, one audit row)

```bash
.venv/bin/python -m secop_firstbid clock-probe --config run.json
```

Sends one `SetStartBiddingAudit`, reads its row back from the event log and prints how far SECOP's
application clock is from yours, as an interval:

```
SECOP clock    945.0 ms behind this host (feasible -960.0 .. -930.0 ms, 1 candidate row(s))
```

Run it from the host that will bid, a few minutes before the auction. It writes **one** buyer-visible
"Comenzar Lances" row and no bid. Then either:

- set `apply_clock_offset=true` and `clock_fallback_shift_ms` to the value `clock-probe` printed. The
  run measures again from its own start audit and moves `T` by that; if it cannot, it moves `T` by
  the fallback instead. One probe serves every account bidding from the same host; or
- leave it off and add the figure to `schedule_offsets_ms` by hand (945 ms behind → `[900, 950, 1000]`
  brackets SECOP's `T`).

Running several accounts of one company? Give each a different `start_audit_lead_ms`, at least
`2 × clock_window_ms + 1000` ms apart (60000, 71000, 82000 at the defaults). The event log names the
company, not the account, so audits sent together cannot be told from one another.

## 7. Dry run — understand what it costs

```bash
.venv/bin/python -m secop_firstbid run --config run.json --set dry_run=true
```

Exercises the whole pipeline including the schedule and the threading, but **never sends
`CreateBid`**.

It is not free. Every `ValidateBid` writes a buyer-visible `Logs_Search_NewBid` row, labelled as a
failed bid submission, carrying your public IP and company code, and the run's one
`SetStartBiddingAudit` writes a row beside them. If you want a genuinely zero-footprint rehearsal,
that's `plan`.

Prefer a throwaway or practice auction for this.

## 8. Live

```bash
.venv/bin/python -m secop_firstbid run --config run.json --yes-i-mean-it
```

Without `--yes-i-mean-it`, `confirm_live` stays false and no real bid is sent. That friction is
deliberate; don't put `confirm_live` in the config file.

In order, the run: warms one connection per bid at arming; sends its one start audit
`start_audit_lead_ms` before the first release, on a separate connection, and reads the row back to
place SECOP's clock; re-reads every bid connection at `keepalive_offsets_ms`; moves `T` onto SECOP's
clock if you asked and the measurement allows; issues the single state gate at `T − gate_lead_ms`;
releases each wave on the monotonic clock; waits `secop_record_delay_s` and reads SECOP's own record
of the auction; and writes everything to `runs/<timestamp>_<id>/`.

**Arm at least 90 s before `T`.** Later than that and the start audit and the first keep-alive are
skipped or squeezed, and `T` will usually not be moved.

**Send few bids.** One host close to SECOP, one or two accounts, three to five offsets bracketing
SECOP's `T`. On 2026-11-20, 42 bids from two hosts reached the server no sooner than 13 would have;
the connections beyond the first eight or nine per host were refused.

**Keep a person on the auction page.** Its countdown follows SECOP's clock, and a human with the page
open is the fallback if the run reports `NO_VERDICT`.

## Reading the result

Start with `summary.json`, then `run.log`, then the raw `events.jsonl`.

| `outcome` | Means |
|---|---|
| `BID_PLACED` | Some wave succeeded. Other waves tagging `STATE_NOT_STARTED` or `DUPLICATE` is designed behaviour, not failure. |
| `ALL_REJECTED` | Bids were sent, at least one was answered, and every answer was a bounce. Read `failure_taxonomy`. |
| `NO_VERDICT` | Bids were sent and **none was answered** — every one timed out or failed to connect. Not a rejection: the server may still have processed them. Read the `SECOP says` lines. |
| `NOT_FIRED` | Nothing was sent — gate aborted, dry run, or a config problem. |
| `ERROR` | Something failed before or during firing. |

Common tags:

- **`BID_ERR_MIN`** — the server rejected the value. On 2026-11-20 it was the answer to every bid that
  arrived after another company's had been accepted; `summary.secop_record.bids` says whether that is
  what happened, whose bid it was and when. It is not the only cause: in the capture it came back on
  an auction with no bids at all, and a bid at *exactly* `minBid` was rejected identically, so `minBid`
  is not simply a floor.
- **`NETWORK`** on every attempt — nothing was answered. `rtt_ms` near the connect timeout means the
  bid never left; near the read timeout means it left and the answer never came. Only the second kind
  can have been processed, and `summary.secop_record.own_bid_rows` counts the ones that were.
- **`STATE_NOT_STARTED`** — expected on a pre-`T` wave. Also: nobody has ever observed this response,
  so if you get one, write it down — it settles
  [Q4](open-questions.md#q4--does-a-pre-open-createbid-get-a-clean-rejection-or-trip-something).
- **`DUPLICATE`** — a post-win bounce. Expected.
- **`AUTH_EXPIRED`** — the token died. Re-harvest; nothing is retried.
- **`WAF_BLOCK` / `RATE_LIMIT`** — stop. Nobody has measured where the threshold is; do not retry into it.
- **`UNKNOWN`** with a 404 — likely the unverified `bid/CreateBid` route casing.
  `unverified_routes` in the summary tells you which routes the run used that have never been observed.

`capture.har` opens in Chrome devtools and any HAR viewer. That is usually the fastest way to see what
actually went over the wire, and it is the same format the whole project was reverse-engineered from.

## After a live open, read what SECOP recorded

The summary prints it, from the bid list and event log the run reads back:

```
  SECOP clock 945.0 ms behind this host, a 30.0 ms-wide interval over 1 candidate row(s); T not moved
  SECOP says  bid 1: 0.000000000000 by company 738… (not yours) at 2026-11-20T21:00:00.0443673Z, +44.4 ms from startDate
              14 of your company's bids were processed, the first +11.58 s and the last +35.28 s from startDate
```

Three numbers calibrate the next run, all on SECOP's own clock and all in `summary.secop_record`:

- **the accepted bid's distance from `startDate`** — how soon after the opening the winner got in;
- **your first processed bid's distance from `startDate`** — seconds, not milliseconds, means the
  server was saturated and your requests queued, whatever instant they left at;
- **`summary.clock.event_log`** — where SECOP's clock sat against yours.

Keep the run directory. `history/2026-11-20-audit.md` is what two such runs added up to.

## Troubleshooting

**`PARSE_FAILED`** — almost always the wrong save method. You need a DOM serialisation, not
view-source. Confirm the file contains `RedirectToEAuction` (opportunity page) or `iframeEauction`
(launch page).

**`PARSE_AMBIGUOUS`** — several auctions on the page. Pass `auction_id`. Correct behaviour, not a bug.

**`CONFIG_INVALID` on `bid_value`** — you wrote something like `1.234`, which is 1234 in Colombian
format and 1.234 on the wire with no way to decide. Write `1234` or `1.2340`.

**`FIRE_TIME_TOO_FAR`** — usually the wrong day. Also a real guard: a run armed for hours outlives the
24-hour token.

**`CLOCK_SYNC_FAILED`** — the measured clock uncertainty is wider than your schedule span. Either sync
the host, widen the schedule, or accept that the spread is not doing what you think it is.

**"T was NOT moved"** — `apply_clock_offset` was on and the run declined. The message says why: the
start audit's row was not read back in time (arm earlier, or raise `start_audit_lead_ms`), the interval
was too wide (the server was slow to answer the audit, or several rows of your company fell in the
window — stagger the leads), or the audit itself failed. The schedule fired on `fire_time` as given.

**403 on everything** — check the User-Agent. A library default UA is believed to draw a 403, though
that is asserted from outside this capture rather than observed.

**Everything hangs** — shouldn't happen; every call carries a timeout. If it does, the timeouts have
been removed from `session.py`, and one hung socket in a barrier-synchronised burst will hang the run
past the auction window.
