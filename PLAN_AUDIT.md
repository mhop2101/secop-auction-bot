# Audit — SECOP_FIRSTBID_PLAN.md vs. beforebid.html, bid.html, www.secop.gov.co.har

Method: 6 parallel auditors (HTML parsing, wire protocol, numbers/payload, timing/concurrency,
provenance, buildability), each dimension's findings then adversarially re-verified by an
independent agent instructed to refute them, then synthesised. Refuted findings were dropped;
the "explicitly dropped" list at the end records what was checked and found NOT to be a problem.

I've read the plan end-to-end and independently re-derived the top findings from the raw files. Here is the synthesis.

---

# Is it solid?

**Half of it is excellent and half of it will not run. The half that's excellent is the half you derived by reading bytes; the half that fails is the half you derived by reasoning.**

Everything in this plan that came from staring at the HAR — the payload field set, the Decimal scale rules, the 12-dp `itemQuantityUnitPrice`, the double slash, the `Bid` capital-B, the response shapes, Appendix A — is byte-accurate. I re-verified it and found essentially nothing wrong. That work is trustworthy as-is.

Everything that came from *inference over* the capture — Stage 1's HTML parser, the clock-sync design, the concurrency mechanics, the safety accounting — is where the defects are, and they are not cosmetic. Two examples that decide the verdict: **§5's parser returns `None` on the only page you have** (I ran it verbatim), and **§9.1's default clock is quantized ~25× coarser than the schedule §11.4 builds on top of it** — the app would fire ~0.45 s late while logging a precise-looking `actual_offset_ms`. The first fails loudly at milestone 5. The second never fails at all; it just misses.

There is also one finding with consequences outside the codebase: **`dry_run` is not dry.** Six auditors and their adversarial verifiers converged on ~30 surviving defects, but the pattern is what matters more than the count — see *Structural issues* below.

**Verdict: do not start building from §5, §9, §11 as written. §1, §6.1, §8, §10 you can code against today.**

---

## Ranked findings

### 1. The default clock source cannot deliver the precision the entire schedule assumes — CRITICAL
*Independently found by four dimensions (timing, provenance, structure, and again by an adversarial verifier).*

**Plan says** (§9.1 L399-401): "Default (`date_header`, **recommended**)… `offset = server_time − local_time`… Re-measure a few times, take the median, and account for `RTT/2`." Consumed by §9.2 L412's "busy-wait for the final few ms" and §11.4 L551's `pre_ms=60, interval_ms=30`.

**Actually true:** HTTP `Date` is whole-second (RFC 7231). `Date − local` is therefore a *biased* estimator with expected error ≈ −0.5 s, not a noisy centered one. **A median converges to a bias, it does not remove it.** `RTT/2` corrects ~68 ms of a ~500 ms error.

**Evidence** (my own re-derivation, all 429 entries):
- 429/429 responses carry `Date`; **0 have a fractional part** (`Tue, 28 Jul 2026 21:07:40 GMT`).
- Plan's prescribed estimator over the 128 `GetAuctionSimple`: min −1.223 / **median −0.458** / max −0.023 s. With `RTT/2`: median −0.391 s.
- Rigorous Marzullo intersection from the *same* Date data: **offset ∈ [−0.016, +0.114] s**. So the prescribed estimator lands ~0.45 s from what its own source supports.
- Sign check against §9.2 L411 (`fire_at_local = fire_at_server − offset − lead_ms`): an offset 0.45 s too negative fires **0.45 s late**. Your `T−60/−30/0/+30` schedule actually lands ≈ `T+390…T+480` ms. **No wave lands before T** — the entire §11.4 L529-533 pre-T insurance rationale is nullified.
- Even under *ideal* uniform-phase sampling (simulated with the observed RTT distribution), residual width is 687 ms at k=3, 315 ms at k=10, 184 ms at k=30 — all larger than your 90 ms schedule span. "A few times" cannot reach it at any practical k.

**Compounding:** the `Date` clock and the SignalR hub clock are two *different* server clocks whose feasible intervals **do not intersect** — Date `[−0.016, +0.114]` vs hub median **−0.547 s** (128 `ReceiveClock` samples). §4 L156 and §9.1 present them as two precisions of one measurement. Neither is tied to the auction state machine by any evidence.

**Fix:** (a) State plainly that `date_header` is second-granular. (b) Replace `median(Date − now)` with Marzullo interval intersection — each sample gives `offset ∈ [D − t_recv, D + 1 − t_send]`; intersect, take the midpoint, **log the residual width as the honest error bar**, and refuse to run any schedule whose span is narrower than that width (`CLOCK_SYNC_FAILED`). (c) Do *not* simply flip the default to `signalr` — phase analysis shows the Date data is consistent with true offset ≈ 0, making the hub the outlier. Measure both, log the delta, abort on disagreement > schedule span.

---

### 2. §5's parser matches zero bytes on the only page you have — HIGH
*Independently found by four dimensions.*

**Plan says** (§5 L191-193): "locate the anchor whose `onclick` contains `RedirectToEAuction` and `externalId=`, then `urllib.parse` the params out of the JS string. Fall back to a global regex for `externalId=([0-9a-f-]{36})`…"

**Actually true:** the onclick is not a URL — it's a JS string-concatenation expression. `parse_qs` applies `unquote_plus`, so every `+` operator decodes to a space and every key comes back wrapped in quote junk.

**Evidence** (I implemented §5 verbatim and ran it against `beforebid.html:3822`):
```
§5 parse_qs keys: ["'   'docUniqueIdentifier", "'   'market", "'   'externalId", 'mkey']
params.get('externalId') -> None
```
`auction_id` is `None`; §5 L196's "validate `auction_id` is a GUID" then fails → `PARSE_FAILED`. The fallbacks fail identically: `re.findall(r'externalId=([0-9a-f-]{36})', …)` → `[]` on raw bytes, on `html.unescape`d text, and on `bid.html`. The two failure modes are perfectly correlated — the safety net has the same root cause as the thing it's catching.

**The dangerous repair:** loosening to a bare-GUID scan. `beforebid.html` has **75 GUID occurrences across 25 distinct GUIDs**; the target is the 72nd in document order and appears **twice — tied for least frequent**. Both "first GUID" and "most frequent GUID" pick a context-menu id (`e93d758e-…`, `0737f471-…` ×7).

