# Web app

A logged-in web page on the bot server for setting up, arming and following an auction
attempt. It runs the bot's own commands; it does not bid by itself, and it changes nothing
about how the bot fires.

```
https://<server-ip>/        (http://<server-ip>/ redirects there)
```

The certificate is self-signed, so the browser warns once; accept it for this address. The
login is in `secrets/webapp_login.txt` on the server (and a copy in your local `secrets/`).

## What a job is

One auction attempt with one token. A job holds its settings, the pasted auction page, the
token (mode 0600 on disk, never shown back on a page), the bot config built from them, and
everything the bot printed. Jobs live in `webjobs/` on the server, which git ignores.

Several jobs can be armed at once: one per account. Jobs of the same company for the same
opening get their start requests 11 s apart automatically, so each run can find its own row
in SECOP's log.

## The page, top to bottom

**Facts.** The opening in Bogotá time, when the clock probe and the bot will run, how many
bids, how many rows the buyer will see, and what the bot reads from the token (company,
user, expiry) and the page (auction id, document, state).

**Actions.**

| Button | What it does | Sends to SECOP |
|---|---|---|
| Preflight | `secop_firstbid preflight`: token, auction state, start date, latency, `Date` clock | reads only |
| Clock probe now | `secop_firstbid clock-probe`: places SECOP's log clock against this server's; the reading becomes the clock fallback | **one** start-bidding row |
| Read SECOP result | `secop_firstbid record`: who holds the accepted bid, and which of this server's bids SECOP processed, and when | reads only |
| Arm | schedules the run (below) | bids, at the opening |
| Cancel | before launch: disarms. After launch: stops the bot, up to 2 s before the opening | — |
| Delete job and token | removes the job directory, token included | — |

**Arming** needs the confirmation ticked and the bid value retyped — this page's
equivalent of the command line's `--yes-i-mean-it`. Once armed, the scheduler:

1. runs the clock probe `probe_minutes_before` the opening (default 9) and stores its
   reading as the fallback, if *Run the clock probe automatically* is on;
2. launches `secop_firstbid run --yes-i-mean-it` `arm_minutes_before` the opening
   (default 5). From there the bot does what it always does: warm connections, one start
   request 60 s before the first bid, keep-alives at −45/−30/−15 s, the clock move, the
   bids, and SECOP's record read 90 s after the last one;
3. marks the job done when the bot exits and shows its summary.

Arming is refused less than 120 s before the opening, or with a token that expires before
the auction ends. A launch the scheduler misses (the server was down) fails the job
instead of firing late.

**Result.** The bot's outcome, SECOP's clock as measured, how far the bids were moved, when
each bid left, what each got back, and SECOP's own lines: the accepted bid with its company
and time, how many of *this server's* bids SECOP processed, and how many of the company's
bids came from other addresses (someone bidding by hand).

**Settings.** Locked while a job is armed or running.

| Setting | Default | Meaning |
|---|---|---|
| Auction opening | — | Bogotá time, to the second |
| Bid value | `0` | sent exactly as typed |
| Offsets | `-50, -20, -5, 10, 25, 50, 90` | ms from SECOP's opening, one bid each |
| Bids per offset | `1` | threads per offset, 1–5 |
| Wait for SECOP's answer | `60` s | the bot's read timeout; nothing is retried |
| Arm the bot | `5` min before | when the bot process starts |
| Fire on SECOP's clock | on | move the bids onto SECOP's clock, measured from the run's own start request |
| Run the clock probe automatically | on, `9` min before | one log row; its reading becomes the fallback |
| Clock fallback | empty | ms to move the bids if the run's own reading fails; filled by the probe |
| Auction id | empty | only when the pasted page lists several auctions |
| Advanced | | connect timeout (8 s), start request lead (60 s), keep-alives (−45, −30, −15 s), result read delay (90 s) |

**This server's clock** on the front page: whether chrony says it is synchronised, its
offset from NTP, and the time source (on EC2, Amazon's time service at 169.254.169.123).

## Deploying

On the server, from the repository:

```bash
bash deploy/install.sh
```

It creates the certificate and the login on its first run, installs
`deploy/secop-web.service` as `secop-web`, and restarts it. The service runs as the normal
user with only the right to bind ports 80 and 443. `KillMode=process`: restarting the web
app never kills a bot that is mid-run; the restarted app finds it again by its pid.

Port 443 (and 80) must be open in the EC2 security group.

Logs: `journalctl -u secop-web -f`. Each job's bot output is in `webjobs/<id>/*.out` and the
bot's run directories in `webjobs/<id>/runs/`.

## Security

- HTTPS only; HTTP redirects. Session cookie `Secure`, `HttpOnly`, `SameSite=Strict`, 12 h,
  held in memory (a restart logs everyone out).
- Every form carries a CSRF token. Five failed logins from one address lock it out for five
  minutes.
- The password is stored as a PBKDF2-SHA256 hash. To change it:
  `rm secrets/webapp_users.json secrets/webapp_login.txt && bash deploy/install.sh`.
- Tokens are written mode 0600 and never rendered; deleting a job deletes its token.
- The certificate is self-signed: it encrypts the traffic, but the browser cannot tell you
  it is talking to *your* server. Check the address before you log in.
