# AGENTS.md — working on secop_firstbid

Instructions for coding agents (and humans) working in this repository.

## What this is

A single-shot, scheduled, multi-threaded **first-bid submitter** for SECOP II electronic reverse
auctions (`subastas.secop.gov.co`). Given a manually harvested JWT, a saved auction page, one bid
value and one operator-specified fire instant, it places **one logical bid** at that instant and stops.

Read `README.md` for the shape of it, then `docs/evidence.md` before changing anything. That page is
not background reading — it is the constraint list.

## The one thing to understand first

This codebase was specified twice. The first pass (`SECOP_FIRSTBID_PLAN.md`, since revised) was
audited against the raw artifacts (`PLAN_AUDIT.md`), and roughly half of it turned out not to run. The
audit's summary of the pattern is the most useful thing in the repository:

> Everything derived by reading bytes was byte-accurate. Everything derived by *reasoning over* the
> capture is where the defects were.

Three examples, all of which are now defended by tests:

- The HTML parser looked correct and matched **zero bytes** on the only page available, because the
  `onclick` is a JavaScript string-concatenation expression, not a URL.
- The clock design took a median of `Date − now`, which is *biased* by about half a second because the
  header is whole-second — so every wave of a `T−60 … T+30 ms` schedule would have landed at roughly
  `T+390 … T+480 ms`.
- `dry_run` was described as safe, and writes rows labelled "failed bid submission" into a
  buyer-visible audit log under the operator's public IP.

**So: when you touch this code, state the procedure and show it running against a fixture.** "I can see
the value in the data" and "I know how to obtain the value" are different claims. Most of the defects
above would have died in ten minutes at a REPL.

## Environment

- **Python 3.9.6 is the only interpreter available.** `from __future__ import annotations` at the top
  of every module. Use `typing.Optional` / `List` / `Dict` / `Tuple`. Never `X | None`, `list[str]`,
  `match`, or `tomllib`.
- **Runtime dependency: `requests` only.** No PyJWT (the JWT is decoded by hand — we read claims, we
  never verify signatures), no dateutil, no bs4/lxml, no structlog. `pytest` is test-only.
- Everything runs from the project venv:

```bash
.venv/bin/python -m pytest -q
```

## Non-negotiable invariants

Break any of these and the app silently bids wrong. Each is covered by a test — if you find yourself
editing the test to make a change pass, stop.

**Money is `decimal.Decimal`, never `float`.** Context precision 50.

**Never `.normalize()` or strip trailing zeros on a wire value.** Decimal scale *adds* on multiply and
that is exactly what reproduces the wire: `Decimal("1.000000") × Decimal("10")` → `"10.000000"`, and
`× Decimal("7813974.32")` → `"7813974.32000000"`.

**`numbers.to_wire()` is the only place a Decimal becomes a wire string, and it uses
`format(d, "f")` — not `str(d)`.** `str` switches to scientific notation once the adjusted exponent
drops below −6, which the 12-decimal-place `itemQuantityUnitPrice` reaches: the documented zero case
renders as `"0E-12"` and a small ratio as `"1.000E-9"`. Every amount in the capture is plain fixed
point, and an exponent form has never been sent to this server. Don't add a second wire-formatting
helper elsewhere "to be safe" — two functions claiming to own the wire format is how they drift.

**The wire key order is fixed.** Top level: `auctionId, currency, Date, ranking, items, totalValue,
languageCode`. Per item: `itemId, quantity, isPriceListItem, isChanged, itemInternalReference,
lotInternalId, lotName, itemUnit, totalValue, bidValue, itemQuantityUnitPrice, bidded`. Note
`totalValue` precedes `bidValue`. Do not sort keys.

**`ranking` is a JSON number (`-1`); every money and quantity field is a JSON string.**

**The double slash in `/eauctions.api/v1//` is real** (146/146 requests). Do not normalise it. Slash
counts differ per SignalR endpoint too — also don't normalise those.

**Path casing is load-bearing**: `Bid/GetBidsTotalInfo` has a capital B while `bid/CreateBid` does not.

**`Date` is JavaScript `toISOString()`**: exactly 3 fractional digits and a `Z`. Python's `isoformat()`
gives 6 digits and `+00:00`, which is wrong. Use `payload.js_iso_now()`.

**No bare-GUID fallback in the page parser, ever.** `beforebid.html` holds 75 GUID occurrences across
25 distinct GUIDs; the target is 72nd in document order and tied for least frequent. Both "first GUID"
and "most frequent GUID" select a context-menu id. A bare-GUID fallback does not degrade gracefully —
it silently bids on the wrong auction. Likewise never relax the `CO1\.EAREQ\.` anchor: 20 of the 21
`docUniqueIdentifier` hits are the parent opportunity dossier.

