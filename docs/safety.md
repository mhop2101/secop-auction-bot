# Safety, footprint, and what this app costs you

Read this before the first live run. Most of it is not about the code failing — it is about the code
working exactly as designed and leaving traces you didn't expect.

## You are not anonymous

Every bid attempt writes a row into a **server-side audit log the auction owner can read**, carrying
your **public IP address** and your **company code**. This is observed, not theorised:
`audit/GetEventLogs` in the capture returns rows like

```
Logs_Search_NewBid       2026-07-28T21:19:31.4447569Z  ip 203.0.113.10  userCode 900000000
  "Error cuando presentado el lance. Problema: El valor total tiene que ser inferior…"
Logs_Search_StartBidding 2026-07-28T21:19:31.3257602Z  ip 203.0.113.10  userCode 900000000
```

`900000000` is the JWT's `LoggedInCompanyCode`. There is a direct, permanent link from every attempt to
your organisation.

## `dry_run` is not dry

This is the finding most likely to surprise you.

`grep -c CreateBid` over the whole 22 MB capture is **0** — no bid was ever submitted. Yet the audit log
contains rows explicitly labelled **failed bid submissions**. They can only have come from
`ValidateBid` and `SetStartBiddingAudit`.

> A `dry_run`, or a "probe-only" rehearsal with `n_validate_only=1`, writes rows describing failed bids
> under your IP and company code into a log the buyer can read. It is not footprint-free.

`config.validate()` emits this as a warning on every dry run. What `dry_run` actually guarantees is
narrower than the name suggests: **it never sends `CreateBid`.** That is worth having — it is the
difference between a rehearsal and a bid — but it is not invisibility.

If you want a genuinely free rehearsal, use `plan`, which does everything offline: parses the page,
builds the waves, builds the payload, prints the footprint, and touches the network zero times.

## Count the footprint before you fire

The metric that matters is not submissions, it is **audited requests** — and it counts *requests*, not
*threads*, because a validate-then-submit thread makes more than one:

```
audited_requests = Σ waves × [ direct×1              # CreateBid
                             + validate×2            # ValidateBid AND CreateBid
                             + validate_only×1 ]     # ValidateBid
                 + (1 if a start audit is planned else 0)   # one for the whole run
                 + (1 if do_submit_audit else 0)
```

`config.audit_footprint()` computes it and `plan` prints it. Three things people get wrong:

- **A validate-then-submit thread costs two bid-endpoint rows, not one.** It sends `ValidateBid` and
  then `CreateBid`, and both are logged.
- **Validate-only probe threads count.** They write the same `Logs_Search_NewBid` rows as a real bid.
- **The start audit is one row per run, not one per bid.** It used to be one per thread, which doubled
  the footprint and — because each shared its bid's connection — delayed bids by seconds when the
  server stalled. A run that sends nothing (a dry run of D threads) writes none.

So a `burst` with `bid_count=4, wave_direct=1` writes **5** rows; add a validate thread per wave and it
is **13**. Configurations above 10 audited requests warn.

`clock-probe` writes exactly one row of its own: the start audit it reads back.

**More is not better.** On 2026-11-20 the first auction was run with 42 bids and 42 start audits from
two hosts. SECOP refused or timed out a third to half of the connections, the audits that got through
delayed the bids behind them, and none of it reached the server sooner. Fewer, well-placed bids from
one host cost less in rows, load the platform less at the moment every bidder needs it, and are easier
to read afterwards.

The number `plan` and the summary report is a **projection** of what the configuration would send. What
a run actually sent is `submissions_sent` and the attempt rows. Under `dry_run` the projection drops
the `CreateBid` terms — but not the `ValidateBid` or audit terms, which is the arithmetic behind
"`dry_run` is not dry" above.

## `stop_on_success` will not save you

It cancels waves that have not yet been released — but at the recommended schedule densities it can
never fire.

The fastest bid-endpoint response anywhere in the capture is 168.5 ms (median 614.4, max 1633.1). A
`burst` with `pre_ms=60, interval_ms=30` spans **90 ms end to end**. Wave 0's verdict cannot possibly
return before wave 3 has already been sent. **Zero waves get dropped.** The same holds for the "wide
net" custom shape at anything near median RTT.

`bidder.py` logs this at INFO rather than letting the counter imply a saving that isn't there. If you
want `stop_on_success` to actually do something, your wave span has to exceed the observed p50 bid RTT
— hundreds of milliseconds, not tens.

## Keep the thread count small

Start at `D + V + P` between 2 and 5.