**Fix** (verified working): de-concatenate first — `joined = re.sub(r"'\s*\+\s*'", "", onclick)`, slice between `'?'` and `',true);'`, then `parse_qs` →
```
{'docUniqueIdentifier': ['CO1.EAREQ.937510'], 'market': ['GovCo'],
 'externalId': ['d6a49a23-3556-4b3d-9bb4-3e209279b9f8'], 'mkey': ['5f1742a6_…']}
```
Concatenation-aware fallbacks: `externalId'\s*\+\s*'([0-9a-fA-F-]{36})` (yields the correct GUID twice). **Explicitly forbid a bare-GUID fallback and cite the 25-distinct-GUID census as the reason.** Also: name the required page unambiguously (`<title>Oportunidades</title>`, `beforebid.html` — `bid.html` is `Integración` and contains **0** of §5's tokens), and add `iframe#iframeEauction[src*='eauctions.site']` as a first-class extractor — `bid.html:3315` yields `auctionId`/`languageCode`/`marketCode` from a real query string with plain `urlparse`. Name the fixture file in §13 L700 and §14 L730.

Two demoted sub-items folded in: the anchor selector does match **two** anchors (`RedirectToEAuctionIFrame` contains `RedirectToEAuction`), but both carry the same `externalId`, so on any evidenced page the only loss is `eauction_doc_id` — which nothing consumes. The real gap is the **multi-row case**: `incWidgetAuctiongrdEAuctionGrid_tr0` / `…_0` is an indexed "My Auctions" widget, multi-row by construction, with no rule for which row is "the" auction. Refuse to guess; list candidates.

---

### 3. `dry_run` is not dry — `ValidateBid` writes "failed bid" rows into the buyer-visible audit log — HIGH
*Independently found by provenance and wire-protocol.*

**Plan says** (§4 L154): `dry_run` = "Do everything **except** `CreateBid` (validate + log only)". §14 step 11: "**Dry-run rehearsal** on a live auction with `D=0,V=0,P=1` (probe only)… **before** ever firing a real `CreateBid`." §11.4 L544: "total submissions = `Σ waves × (direct + validate)`."

**Actually true:** `grep -c CreateBid` on the 22 MB HAR = **0**. Yet `GetEventLogs` (21:23:05.029Z, `recordsTotal: 23`) returns:
```
Logs_Search_NewBid       2026-07-28T21:19:31.4447569Z  ip 203.0.113.10  userCode 900000000
  "Error cuando presentado el lance. Problema: El valor total tiene que ser inferior…"
Logs_Search_StartBidding 2026-07-28T21:19:31.3257602Z  ip 203.0.113.10  userCode 900000000  "Comenzar Lances"
```
Those rows can only come from `ValidateBid` + `SetStartBiddingAudit`. So a "safe probe-only rehearsal on a live auction" writes rows **labelled as failed bid submissions**, under the operator's public IP and company code, into a permanent event log the auction owner can read.

**Also:** the footprint formula excludes validate-only (P) threads entirely, and `do_start_audit` (default **true**) adds one row per attempt. A `burst, bid_count=4, wave_direct=1` config the operator believes writes 4 rows actually writes **8** (4 CreateBid + 4 StartBidding), up to 12 with validate threads. *Correction to the auditors:* the mapping is not strictly 1:1 — the 21:08:20 attempt logged `StartBidding` but no `NewBid`, so it's "usually" not "always."

**Fix:** Redefine the metric as total **audited** requests = `Σ waves × (direct + validate + validate_only) × (1 + do_start_audit)`. State in §11.3 and §4 L154 that `ValidateBid` itself writes a buyer-visible `Logs_Search_NewBid` row with the full error text, IP and company code. Rewrite §14 step 11 — it is not a footprint-free rehearsal; either run it against a throwaway auction or state the cost explicitly.

---

### 4. §9.3's mandatory state gate is timing-incompatible with §11.4's own schedules — HIGH
*Found by adversarial verifiers in two dimensions independently; missed by all six auditors.*

**Plan says** (§9.3 L419-422): the gate "must pass immediately before any `CreateBid`"; "Re-fetch `GetAuctionSimple` right before the burst." §11.4 L536: "waves at/after `T` re-check the state gate (§9.3) before submitting." §11.1 L480: D = "at `T`, POST `CreateBid` immediately. **One RTT.**"

**Actually true:** the gate *is* a `GetAuctionSimple`. Observed latency (n=128): **min 90.3 / median 135.3 / max 1138.3 ms** — 1.5×–12× your entire 90 ms schedule span. With the gate, D carries **two RTTs**, which §11.1 L482-483 says is what distinguishes V from D. The D/V role distinction the whole concurrency design rests on collapses.

Worse in default `single` mode: the gate can only read `Started` if issued *after* T, so `CreateBid` necessarily lands at `T +` one gate RTT (≥ 90.3 ms, the fastest response of any kind in the capture). Issue it before T and it reads not-started and L422 says don't fire.

**Fix:** Budget the gate's latency explicitly. Either issue one gate check ahead of T so its *response* lands before release and cache the result across waves (state which), or drop the per-wave gate and rely on server-side rejection (`STATE_NOT_STARTED`) as §11.4 already does for pre-T waves. Say which, in §9.2 and §11.4.

---

### 5. The `signalr` clock path is not buildable from this document — HIGH
*Independently found by all four technical dimensions. Escalated by finding #1: it's the only stated remedy.*

**Plan says** (§9.1 L402-404): "Handshake **(observed)** uses `transport=longPolling, clientProtocol=1.5, connectionToken=…, connectionData=[{"name":"auction"}], token=<JWT>`." §12.1a L588 tells you to log "the SignalR negotiate/connect/start/poll/send/abort traffic."

**Actually true:** those params are observed — as query strings on `poll`/`send`, never from a handshake. My substring scan for `negotiate`, `/start`, `/abort`, `/connect` over all 429 request URLs: **zero hits.** `connectionToken` (182 chars, URL-encoded) is already minted on the first captured request. **It is the one value that cannot be derived from anything in these files.**

Three further omissions that make the poll loop unimplementable:
- **Path slash asymmetry:** `142 POST /eauctions.api/signalr/signalr/poll` (**single** slash) vs `129 …//signalr/signalr/send` and `10 …//signalr/signalr/ping` (**double**). §9.1 writes `send` behind an ellipsis and never gives the poll path.
- **No cursor:** each poll POST carries `messageId=d-C00F3698-Ch%2C0%7CBHP%2C27…` and each response returns the next cursor in a top-level `C` field. §9.1 mentions neither. Without cursor advance you re-receive stale messages.
- **Empty-`M` keepalives:** 15 of 142 polls return `{C, M:[]}`. A naive loop calls 10% of polls failures. Also: one response delivered the *same* `ReceiveClock` twice — you must dedupe.
- **Wrong Content-Type and auth:** §1's table says POSTs are `application/json;charset=UTF-8`, but all 271 poll/send POSTs are `application/x-www-form-urlencoded; charset=UTF-8` with the JSON form-encoded under `data=`. SignalR is **not** Bearer-authenticated — it authenticates by the `token=` query param.

**Fix:** Relabel to "handshake **not captured**; params below are read off poll/send." State `connectionToken` must come from a `/signalr/negotiate` call whose shape is unknown — reverse it from `jquery.signalR` 2.x before building. Write all three paths literally with their real slash counts and add "the slash count differs per SignalR endpoint — do not normalize." Scope §1 L38's double-slash rule to `v1` (it is correct there: **146/146**). Add the poll body, the `C` echo, the empty-`M` case, and dedup. Trim §12.1a's list to poll/send/ping.

---

### 6. Concurrency mechanics: pre-warm doesn't pre-warm, no thread-safety note, no pool sizing, no timeout — HIGH
*Merged from timing and structure.*

**Plan says** (§11.2 L491-492): "open and keep-alive the TCP+TLS connection **each thread will use** (e.g. a warm-up GET)."

**Actually true:** in `requests`/`urllib3` there is no thread-affine connection — a shared `HTTPConnectionPool` hands out whatever's free. A *sequential* warm-up loop (the obvious reading of "a warm-up GET") leaves exactly **one** pooled connection. At T, N−1 threads open fresh TCP+TLS inline.

**Evidence — and the capture demonstrates this directly.** Only **3 of 429 entries have non-zero `connect`**, and two of them fire at the exact instants the browser issued **two requests in the same millisecond**:
```
21:13:41.486  SetStartBiddingAudit  connect=184.168  ssl=111.155   (ValidateBid same ms, connect=-1)
21:27:03.011  SetStartBiddingAudit  connect=167.433  ssl= 78.309   (ValidateBid 21:27:03.010, connect=-1)
```
So a *second concurrent* request on this host forces a fresh handshake costing **167–184 ms — 2× your entire 90 ms schedule span**, paid by exactly the D threads §11.1 L481 says "win any timestamp tiebreak."

**Also unstated anywhere in 787 lines:**
- **`requests.Session` is not thread-safe.** §13 L686's "**shared** requests.Session factory," read literally under a 3–5 thread burst, is the documented-unsafe usage. (§3 L121 and §11.2 L493-494 are actually *consistent* — share the jar, per-thread Sessions permitted — it's the §13 file comment that's wrong.)
- **No `pool_connections`/`pool_maxsize`.** (The >10 exhaustion scenario does *not* apply at your recommended N of 2–5.)
- **No HTTP timeout — anywhere.** `grep -E 'timeout|retry|proxy|verify=|pool'` over the plan returns only §12.1a L595 and §12.6 L667, neither defining a value. `requests` defaults to blocking forever. §12.6 tags `NETWORK (timeout/…)` and §12.1a mandates logging "requests that timed out" — an exception path that can never fire. One hung socket in a barrier-synchronized burst hangs the run past the auction window.

**Fix:** Pre-warm with N *concurrent* GETs and `HTTPAdapter(pool_connections=N, pool_maxsize=N)`, or bind one `Session` per thread each with `pool_maxsize=1`, seeded single-threaded before T with a *copy* of the preflight cookies. Add `connect_timeout_ms`/`read_timeout_ms` to §4 (3000/5000 is defensible against max observed 1633 ms). State that network errors are not retried. Fix §13 L686. Name a keep-alive refresh interval.

---

### 7. `bid_is_total` invents a division and never specifies it; the quantity factor is unverified — MEDIUM-HIGH
*Merged: numbers-payload's headline + the adversarial verifier's sharper version + structure.*

**Plan says** (§4 L135): "`bid_is_total` … If true, `bid_value` is a per-item total; **invert to per-unit before use**." That is the **only** occurrence of the flag in 787 lines (verified by grep). §8 never mentions it; §13/§14 define no test.

**Actually true:** three problems at once. (a) "Invert" is the wrong word — converting a per-item total to per-unit is `bid_value ÷ quantity`, not a reciprocal, and §8.3 L341 already uses "**inverse ratio**" for an unrelated quantity (`itemQuantityUnitPrice`). Active trap. (b) The division is exactly the case §8.4 spends a paragraph on — non-terminating quotients, quantization, rounding mode — and here it gets nothing: no scale, no rounding mode, no zero-quantity guard. It also breaks your own round-trip: with scale-addition, `totalValue = quantity × (bid_value ÷ quantity)` will not reproduce `bid_value`'s string form. (c) It flatly contradicts §7 L273-275 ("It **never** modifies it… Value in → payload → sent"), so a reader would reasonably conclude the flag is dead.

**And the related overclaim** (§8.4 L358): "**Invariant to sanity-check (proven across all 8 HAR bids)**: `itemQuantityUnitPrice × bidValue = initialValue × quantity`." The equation holds exactly — but **every captured bid has `quantity == "1.000000"` and exactly one item** (I verified: distinct quantities = `{'1.000000'}`, item counts = `{1}`). With quantity ≡ 1 the factor is non-discriminating. I ran four mutually inconsistent formulas against the wire strings — `init×q/bid`, `init/bid` (no quantity at all), `init×q²/total`, `init/(bid×q)` — **all four reproduce all 8 captured values byte-for-byte.** What is proven is only `iQUP × bidValue = initialValue`. §14 step 1 then directs you to "nail it first against the §8.5 golden values" — a golden set mathematically incapable of discriminating correct from wrong quantity handling.

*In the plan's defence, and correcting the auditor:* the formula's provenance **is** correctly labelled (§8.3 header cites `priceListController.js`; Appendix B L784 files the payload formulas under "From client code only"), and Appendix A L775 does disclose the single item at qty 1. Only line 358's word "proven" overclaims, and the invariant assertion is correctly scoped to "formatting/precision bug."

**Fix:** Delete `bid_is_total` (simplest, consistent with §7), or define it precisely — `bidValue_i = bid_value / item.quantity_i` — accept that `bidValue` then differs per item, and amend §7 and §8.3 L339. Separately, relabel L358: "consistent with all 8 HAR bids, but `quantity ≡ 1` in every capture, so the quantity factor is **unverified**"; move it into §15 alongside Q7; and add a runtime guard that refuses (or loudly warns with alternatives logged side by side) on any item with `quantity ≠ 1`.

**Related, same root:** Appendix A L776 lists `initialUnitPrice 390698716` on the **item** line, but no item-level field carrying that value exists anywhere in the HAR — 130 occurrences, all `"startPrice":390698716.0` (an *auction*-level field) in the `GetAuctionSimple` responses. It's derived, not captured, and coincides with `startPrice` only because there is one item at qty 1. Mark it derived. (§6.2 is **not** defective here — L235 already heads the subsection "(client-code-only)".)

---

### 8. "Exact request header set" is 7 of 16 — MEDIUM
*Independently found by three dimensions.*

**Plan says** (§1 L50-60): "**Exact request header set to replicate** (from the HAR bid calls)."

**Actually true** — the observed `ValidateBid` request carries 16 headers:
```
Accept, Accept-Encoding, Accept-Language, Connection, Content-Length, Content-Type,
Host, Origin, Referer, Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site,
User-Agent, sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform
```
Four of the nine omissions (`Host`, `Content-Length`, `Connection`, `Accept-Encoding`) your HTTP library emits anyway. The **six that matter** are `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform` (on 428/429) and `Sec-Fetch-Dest/Mode/Site` (427/429) — precisely the client-hint and fetch-metadata surface a UA-sniffing WAF cross-checks against the `User-Agent` §1 tells you to spoof. Also endpoint-conditional and undocumented: `X-Requested-With: XMLHttpRequest` (282 entries: all SignalR + `GetEventLogs`; **absent** on ValidateBid/GetAuctionSimple/SetStartBiddingAudit/GetBidsTotalInfo) and `Cache-Control: max-age=0` + `If-Modified-Since: Mon, 26 Jul 1997 05:00:00 GMT` (129: GetAuctionSimple + GetBidsTotalInfo).

Two secondary mismatches: `Origin` is **POST-only** (present on all 287 POSTs, absent from all 142 GETs) but listed unconditionally while only `Content-Type` is marked "(POST only)"; and the UA is given truncated as `Chrome/…` while §4 L155 says "Defaults to a real Chrome UA" — the version (`Chrome/150.0.0.0`) sits in the capture and has to be invented from the doc.

Note the 403 consequence is **speculative** — all 429 captured responses are 200 and no library UA was ever sent. The defect is the word "exact," not a demonstrated failure.

**Fix:** Paste the full 16, marking library-emitted vs must-set-manually. Mark `Origin` POST-only. Give the literal UA string.

---

### 9. Audit-endpoint semantics contradict the wire and the plan's own inversion note — MEDIUM
*Independently found by three dimensions.*

**Plan says** (§10 L458): "The browser fires **`SetStartBiddingAudit`** when the bidder **opens/starts bidding**… and **`SetSubmitBiddingAudit`** on submit." §4 L152-153 defaults both `do_start_audit` and `do_submit_audit` to **true**.

**Actually true:** `SetStartBiddingAudit` fires **once per bid attempt** — 8 POSTs paired 1:1 with the 8 `ValidateBid` at Δ ≤ 1 ms (0,+1,0,0,+1,+1,+1,0), not once per session. `SetSubmitBiddingAudit` appears in **0 of 429 entries** — and per §10's own inversion note two lines above, it's what `auditStartBidding()` posts, i.e. the *session-open* audit. So `do_submit_audit=true` fires a never-observed endpoint that is probably the wrong one, post-win, on a live auction.

*Correcting the auditors:* §4's "**before** firing" is **right** — the server logs `Logs_Search_StartBidding` at 21:16:40.1328905Z before `Logs_Search_NewBid` at 21:16:40.415597Z. Only the *cardinality* is wrong. And per-attempt firing is faithful browser mimicry, not an unintended doubling — though it does belong in the footprint metric (finding #3).

**Fix:** Rewrite L458: "Observed: `SetStartBiddingAudit?auctionId={id}` (empty body, `Content-Length: 0`) fires once per bid attempt, concurrently with `ValidateBid` (Δ ≤ 1 ms, 8/8). `SetSubmitBiddingAudit` was never observed; its trigger is unknown." Default `do_submit_audit` to **false**. Add to §10's audit row: send `Content-Type: application/json;charset=utf-8` + `Content-Length: 0` with an empty body — `requests.post(url, headers={...}, data=b'')`, **not** `json=None`, which sends neither header. And state in §12.6 that a 200 with an empty body maps to success.

---

### 10. §3's JWT claim keys are URIs, not bare names — MEDIUM
*Independently found by two dimensions.*

**Plan says** (§3 L104-113), table labelled "**(observed, from the HAR token)**": `LoggedInCompanyCode`, `LoggedInCompanyName`, `LoggedInUserId`, `LoggedInUserName`, `LoginGUID`.

**Actually true** (I decoded the token from the `token=` query param myself): bare keys are only `iss, aud, nbf, exp, unique_name, authmethod, auth_time`. All **15** `LoggedIn*`/`LoginGUID` claims are `http://www.nextway.com/NextWay.STS.Claim/`-prefixed. `claims["LoggedInCompanyCode"]` → `KeyError`.

Impact is confined to **logging** (`config.json`, `summary.json`) — the one operationally load-bearing claim, `exp` (§3 L115 preflight, §12.6 `AUTH_EXPIRING`), is bare and works. Values are all correct: `exp − nbf = 86400`, `aud = https://subastas.secop.gov.co/eauctions.site/`, `LoggedInCompanyCode 900000000` = the `userCode` in `GetEventLogs`.

**Fix:** Print the URIs verbatim, or specify `claims.get(k) or claims.get('http://www.nextway.com/NextWay.STS.Claim/'+k)`. Mark the table **partial** (21 claims exist) and add `iss = https://community.secop.gov.co/STS/users/issue.aspx` — it's the only evidence in the set for where the token is minted, which Appendix B lists as "not captured at all." Note the ambiguity: the token carries both `LoggedInCompanyCode 900000000` (= audit `userCode`) and a distinct `LoggedInUserCode 3212081`.

---

### 11. The JWT leaks into all three log sinks via the URL — MEDIUM
*Independently found by three dimensions.*

**Plan says** (§12.4 L655-656): "**Never** log the raw JWT, full `Authorization` header, or full cookies… unit-test it against a sample request." §12.2 L619 mandates logging `url`; §12.1 L575 mandates `capture.har` of "**every** request/response"; §12.1a L588 explicitly requires the SignalR traffic.

**Actually true:** on the SignalR path the JWT is a **query parameter** — `token=<2370 chars>` on all **281** poll/send/ping requests. §12.4's blanket "never log the raw JWT" does forbid it in spirit (so this is *not* the direct contradiction the auditors claimed), but every worked example is header- or cookie-shaped, so `redact()` will plausibly be built as a header-dict transform and §12.2's `url` sails through — writing a live 24 h bearer token into `events.jsonl`, `http/*.json` and `capture.har`, the artifact most likely to be attached to a bug report.

**Fix:** State explicitly that `redact()` rewrites URL query params matching `token|access_token|jwt`, not just header dicts, and require `test_redaction.py` to cover a real SignalR poll URL.

---

### 12. `stop_on_success` is provably inert at your own recommended schedules — MEDIUM
*Found by an adversarial verifier; missed by all six auditors.*

**Plan says** (§11.4 L540-541): cancelling not-yet-released waves "**directly cuts** the audit-log and WAF footprint." §4 L151 defaults it true on that rationale, and §12.3 reserves a `waves_dropped` field.

**Actually true:** the recommended burst (L551: `bid_count=4, pre_ms=60, interval_ms=30`) spans **90 ms end to end**. The fastest bid-endpoint response anywhere in 429 entries is `ValidateBid` **168.5 ms** (median 614.4, max 1633.1); the fastest response of *any* kind is 90.3 ms. Wave 0's verdict cannot return before wave 3 has already fired. **Zero waves are ever dropped.** Same for the "wide net" custom shape (200 ms span) at anything near median RTT.

**Fix:** Say so. Either drop the claim, or state the minimum wave span at which `stop_on_success` does anything (≳ observed p50 bid RTT, i.e. hundreds of ms) and note that your recommended shapes are below it.

---

### 13. The success predicate is stated three ways, and `DUPLICATE` is indistinguishable from a real rejection — MEDIUM
*Found by an adversarial verifier.*

**Plan says:** §10 L466-467 — "treat **`isValid == true` and empty `errorMessage`** as success." §11.2 L496 — "the first `CreateBid` returning `isValid==true`." §11.4 L538 — "any `CreateBid` returns `isValid == true`." Two of three drop the `errorMessage` conjunct. A `CreateBid` returning `isValid:true` with a warning is a win under `bidder.py` and a non-win under §10 — and `stop_on_success` cancels the schedule on that basis. Nothing in the capture resolves it (all 8 responses are `isValid:false` with non-empty `errorMessage`), which is exactly why the sections that dropped it shouldn't have.

**Separately:** §11.4 L541 says post-win bids "bounce (equal/not-lower)" → tag `DUPLICATE`, not failure. But the observed response for a not-lower bid **is** `BidErrMin` — same status, same body, byte-identical `errorMessage`/`minBid`. §12.6 L669 mandates "Map HTTP status + response body → one enum value in a single classifier function." With status and body identical, that function **cannot** distinguish them. The only disambiguator is local run state (has `first_success` been set, did this attempt start after it), which the plan never routes into the classifier. Consequence: every post-win wave is tagged `BID_ERR_MIN`, `failure_taxonomy` is wrong, and §12.6's blockquote promise cannot be delivered.

**Fix:** Pick one success predicate and state it once. Give the classifier a run-state parameter, or classify `DUPLICATE` outside it.

---

### 14. `lead_ms` from a single RTT sample of the wrong shape — MEDIUM

**Plan says** (§9.4 L426-427, §4 L141): "Measure RTT during preflight (time a `GetAuctionSimple`). Default `lead_ms ≈ RTT/2`."

**Actually true:** one sample against a distribution spanning **90.3 → 1138.3 ms** (n=128; p25 104.5, median 135.3, p75 281.4, p95 ~665). `lead_ms` is a random variable ranging ~45–570 ms against a 90 ms schedule — two identical invocations minutes apart differ by ~half a second, on top of the clock error. HAR `time` also includes client-side `blocked` queueing (max **235.6 ms**) and `receive`, which are not propagation.

**Explicitly reject one auditor's proposed fix:** *do not* derive `lead_ms` from `ValidateBid` RTT/2 (~307 ms). Per-entry `timings` show `ValidateBid` is almost entirely `wait` (1633.1 total / 1627.1 wait) — that's server-side validation work, not transit. Leading by it would make the request arrive ~240 ms **before** the open and be rejected.

**Fix:** Take N ≥ 10 preflight samples, use a low quantile (min or p10) of `timings.wait`, not total `time` and not one sample. Log the full distribution. Refuse to run if p50 RTT exceeds the schedule span.

---

### 15. §7 omits the single most probative bid in the capture — MEDIUM (document honesty)
*Independently found by two dimensions.*

**Plan says** (§7 L278): "bids of **1/10/200** came back with `errorType: "BidErrMin"`."

**Actually true:** there are **four** distinct values — `10 ×4, 1 ×2, 200 ×1, 7813974.32 ×1`. The fourth bid `7813974.32` is **exactly the `minBid` the server had been returning**, and was rejected with a byte-identical error. That is the datum that kills the "minBid is a floor you must clear" reading and is the strongest support §7's own story has. The plan implicitly relies on it — §8.4 L352 quotes `"50.000000000000"`, which can only arise from `390698716 ÷ 7813974.32` — so the document is internally inconsistent about how many bids it saw.

**Also** (L278-280): the causal claim ("**because** `GetBidsTotalInfo` showed `myLastBid: 0`") carries **no provenance marker** in a document whose defence is that everything is marked, and it cites a single post-auction sample (21:30:11.665Z — *after* `GetAuctionSimple` at 21:30:11.502Z already read `Finished`) while the contemporaneous evidence — `auctionTotalBids: 0` / `supplierTotalBids: 0` in all 8 responses — is never cited. *In the plan's favour, correcting the auditor:* the server's own message names the baseline explicitly ("El valor total tiene que ser inferior al valor de **su último lance**"), which does select your reading over the rival "server refuses any bid with zero prior bids."

**Fix:** State all four values and counts. Add that a bid at exactly `minBid` was rejected identically. Mark the causal claim `inferred` and cite `auctionTotalBids`/`supplierTotalBids`.

---

### 16. Provenance mislabels — MEDIUM in aggregate, because they're the doc's trust mechanism

§2 L68-74 sets up Observed / Client-code-only / Inferred and L8 says "Read those markers — they are where bugs will hide." Then:

- **§1 L43-45**, WAF 403, marked "(**Observed** on the static host)": status census over all 429 entries is `{200: 429}`; zero non-200, zero redirects, and the only two static-host entries succeeded *with* a Chrome UA, so they cannot show the negative case. Not checkable from this corpus. (*Refuting the auditor's added charge:* §1 scopes it to the static host and defers the API host to §15 Q6 — that is coherent, not inconsistent.)
- **§5 L178**, "(verified **2026-07-29**)": one day in the future relative to today and to every artifact's mtime, about a host (`www.secop.gov.co`) that appears in **zero** HAR entries.
- **§9.3 L423-424** labels the Finished-lag "(**Observed**… — **inferred**…)" in one sentence. The fact is directly observed and bracketed by the 3.000 s poll cadence to `(+9.35 s, +11.66 s]`. Rewrite as observed-with-a-precision-caveat.
- **§9.3 L420-421** gives one unmarked enum and applies it to **two** fields. `auctionExecutionStateCode` is `Running` on **128/128** — and `grep -n 'Running'` over the plan returns **zero hits**. The only value the field ever takes is absent from the document. Worse, it stayed `Running` on all 125 responses *after* `auctionStateCode` flipped to `Finished`, so `!= "Paused"` is permissive past the auction's end and contributes nothing to the gate.
- **§10 L434** ("From `apiService.js` cross-checked with the HAR") has no per-row markers, and **only 5 of 16 rows exist on the wire** (`bid/ValidateBid`, `audit/SetStartBiddingAudit`, `auction/GetAuctionSimple`, `Bid/GetBidsTotalInfo`, `audit/GetEventLogs`). *In the plan's defence:* the load-bearing ones **are** marked elsewhere (§6.1 L215 "Not captured", §2, §10 L460, Appendix B, §15 Q2). The genuine residual gap is narrow but real: **nothing states that the path string and casing of `bid/CreateBid` are unverified**, and no build step probes that route before T (§14 step 11 is validate-only). A casing miss on an IIS/ASP.NET route is a 404 discovered at T.
- **§9.3 / §11.4 L531-532** assert flatly "If the auction is not yet `Started`, the server rejects it → `STATE_NOT_STARTED`, expected, not a failure." Zero supporting bytes: the HAR begins **7 m 40 s after** `startDate`, all 128 `GetAuctionSimple` calls run *after* `endDate` (first at 21:30:02.506Z), and `ReadyToStart` was never returned. §12.6's `STATE_NOT_STARTED` names a response nobody has seen. §15 Q9 covers it — but §9.3/§11.4 read as settled design at the point of use.
- **§11.3 L503** "the capture only ever showed ~2.5 s polling": nothing runs at 2.5 s. `GetAuctionSimple` gaps are median **exactly 3.000 s** (3.0 ×54, 2.999 ×29, 3.001 ×24), and the SignalR clock cycle is ~30 s. It's the one quantitative anchor §11.3 offers and it's off by 20% — and that traffic sits entirely *outside* the bidding window.

---

### 17. Config, schema and cross-reference bugs — LOW individually, but they're the first thing an implementer touches

| Where | Defect | Fix |
|---|---|---|
| §4 L134, L142, L156, L166 | Four wrong cross-refs. `bid_value` "normalize (§6)" → §8.1. `validate_lead_ms` "(§8)" → §9.2/§11.4. `time_source` "Clock-sync method (§8)" → §9.1. L166 "§9, §11.3" → §1, §11.3. Two of four misdirect into §8, the section you call highest-stakes. | Correct them |
| §4 L133 | `auction_page_html` is a config **string** with no file-path option; §4 says "CLI flags, env, or a JSON/TOML file." The real artifacts are **463,702** and **333,861 bytes**. Un-pasteable; §13 L700 already assumes a file on disk. | Add `auction_page_html_path` |
| §4 L145-146 + §11.4 L521 | `pre_ms` and `interval_ms` both default **0**, and `offset_ms(i) = −0 + i·0 = 0` for all i. `mode=burst, bid_count=5` silently produces 5 simultaneous waves at T — `single` with a bigger D, N barriers, maximum audit footprint. L164-166 validates only `bid_count >= 1`. | Require `interval_ms` for burst, or validate `bid_count > 1 ⇒ interval_ms >= 1` |
| §4 L163-164 | Self-contradictory in one sentence: "at least one submitting thread **must** be configured" vs "if none, or if `dry_run`, no `CreateBid` fires." Error or warn? (Milestone 11 is **not** blocked — its dry-run framing falls under the tolerated branch.) Also `n_direct` is Required=yes (L136) though unused in burst/custom (L158-159). | Restate as: error only when `dry_run==false` AND submitters==0 AND `n_validate_only==0`. Make `n_direct` conditionally required |
| §11.4 L516 vs L521-522, §4 | Wave schema declares `validate_only`, but no `wave_validate_only` key exists and both burst/custom rows compose from `wave_direct + wave_validate` only. §12.3 L640's example omits it too. Dangling field → divergent dataclass. | Add the key, or state P is single-mode only |
| §12.2 L610-611 vs L627-628 | The `event` enum omits `schedule_planned` and `wave_dropped`, which the same section mandates 8 lines later. Per-attempt fields (`mode`, `wave_index`, `scheduled_offset_ms`, `actual_offset_ms`) have no schema home. `t_from_fire_ms` (L644) appears exactly once in 787 lines, undefined, beside `actual_offset_ms`. | Add both events, define the `detail` sub-schema, define or delete `t_from_fire_ms` |
| §12.3 L640-641 | `"waves_fired": 3, "total_submissions": 4` with `direct: 1` — but L160/L544 define it as `Σ waves × (direct + validate)` = 3. Planned vs actual, and it's the field §4 L165 warns on. | Disambiguate |
| §12.1 L575-576, §13 L693-695 | `capture.har` (**required**) and `timeline.jsonl` are owned by **no module**. §12.5's QueueHandler covers only the `logging` sink; `http/NNN_*.json` (shared `NNN` counter, ordering undefined), `capture.har` and `timeline.jsonl` have no concurrency discipline — they may interleave-corrupt at exactly the fire instant they exist to record. Two conflicting redaction conventions for `Authorization`/`Cookie` (§1 L62-64 "stripped" vs §12.4 L656 `Bearer <redacted>`). | Route all four sinks through the QueueListener; assign an owner; pick one convention; define `NNN` as assign-at-send under the `corr_id` counter. (HAR version/`timings` are *not* gaps — the spec permits `-1`, which the source capture uses on every entry) |
| §3 L115 | `safety_margin` gates a hard `AUTH_EXPIRING` abort but is not one of §4's 25 keys; "e.g. 5 min" is a suggestion | Add `auth_safety_margin_s` with a default |
| §5 L188-189, L204 vs L707 | `eauction_doc_id` is consumed **nowhere** (grep: lines 188, 204, 707 only). `market` likewise (187, 196, 204, 707, 770) — the only `marketCode` in the plan is §1 L57's Referer, which **hardcodes** `GovCo`/`es-CO` while templating `auctionId`. `redirect_url` is a parse target with no field to hold it (absent from both L204 and L707). L189's "the **full** URL" is a relative path. | Template the Referer from `AuctionRef` (`?auctionId={auction_id}&languageCode={lang}&marketCode={market}`) — that retires both orphans. Mark `eauction_doc_id` log-only/never-fatal. Say `redirect_path` |
| §6.2, §8.4 vs §8.2 | No `json.loads(..., parse_float=Decimal)` anywhere; no module owns response decoding, though §13 types `start_price: Decimal`. `startPrice` arrives as float. *Impact is limited* — §6.2 L248/L253 correctly source the formula inputs from the `*Str` string fields, so §8.4 is unaffected | Mandate one `load_json()` helper |
| §8.1 L301-302 | "dot-as-thousands" is not a decidable predicate: `"1.234"` is 1234 (Colombian) or 1.234 (wire) with no tiebreaker. The L304-305 round-trip self-test only ever runs against server `minBid` (always 2 dp, multiple groups) — trivially well-defined, always passes, **never touches the user-input branch** — yet carries "refuse to bid." Assurance about the wrong code path | Explicit rule: comma present ⇒ Colombian; no comma ⇒ require zero dots or one dot with fractional length ≠ 3, else **reject**. Add `1.234`/`1.500`/`390.698.716` as rejection test cases |
| §8.3 L339-340, §8.5 L374-375 | Key order inverted vs the wire (`totalValue` precedes `bidValue` in all 8 bids). Values are all exact. *Correcting the auditors:* the harm chain is invented — §13's oracle is the §8.5 example itself, so no described test breaks | Reorder for fidelity; state the golden compares parsed structures |
| §8.5 L382 + §13 L699 | `"Date": "<now ISO8601 UTC>"` is a per-run placeholder in the object §14 step 1 calls the check for "the riskiest logic." The plan mandates a golden comparison against something that provably cannot be compared whole, and never says which fields to exclude or how to inject a fixed clock. Also: pin the format — `new Date().toISOString()` is 3 fractional digits + `Z`; Python's `isoformat()` default is 6 digits + `+00:00` | Define the comparison contract |
| §8.4 L356, §15 Q8 | "for whole-number bid values like 1 or 2 the division is exact, so prefer those" — integrality is irrelevant, **divisibility** is what matters. For `initialValue = 390698716`, 6 of the first 12 integers are non-terminating (3, 6, 7, 9, 11, 12). A false mitigation in the consolidated open-questions list | Replace with the real test: terminates iff `bidValue` divides `initialValue × quantity` to ≤ 12 dp. Compute it at build time and WARN with the residual |
| §11.4 L524 vs §9.1 L401 | `offset` (clock offset) and `offset_ms` (wave schedule offset) are unrelated quantities with the same name, **one clause apart** in the most load-bearing formula in the document. §12.2/§12.3 add `scheduled_offset_ms`/`actual_offset_ms`. The signs are correct; the naming is the hazard | Rename to `clock_offset` throughout |
| §6.2 L260-263 | `AuctionbyItem`/`AuctionbyLot` left as an unresolved either/or. Option (b) breaks §11.2 L488 ("pre-build the exact payload bytes **once**"), §11.4 L544's submission accounting, and the single-first-success model. No §13 module owns the branch, no §14 milestone mentions it, no test covers it, and the capture is 100% `AuctionbyProposal` | Scope v1 to `AuctionbyProposal` in §0; hard-abort on any other `auctionVersionCode` with a new §12.6 code |
| §13 tests | No `test_detail.py` — the §6.2 `ItemMeta` mapping (client-code-only, flagged uncertain in §15 Q7) has zero offline coverage, and milestone 7's golden consumes hand-built `ItemMeta` so it can't exercise the mapping. (*Milestone 11 does isolate it before any `CreateBid` — the auditors' "three unknowns at once" framing is overstated*) | Add it; capture a real `GetAuction` body to `tests/fixtures/` |
| §5 L200-201 | State check gives no locator. `Terminado` occurs once, as link text inside `<td id="incWidgetAuctiongrdEAuctionGridtd_thEAuctionStateCol">`; `EAuctionStateCol` is absent from `bid.html` entirely, so the check silently no-ops there. Only that one label is evidenced. **Cosmetic only** — §9.3's server-side gate is the real guard | Locate by `td[id$='thEAuctionStateCol']`; distinguish `STATE_UNKNOWN` from "not finished". Note in §13 that the fixture is itself a **finished** auction |

**Also worth one line each:** both HTML fixtures are DOM serializations (`data-scribe-recorder-ready="true"`, pretty-printed one attribute per line), not view-source — so every §5 regex is calibrated against prettified output and must use `\s*\+\s*`, never a literal `' + '`. §5 should say which capture method is required. And `docUniqueIdentifier` occurs **21×** in `beforebid.html`, only 1 of which is the auction (the other 20 are the *opportunity* dossier `CO1.OPDOS.31397606`) — your `CO1\.EAREQ\.` anchor discriminates correctly, so say explicitly that nobody may relax it.

---

## What the plan gets right — trust these as-is

This is not padding. I verified each of these against the bytes.

**The payload spec is essentially perfect.**
- §8.3's top-level field table: all 7 fields, no extras, no omissions, **order matching the wire exactly** (`auctionId, currency, Date, ranking, items, totalValue, languageCode`).
- The capital-D `Date` at top level while the response echoes lowercase `date` — a genuine trap, caught.
- §8.3's per-item set: the wire has exactly 12 fields and you list exactly those 12. Only the `totalValue`/`bidValue` ordering differs.
- Type discipline: `ranking` a JSON **number** (−1) while every money/quantity field is a JSON **string**. Verified per-field across all 8 bids. §8.5 gets all of it right.
- `totalValue = quantity × bidValue` with **scale addition and no trailing-zero stripping** reproduces the wire byte-for-byte, including the awkward case: `Decimal("1.000000") × Decimal("7813974.32")` → `"7813974.32000000"` (8 dp), exactly HAR bid 7. "Do not strip trailing zeros" is correct and load-bearing.
- `itemQuantityUnitPrice` at 12 dp reproduces all four distinct wire strings exactly.
- The invariant holds **exactly in Decimal** (not approximately) for all 8 bids, including `1953493.58 × 200 = 390698716` with no loss.
- §8.1's Colombian↔wire recipe works on the real string: `"7.813.974,32 COP"` → `Decimal("7813974.32")`. ✓
- §8.5 is a **real observed payload** (HAR entries 4 and 5), not invented, and every value matches.
- §8.3/§8.4 do generalise correctly to N items — `bidValue` per-unit and identical, per-item `totalValue` varying, top-level as a max-scale Decimal sum, invariant per-item. The N-item risk lives in `bid_is_total` and byItem/byLot, not in these formulas.

**The wire facts.**
- Double slash after `v1`: **146/146** v1 requests. Exactly right.
- Path casing byte-exact for all 5 observed endpoints, including `Bid/GetBidsTotalInfo` capital-B — §6.1 L216's warning is correct.
- Referer format: **427/429** verbatim, including parameter order. User-Agent, `Accept`, `Accept-Language`: verbatim.
- §6.1's `GetAuctionSimple` field list matches the real 59-key response including all 8 empty arrays.
- §10's ValidateBid response list is exactly the 22 observed keys — no omissions, no inventions.
- §8.3's "server ignores `Date`" echo (`date: "0001-01-01T00:00:00"`), `bidId: 0`, `isValid: false`, `errorType: "BidErrMin"`, `minBid: "7.813.974,32 COP"` — confirmed on all 8.
- §1's "CORS headers are browser-only" is well-founded: `Access-Control-Allow-*` appears on exactly the 287 POST responses and no GET; no OPTIONS preflights anywhere.
- §5's `lang` extraction is the **one §5 instruction that works verbatim** — `beforebid.html:4456` has `<option value="es-CO" selected="true">`, and the `/es-CO.json` fallback yields 68 hits with zero competing locales.
- §5's `CO1.EAREQ` constraint is genuinely disambiguating (1 of 21 `docUniqueIdentifier` hits).
- §5's "`mkey` not needed to call the eauctions API" is **confirmed from the wire**: across all 429 entries `mkey`, `externalId`, `CO1.EAREQ`, `docUniqueIdentifier` all appear **0** times, vs `marketCode` 427 and `languageCode` 437. Only those two cross onto the API side.
- Appendix A checks out completely except the one derived `initialUnitPrice` line — every other value verified against `GetAuctionSimple`, the bid bodies, `GetBidsTotalInfo`, and `beforebid.html`.
- §11.3/§12's claim that the audit log carries your IP and company code is exactly right, and §3 L109's `LoggedInCompanyCode → userCode` mapping is confirmed (`900000000` in both).
- §11.4's burst schedule math is correct — `offset_ms(i) = −pre_ms + i·interval_ms` yields exactly `−60, −30, 0, +30`. No off-by-one.
- §9.2's sign convention is correct. §9.1's "monotonic for scheduling, wall clock only to derive offset" is the right architecture and is applied consistently in §11.4 and §12.5.
- §9.1's SignalR **message shapes** are wire-confirmed: send body decodes to `{"H":"auction","M":"RequestClock","A":[],"I":39}`, and "Also `Connect(auctionId)` once" is exactly right — 129 sends = 128 RequestClock + **1** Connect.
- §7's core reasoning is right and its operative instruction ("never adjust the value, just log the rejection") is correct and unaffected by the omitted fourth bid.
- §6.2 is correctly headed "(client-code-only)" and §15 Q7 correctly books it for live verification. §3's sticky-cookie inference is honestly marked at L119.
- §0/§11.4 scope coherence: "one logical first bid" vs many attempts is not a contradiction — L26 patches it explicitly.

**And one upgrade you're entitled to make.** You wrote (§1 L62-64) that the Bearer scheme is "confirmed from the app's interceptor **code**, not from the wire." It's confirmable from the wire by header-size accounting. HAR creator is `WebInspector`; response `headersSize` reconciles exactly, so the *request* deficit is real stripped data. Every v1 request is short by exactly **2547** bytes; every SignalR request by exactly **153**. The difference, 2394, equals `len('Authorization: Bearer ') + 2370 + CRLF` — and a 2370-char JWT appears verbatim in the SignalR `token=` param. So: `Authorization: Bearer <that exact JWT>` on all 146 v1 calls, plus a ~143-byte Cookie value on all 427 subastas requests, **proven from the wire**. The same accounting proves SignalR is **not** Bearer-authenticated (153 bytes leaves no room), so §3 L90's "attach the JWT to every request" should be scoped to the v1 API.

---

## Structural issues — the pattern, not the errors

Four failure modes produced almost every defect above. Fixing them fixes the class, not the instance.

**1. Reasoning-from-the-shape-of-the-data, presented in the voice of reading-the-data.** The clock design, the §5 parser, and the "proven" quantity invariant are all the same mistake: you looked at a value, saw that it was *there*, and concluded you knew how to *get* it. `externalId` **is** in the onclick — but not as a URL. Server time **is** in the `Date` header — but not to sub-second. The quantity factor **is** consistent with all 8 bids — but 1 is consistent with everything. Each claim is true as an observation and false as a procedure. **Fix: for every extraction and every formula in the doc, state the procedure and show it running against the fixture.** Most of these would have died in ten minutes at a REPL.

**2. The provenance system has three labels and needs five.** §2 offers Observed / Client-code-only / Inferred, and it can't express the cases that actually bit you: *observed-but-degenerate* (the invariant — 8/8 consistent, zero discriminating power), *observed-output-of-an-unobserved-process* (the SignalR handshake), *observed-outside-this-capture* (the 403, the 302), and *derived-and-coincidentally-equal* (Appendix A's `initialUnitPrice`). Every one of those got filed as "Observed." **Fix: add "Observed but non-discriminating (why)" and "Asserted from outside this capture (where)" as first-class markers, and require a per-row marker on §10's endpoint table.** The one claim you stamped "verified" with a date is future-dated — that's the tell.

**3. Safety mechanisms were designed but never costed.** `dry_run` writes audit rows. `stop_on_success` cannot drop a wave at any recommended schedule. The footprint formula omits the threads you recommend running. The state gate costs 135 ms in a 90 ms schedule. `gate_pre_t=false` is the only knob that acknowledges a gate has a price. Each was specified as a *policy* and never checked against the *latency and side-effect* data sitting in your own capture. **Fix: for each safety mechanism, add one line — "this costs X ms / writes Y rows / drops Z waves at the recommended settings." Three of the four die on contact with that line, which is the point.**

**4. Two sections were written from what the system *should* look like rather than what the capture shows: §9 and §11.** They are internally elaborate (barriers, waves, roles, sub-ms busy-waits) and externally ungrounded. The capture contains **no successful bid, no concurrency in bidding, no auction open, no pre-`Started` request, no 403, no rate limit, and no `CreateBid` at all**. §11.3 L507-510 and §15 name most of this honestly — but §14's build order commits to the entire concurrency layer at step 9 and then validates it at step 11 with `D=0,V=0,P=1`, a single sequential ValidateBid that is **structurally incapable** of answering either Q1 (tiebreak) or Q2 (concurrent tolerance). The build order does not gate on the questions the design's value depends on. **Fix: add a step between 9 and 11 that actually probes Q1/Q2 — `mode=custom, schedule_offsets_ms=[0,0], wave_direct=1` on a throwaway auction where `myLastBid > 0` — and make `burst`/`custom` contingent on its outcome. Ship `single` first.**

**As a document:** the two things that would raise its floor most are (a) an **evidence-coverage table** at the top — what the 429 entries do and do not cover, stated once, in one place, so §9/§11 can't quietly read as settled; and (b) making every fixture, expected value, and file path **explicit** (§13/§14 never name which HTML file, §8.5's golden has an uncomparable field, `test_page_parse` has no assertions listed). Appendix B is the right instinct — it's just materially incomplete: add `CreateBid` **entirely** (not just its success response), `GetMinBid`, `SetSubmitBiddingAudit`, the SignalR handshake, the auction open, any concurrency, any second bidder, and any non-200.

---

## Prioritized action list

**Before writing any code:**
1. **Rewrite §9.1.** Say `Date` is second-granular. Replace median-of-samples with interval intersection; log the residual width; gate firing on it. Add the two-clock disagreement (Date `[−0.016,+0.114]` vs hub `−0.547`) and state that the state machine's clock is unidentified.
2. **Fix §5.** De-concatenation step, concatenation-aware fallbacks, ban the bare-GUID fallback, name the page (`Oportunidades`) and the fixture file, add the `iframe#iframeEauction` extractor, handle the multi-row grid.
3. **Correct the safety accounting.** `ValidateBid` and `SetStartBiddingAudit` write buyer-visible rows; redefine the footprint metric to include P and `do_start_audit`; rewrite §14 step 11; note `stop_on_success` is inert at recommended densities.
4. **Budget the state gate** (§9.3/§11.4) — 90–135 ms in a 90 ms schedule. Cache-and-share, or drop it and rely on server rejection. Decide.

**Before the first live call:**
5. Paste the full 16-header set; mark `Origin` POST-only; give the literal UA. Add `X-Requested-With` / `Cache-Control` per-endpoint.
6. Add `connect_timeout_ms`/`read_timeout_ms`; fix pre-warm to N concurrent GETs + pool sizing; state `requests.Session` is not thread-safe and fix §13 L686.
7. Fix the JWT claim keys to URIs; extend `redact()` to URL query params and test it against a SignalR URL.
8. Delete or define `bid_is_total`; relabel the L358 invariant as quantity-unverified; add a `quantity != 1` guard.
9. Flip `do_submit_audit` to false; correct §10 L458's cardinality; specify the empty-body audit POST shape.
10. Pick one success predicate; give the classifier run-state so `DUPLICATE` is separable from `BID_ERR_MIN`.

**Documentation pass:**
11. Fix the four §4 cross-refs, the burst defaults, the `wave_validate_only` gap, the `n_direct`/`dry_run` validation contradiction, the events enum, `t_from_fire_ms`, `total_submissions`, `safety_margin`, the orphaned `market`/`eauction_doc_id`/`redirect_url` (template the Referer), `auction_page_html_path`, the §8.5 key order and `Date` placeholder, the §8.1 user-input rule, the §15 Q8 divisibility error, `offset` vs `offset_ms`, and the §11.3 "2.5 s" figure.
12. Fix the provenance markers: the 403, the future-dated 302, the double-labelled Finished lag, `Running` in the state enum, per-row markers on §10, and an explicit note that `bid/CreateBid`'s path and casing are unverified with no build step that probes them.
13. Add the evidence-coverage table and expand Appendix B.
14. Scope v1 to `AuctionbyProposal`; add `test_detail.py`; assign owners for `capture.har` and `timeline.jsonl` and route all sinks through the QueueListener.

**Explicitly dropped as refuted — do not spend time on these:** `#div-item-data` missing from the two HTML files (correctly labelled client-code-only; it lives in the SPA document inside the iframe); a GUID guard on `itemId` (correctly labelled, §15 Q7 books it); deriving `lead_ms` from `ValidateBid` RTT (that extra ~480 ms is server `wait`, not transit — it would make you arrive *early* and be rejected); `parse_float=Decimal` as a formula-corrupting bug (§6.2 sources both inputs from `*Str` strings); §3-vs-§11.2 as "two mutually exclusive architectures" (they're consistent); milestone 11 being unrunnable (its dry-run framing is covered); the `pool_maxsize=10` exhaustion scenario (N is 2–5); §1's WAF claim as an "internal inconsistency" (§1 scopes to the static host, §15 Q6 holds the API host — coherent); the §8.5 key-order harm chain (§13's oracle is §8.5 itself, so no described test breaks).