**No function may return a bare median as "the clock offset."** `tests/test_clock.py` asserts this.
Use `clock.marzullo()` and report the residual width alongside the midpoint.

**Nothing may share a thread's connection with its bid at the release instant.** Each fire thread owns
one `HttpClient`, which owns one pooled connection, so any other request that thread makes is *serial*
with its submission — the bid cannot leave until the earlier response returns **or times out**.
`SetStartBiddingAudit` taught this twice. Issued inline before the submission, it pushed every bid a
full round trip late. Moved `start_audit_lead_ms` ahead of each thread's release, it worked while the
server answered — and on 2026-11-20, with the server refusing or stalling connections before two
openings, an audit that hung for its whole timeout held its thread's bid back by up to ten seconds
(5–16 bids per run more than 0.5 s late). **A lead is not isolation.** So:

- A fire thread sends nothing but its bid (and, for V/P, the `ValidateBid` its release positions).
- The start audit is **one request per run**, sent by `bidder.StartAudit` on a client no fire thread
  uses. `tests/test_bidder.py` pins that a stalled audit costs no bid its instant.
- A keep-alive (`session.keep_warm`) *is* on a bid's connection, deliberately. It is allowed there only
  because it carries its own short timeout and `config.validate` refuses a config whose last keep-alive
  could still be in flight at the first release (`keepalive_quiet_ms`). Do not loosen either half.
- If you add a request, it goes on another client. "Earlier on the same connection" has already failed.

**A run that will refuse to submit refuses before it sends anything.** `require_live_confirmation` is
called ahead of the start audit and the keep-alives, not only inside `run_schedule`. An unconfirmed run
that wrote an audit row on its way to refusing would be a footprint nobody asked for.

**A timeout is not a rejection.** `summary.outcome` is `NO_VERDICT`, not `ALL_REJECTED`, when no
submission was answered. The server processed every bid of ours that left the machine on 2026-11-20,
4 to 35 s later; "rejected" for those runs reported an answer nobody received. What the server did is
read back by `records.read_result` and reported beside `outcome`, never folded into it.

**`errors.classify()` must keep its `run_state` parameter.** A post-win duplicate and a genuine
`BidErrMin` rejection are byte-identical on the wire. Local send ordering is the only disambiguator; a
refactor that drops the parameter silently corrupts the failure taxonomy.

**The bid value is sent exactly as supplied.** No floor, no adjustment, no retry with a different
number. The server rejects on its own and we log why.

**`redact()` covers URL query parameters, not just headers.** The bearer token rides as a plaintext
`token=` query parameter on SignalR URLs. A header-only redactor leaks a live 24-hour credential into
`events.jsonl`, `http/*.json` and `capture.har`.

## Module map

| Module | Responsibility |
|---|---|
| `errors.py` | `Failure` taxonomy, `SecopError`, the single response classifier |
| `models.py` | Dataclasses; `BidItem`/`BidPayload` own the wire serialization |
| `endpoints.py` | Exact paths and header sets; `OBSERVED_ON_WIRE` |
| `numbers.py` | Decimal parsing/formatting and the two payload formulas |
| `config.py` | `Config`, loading, validation, `audit_footprint` |
| `page_parse.py` | Stage 1 — extract auction identifiers from a saved page |
| `auth.py` | JWT claim decoding (no signature check), preflight |
| `session.py` | Per-thread `HttpClient`s sharing one cookie jar; timeouts; pre-warm; keep-alive |
| `detail.py` | Stage 2 — the three GETs → `AuctionDetail` |
| `payload.py` | Stage 3 — build the payload, check the invariant |
| `schedule.py` | Stage 4a — `fire_time` → `T_mono`, waves, monotonic release; `shift_t` |
| `clock.py` | Stage 4b — Marzullo and event-log-stamp arithmetic. Never moves `T` itself |
| `gate.py` | Stage 4c — the single pre-`T` state read |
| `bidder.py` | Stages 5–6 — thread roles, barriers, first-success; the run's one `StartAudit` |
| `records.py` | SECOP's own record, read back: the clock from the audit's stamp, the result |
| `logging_setup.py` | Run dir, JSON events, redaction, single writer thread |
| `capture.py` | `capture.har` and `timeline.jsonl` writers |
| `report.py` | `summary.json` and the human summary |
| `__main__.py` | CLI: `parse-page`, `preflight`, `clock-probe`, `plan`, `run` |

## Conventions