A concurrent burst is outside anything ever observed — the capture shows sequential ~3 s polling and
nothing else. Large N risks a WAF 403 or a rate limit, and writes N rows to the audit log. Nobody has
measured where the threshold is; see
[Q10](open-questions.md#q10--where-is-the-rate-limit-and-does-the-api-host-sniff-user-agent).

Whether the threads buy you anything at all is itself unresolved: if the server does not tiebreak by
arrival time ([Q1](open-questions.md#q1--does-the-server-break-ties-by-submission-time)), racing buys
only resilience against a dropped request.

## Credentials

**The HAR is a credential-bearing artifact.** `www.secop.gov.co.har` contains a live-shaped bearer
token in plaintext — not in a header (those were stripped by the exporter) but as the `token=` **query
parameter** on all 281 SignalR URLs, together with the operator's name, user and company codes and
`LoginGUID`. Anything that logs a URL leaks a 24-hour credential.

That is why `logging_setup.redact_url()` rewrites query parameters and not just header dictionaries,
and why `tests/test_redaction.py` covers a realistic SignalR URL specifically.

Both HTML pages and the HAR are in `.gitignore`. So is `runs/`, which contains full request and
response bodies plus your bid values.

The app never logs the raw token. It renders it as `first6…last6`, rewrites `Authorization` to
`Bearer <redacted>`, and keeps cookie names while blanking their values. `TokenInfo` has an explicit
`__repr__` so the token cannot leak through a stack trace or a debugger.

**Do not paste your JWT into a config file you might commit.** Prefer the environment or a file outside
the repo.

## Failure modes that are not failures

Some outcomes look alarming in the logs and are the design working:

- **`STATE_NOT_STARTED` on a pre-`T` wave.** That is what a wave fired before the open is *for*.
  (Though nobody has ever observed one — see
  [Q4](open-questions.md#q4--does-a-pre-open-createbid-get-a-clean-rejection-or-trip-something).)
- **`DUPLICATE` after a win.** Later identical bids bounce because they are not lower. Expected.
- **`ReadyToStart` from the state gate.** That is the correct pre-open reading, not a problem.

A run is `BID_PLACED` if **any** wave succeeded, regardless of how many others bounced. `ALL_REJECTED`
and `ERROR` mean nothing landed.

`NO_VERDICT` means **the run does not know**. Every submission ended in a timeout or a connection
error, so nothing was answered. That is not a rejection: a request that timed out may still be
processed, and on 2026-11-20 every bid that had left the machine was, 4 to 35 s after the opening.
Read the `SECOP says` lines of the summary — the bid list and your company's rows in the event log,
read back after the schedule — before concluding anything from a `NO_VERDICT` run.

Note that `DUPLICATE` and a genuine `BidErrMin` rejection are **byte-identical on the wire** — same
status, same body. Only local run ordering separates them, which is why `errors.classify()` takes a
`RunState`. If you refactor that parameter away, the taxonomy silently becomes wrong.

## The value is sent as-is

The app never computes a "lowest legal" bid, never enforces a floor, and never retries with a different
number. Your value goes into the payload and onto the wire unchanged.

The server can and does reject on its own. In the capture, bids of 1, 10, 200 and 7813974.32 were all
rejected with `BidErrMin` while `GetBidsTotalInfo` showed `myLastBid: 0`. The fourth value is the
interesting one: **7813974.32 was exactly the `minBid` the server had been returning**, and it was
rejected identically. Whatever `minBid` means, it is not simply a floor you clear.

This page used to explain those rejections as "the company had no standing bid to decrement from".
That did not survive 2026-11-20: in each of two live auctions the one accepted bid was a **zero** from
a company with no standing bid, and every later zero — ours — was answered `BidErrMin`. So
`myLastBid: 0` predicts nothing, and in those auctions the rejection followed from the bid already
standing. That is not a general account of `BidErrMin`: the eight rejections in the capture happened
on an auction with **no bids at all** (`auctionTotalBids: 0`) and remain unexplained. `lastBid` does
not help either: it reads `0` both when nobody has bid and when the standing bid is zero.

The app's only job here is to record `errorType`, `errorMessage` and `minBid` verbatim so you can see
why. Nothing is retried or altered.

## Before every live run

- [ ] Host clock synced — `sudo sntp -sS time.apple.com` or `chronyc tracking`.
- [ ] JWT freshly harvested; `preflight` shows comfortably more than `auth_safety_margin_s` remaining.
- [ ] `plan` run, and the printed `audited_requests` is a number you are willing to have logged
      against your company.
- [ ] `fire_time` carries an explicit offset, is the right **day**, and matches `startDate` (the app
      warns but will honour a mismatch — it is not going to stop you).
- [ ] You know that `--yes-i-mean-it` is what makes `CreateBid` real, and you have not set it by habit.
- [ ] The auction is one you are actually entitled to bid on.

## Scope

This app automates a bid you are entitled to place, on an auction you are registered for, using
credentials you obtained by logging in yourself. It does not obtain credentials, evade access
controls, or defeat any protection mechanism.

Two of its design choices exist to keep it that way: `fire_time` is manual so the operator remains the
decision-maker, and `confirm_live` is off by default so a real submission is always a deliberate act.
Keep both.