- Module docstring names the plan section it implements.
- Type-annotate public functions.
- **Comments only where the code cannot show a constraint.** `# wire order, do not sort` is good.
  `# Step 3: build the payload` is noise. Never write a comment referencing this task, a review, an
  audit finding number, or "as requested" — state the constraint itself so it still reads correctly to
  someone who arrives in a year.
- Raise `SecopError` with a `Failure` code. Never `assert` for control flow.
- Where a fact is unverified, **say so in the docstring** and link it to the relevant entry in
  `docs/open-questions.md`. Marking uncertainty is a feature of this codebase, not clutter — every
  place it was dropped is a place a defect appeared.

## Testing

Unit tests must pass **offline** with zero network calls. Fixtures under `tests/fixtures/` are real HAR
data with the token scrubbed; `tests/conftest.py` exposes them all. Do not re-read
`www.secop.gov.co.har` from a test — it is 22 MB and credential-bearing.

`beforebid.html` and `bid.html` stay at the repository root, byte-identical. The parser's whitespace
handling is calibrated against their DOM-serialised form (one attribute per line), which is why every
pattern uses `\s*\+\s*` and never a literal `' + '`. **Never edit them.**

Anything named `*_synthetic.json` is invented, not captured. It must carry a `"_synthetic": true` key,
and the test using it must say what is unverified. `getauction_synthetic.json` is shaped on the real
`getauction.json` but its second item, chapter entry and quantitative item are made up; it exists to
reach paths one single-item auction cannot.

**`tests/fixtures/getauction.json` is the real response** and it is what settled the item mapping —
the GUID is `id` (there is no `itemName` on the wire) and the lot id is the item's `biddingListId`
(the entry's `lotInternalId` is the marker `ALLLOTS`). Do not "restore" the plan's §6.2 names; they
describe the SPA's flattened model, not the server's JSON.

## Things you must not do

- **Do not commit** `*.har`, `beforebid.html`, `bid.html`, or `runs/`. They carry credentials, session
  state, and your bid values. They are in `.gitignore`; keep them there.
- **Do not make a live `CreateBid` easier to fire.** `confirm_live` defaults false and is set by an
  explicit `--yes-i-mean-it`. That friction is deliberate.
- **Do not add a retry.** A retry at the fire instant lands at an unknown time and writes another
  buyer-visible audit row.
- **Do not "fix" `apply_clock_offset` to default true.** SECOP has more than one clock. The HTTP
  `Date` header tracks real time; the clock the application stamps its event log and bids with read
  0.9–1.0 s behind it on 2026-11-20, and the accepted bids were stamped 32 and 44 ms after `startDate`
  on that second clock. That is two auctions on one day. Whether the offset is stable is unknown
  (`docs/open-questions.md`), so moving `T` stays an explicit operator decision, and the code that does
  it (`__main__._place_t_on_secop_clock`) must keep declining when the measurement is missing, wide or late.
- **Do not put a request back on a fire thread**, and do not turn the start audit back into one per
  thread. Besides the delay, one audit per run is what makes its row identifiable in the event log:
  with one per thread, fifteen rows of the same company fell inside one request's window and the clock
  could not be read from any of them (`tests/test_records.py` pins this against the real log).
- **Do not implement SignalR** without first reversing the handshake out of `jquery.signalR` 2.x. The
  `negotiate` call was never captured and `connectionToken` cannot be derived from anything here.
- **Do not delete the tests that document unknowns** — the `quantity != 1` divergence test, the
  naive-regex-returns-`[]` test, the median-vs-Marzullo test. They look redundant and each one marks a
  real unresolved question.

## Before you claim something works

The capture contains **no successful bid, no auction open, no pre-open request, no concurrency in
bidding, no 403, no rate limit, and no `CreateBid` at all.** If your change depends on any of those
behaving a particular way, it is an assumption. Say so, add it to `docs/open-questions.md`, and make
the code log enough for the first live run to settle it.

Two live auctions on 2026-11-20 added a little, and it is written up in `history/2026-11-20-audit.md`:
the server **processes** `CreateBid` requests built by this app (each left a row in the event log); a
bid of **zero** is accepted (it was the one accepted bid in both, from other companies); a company's
first bid is not rejected for having `myLastBid: 0`; and the event-log clock is not the `Date` clock.

What is **still** unobserved: any response to a `CreateBid` — every one of ours timed out — and so what
a win looks like on the wire; whether a bid arriving before the opening is refused or held; how the
server orders work that arrives together; whether the start audit is needed at all. The reasoning that
was wrong before those auctions was wrong in the usual way: "`myLastBid` is 0, so a zero will be
rejected" and "the *Subasta en curso* row marks the opening" were both read off the data rather than
checked against an outcome.
