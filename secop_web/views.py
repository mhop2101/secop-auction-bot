"""HTML for the web app. Plain server-rendered pages; no JavaScript is needed to use them.

Everything interpolated into a page goes through :func:`esc`. The token is never written
back into a page: only what the bot decodes from it (company, user, expiry).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .jobs import BOGOTA, Job, schedule_points

CSS = """
:root{--bg:#f6f7f9;--fg:#1b1f24;--muted:#5c6773;--card:#fff;--line:#d9dee4;--accent:#0b5cad;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;}
@media (prefers-color-scheme: dark){:root{--bg:#0f1216;--fg:#e6edf3;--muted:#9aa5b1;--card:#161b22;
--line:#30363d;--accent:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;
border-bottom:1px solid var(--line);background:var(--card)}
header a{color:var(--fg);text-decoration:none;font-weight:600}
main{max-width:1050px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 12px}h2{font-size:17px;margin:24px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
label{display:block;font-weight:600;margin:10px 0 4px}
.hint{color:var(--muted);font-weight:400;font-size:13px}
input[type=text],input[type=number],input[type=password],input[type=datetime-local],textarea{
width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);
font:inherit}
textarea{font-family:ui-monospace,Menlo,monospace;font-size:12px}
button,.button{display:inline-block;padding:8px 14px;border-radius:6px;border:1px solid var(--line);
background:var(--card);color:var(--fg);font:inherit;cursor:pointer;text-decoration:none;margin:4px 6px 4px 0}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{border-color:var(--bad);color:var(--bad)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
vertical-align:top}th{color:var(--muted);font-weight:600;font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;
font-size:12px;max-height:480px;white-space:pre-wrap;word-break:break-word}
.state{font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.04em}
.s-armed,.s-probing,.s-running{color:var(--warn)}.s-done{color:var(--ok)}.s-failed,.s-cancelled{color:var(--bad)}
.flash{padding:10px 12px;border-radius:6px;margin:10px 0;border:1px solid var(--line)}
.flash.error{border-color:var(--bad);color:var(--bad)}.flash.ok{border-color:var(--ok);color:var(--ok)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.kv td:first-child{color:var(--muted);width:210px}
details summary{cursor:pointer;color:var(--muted);margin-top:10px}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def bogota(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M:%S")


def iso_bogota(text: Optional[str]) -> str:
    if not text:
        return "—"
    try:
        return bogota(datetime.fromisoformat(text))
    except ValueError:
        return text


def page(title: str, body: str, user: Optional[str] = None, refresh: Optional[int] = None) -> str:
    head_extra = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    nav = ""
    if user:
        nav = (
            '<form method="post" action="/logout" class="row" style="margin:0">'
            '<span class="hint">%s</span><input type="hidden" name="csrf" value="{csrf}">'
            '<button type="submit">Log out</button></form>' % esc(user)
        )
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title>%s<style>%s</style></head><body>"
        "<header><a href=\"/\">SECOP first-bid</a>%s</header><main>%s</main></body></html>"
        % (esc(title), head_extra, CSS, nav, body)
    )


def flash(message: Optional[str], kind: str = "error") -> str:
    if not message:
        return ""
    return '<div class="flash %s">%s</div>' % (kind, esc(message))


def login_page(error: Optional[str] = None) -> str:
    body = (
        "<h1>Log in</h1>%s<div class=\"card\" style=\"max-width:380px\">"
        "<form method=\"post\" action=\"/login\">"
        "<label>User</label><input type=\"text\" name=\"username\" autocomplete=\"username\" autofocus>"
        "<label>Password</label><input type=\"password\" name=\"password\" autocomplete=\"current-password\">"
        "<p><button class=\"primary\" type=\"submit\">Log in</button></p></form></div>"
        % flash(error)
    )
    return page("Log in", body)


def _server_clock_block(clock: Dict[str, Any]) -> str:
    synced = clock.get("synchronized")
    state = "synchronised" if synced else ("NOT synchronised" if synced is False else "unknown")
    return (
        '<table class="kv"><tr><td>NTP</td><td>%s</td></tr>'
        "<tr><td>Offset from NTP</td><td>%s</td></tr>"
        "<tr><td>Time source</td><td>%s</td></tr>"
        "<tr><td>Now (Bogotá)</td><td>%s</td></tr></table>"
        % (
            esc(state),
            esc(clock.get("offset") or "—"),
            esc(clock.get("source") or "—"),
            esc(bogota(datetime.now(timezone.utc))),
        )
    )


def dashboard(jobs: List[Job], clock: Dict[str, Any], user: Optional[str] = None,
              message: Optional[str] = None, kind: str = "error") -> str:
    rows = []
    for job in jobs:
        points = schedule_points(job)
        rows.append(
            "<tr><td><a href=\"/jobs/%s\">%s</a></td><td class=\"state s-%s\">%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                esc(job.id),
                esc(job.settings.label or job.id),
                esc(job.state),
                esc(job.state + (" · " + job.busy if job.busy else "")),
                esc(bogota(points["fire"])),
                esc(", ".join(str(v) for v in job.settings.offsets_ms)),
                esc(job.company_code or ""),
            )
        )
    table = (
        "<table><tr><th>Job</th><th>State</th><th>Opening (Bogotá)</th><th>Offsets ms</th>"
        "<th>Company</th></tr>%s</table>" % "".join(rows)
        if rows
        else '<p class="hint">No jobs yet.</p>'
    )
    body = (
        "<h1>Jobs</h1>%s"
        '<div class="card"><form method="post" action="/jobs"><input type="hidden" name="csrf" value="{csrf}">'
        '<button class="primary" type="submit">New job</button></form>%s</div>'
        "<h2>This server's clock</h2><div class=\"card\">%s"
        '<p class="hint">The bot fires on this clock. SECOP\'s own clock is measured per job '
        "with the clock probe.</p></div>"
        % (flash(message, kind), table, _server_clock_block(clock))
    )
    active = any(job.state in ("armed", "probing", "running") or job.busy for job in jobs)
    return page("Jobs", body, user, refresh=15 if active else None)


def _checked(flag: bool) -> str:
    return " checked" if flag else ""


def settings_form(job: Job, editable: bool) -> str:
    s = job.settings
    disabled = "" if editable else " disabled"
    start_value = s.start_local.replace(" ", "T")[:16] if s.start_local else ""
    return (
        '<form method="post" action="/jobs/%s/settings" enctype="application/x-www-form-urlencoded">'
        '<input type="hidden" name="csrf" value="{csrf}"><fieldset style="border:0;padding:0"%s>'
        '<div class="grid">'
        "<div><label>Label</label><input type=\"text\" name=\"label\" value=\"%s\"></div>"
        "<div><label>Auction opening <span class=\"hint\">Bogotá time</span></label>"
        "<input type=\"datetime-local\" step=\"1\" name=\"start_local\" value=\"%s\"></div>"
        "<div><label>Bid value</label><input type=\"text\" name=\"bid_value\" value=\"%s\"></div>"
        "</div>"
        "<label>Offsets <span class=\"hint\">ms from SECOP's opening, comma separated</span></label>"
        "<input type=\"text\" name=\"offsets_ms\" value=\"%s\">"
        '<div class="grid">'
        "<div><label>Bids per offset <span class=\"hint\">threads, 1–5</span></label>"
        "<input type=\"number\" name=\"bids_per_offset\" min=\"1\" max=\"5\" value=\"%d\"></div>"
        "<div><label>Wait for SECOP's answer <span class=\"hint\">seconds</span></label>"
        "<input type=\"number\" name=\"read_timeout_s\" min=\"5\" max=\"300\" value=\"%d\"></div>"
        "<div><label>Arm the bot <span class=\"hint\">minutes before</span></label>"
        "<input type=\"number\" name=\"arm_minutes_before\" min=\"3\" max=\"55\" value=\"%d\"></div>"
        "</div>"
        "<label><input type=\"checkbox\" name=\"apply_clock_offset\"%s> Fire on SECOP's clock "
        "<span class=\"hint\">measure it from the run's own start request and move the bids by it</span></label>"
        "<label><input type=\"checkbox\" name=\"auto_clock_probe\"%s> Run the clock probe automatically "
        "<input type=\"number\" name=\"probe_minutes_before\" min=\"3\" max=\"55\" value=\"%d\" style=\"width:70px\"> "
        "minutes before <span class=\"hint\">writes one row in the auction log; its reading becomes the fallback</span></label>"
        "<label>Clock fallback <span class=\"hint\">ms to move the bids if the live reading fails; "
        "filled by the clock probe</span></label>"
        "<input type=\"number\" name=\"clock_fallback_ms\" min=\"-5000\" max=\"5000\" value=\"%s\">"
        "<label>Auction id <span class=\"hint\">only if the page lists several auctions</span></label>"
        "<input type=\"text\" name=\"auction_id\" value=\"%s\">"
        "<details><summary>Advanced</summary><div class=\"grid\">"
        "<div><label>Connect timeout s</label><input type=\"number\" name=\"connect_timeout_s\" value=\"%d\"></div>"
        "<div><label>Start request, s before first bid</label><input type=\"number\" name=\"start_audit_lead_s\" value=\"%d\"></div>"
        "<div><label>Keep-alives, s before first bid</label><input type=\"text\" name=\"keepalive_s\" value=\"%s\"></div>"
        "<div><label>Read SECOP's result, s after</label><input type=\"number\" name=\"record_delay_s\" value=\"%d\"></div>"
        "</div></details>"
        "<label>Auction page <span class=\"hint\">paste the saved page or just its auction row; leave empty to keep</span></label>"
        "<textarea name=\"page_html\" rows=\"4\" placeholder=\"&lt;tr id=&quot;incWidgetAuctiongrdEAuctionGrid_tr0&quot; ...\"></textarea>"
        "<label>Token <span class=\"hint\">Bearer … from the auction page; leave empty to keep</span></label>"
        "<textarea name=\"token\" rows=\"3\" placeholder=\"Bearer eyJ...\"></textarea>"
        "<p><button class=\"primary\" type=\"submit\">Save settings</button></p>"
        "</fieldset></form>"
        % (
            esc(job.id), disabled,
            esc(s.label), esc(start_value), esc(s.bid_value),
            esc(", ".join(str(v) for v in s.offsets_ms)),
            s.bids_per_offset, s.read_timeout_s, s.arm_minutes_before,
            _checked(s.apply_clock_offset), _checked(s.auto_clock_probe), s.probe_minutes_before,
            esc("" if s.clock_fallback_ms is None else s.clock_fallback_ms),
            esc(s.auction_id),
            s.connect_timeout_s, s.start_audit_lead_s,
            esc(", ".join(str(v) for v in s.keepalive_s)), s.record_delay_s,
        )
    )


def _facts(job: Job, token: Any, ref: Any, ref_problem: Optional[str]) -> str:
    points = schedule_points(job)
    rows = [
        ("Opening (Bogotá)", bogota(points["fire"])),
        ("Clock probe at", bogota(points["probe"]) if points["probe"] else "off"),
        ("Bot launches at", bogota(points["launch"])),
        ("Bids", "%d (%d offsets × %d)" % (
            len(job.settings.offsets_ms) * job.settings.bids_per_offset,
            len(job.settings.offsets_ms), job.settings.bids_per_offset)),
        ("Rows the buyer sees", "%d bids + 1 start request%s" % (
            len(job.settings.offsets_ms) * job.settings.bids_per_offset,
            " + 1 clock probe" if job.settings.auto_clock_probe else "")),
        ("Clock", "fire on SECOP's clock, fallback %s ms" % (
            "not set" if job.settings.clock_fallback_ms is None else "%+d" % job.settings.clock_fallback_ms)
            if job.settings.apply_clock_offset else "fire on this server's clock"),
    ]
    if token is not None:
        rows += [
            ("Token company", "%s (%s)" % (token.company_name or "?", token.company_code or "?")),
            ("Token user", token.user_name or "?"),
            ("Token expires (Bogotá)", bogota(token.exp)),
        ]
    else:
        rows.append(("Token", "not saved"))
    if ref is not None:
        rows += [
            ("Auction id", ref.auction_id),
            ("Auction doc", ref.eauction_doc_id or "—"),
            ("Page state", ref.state_text or "—"),
        ]
    else:
        rows.append(("Auction page", ref_problem or "not saved"))
    return '<table class="kv">%s</table>' % "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in rows
    )


def _actions(job: Job) -> str:
    jid = esc(job.id)
    form = ('<form method="post" action="/jobs/%s/action" style="display:inline">'
            '<input type="hidden" name="csrf" value="{csrf}">' % jid)
    buttons = []
    idle = not job.busy
    if job.state in ("draft", "cancelled", "failed", "done") and idle:
        buttons.append(form + '<button name="action" value="preflight">Preflight '
                       '<span class="hint">reads only</span></button></form>')
        buttons.append(form + '<button name="action" value="probe">Clock probe now '
                       '<span class="hint">1 log row</span></button></form>')
    if idle and job.state != "running":
        buttons.append(form + '<button name="action" value="record">Read SECOP result</button></form>')
    if job.state in ("armed", "probing", "running"):
        buttons.append(form + '<button class="danger" name="action" value="cancel">Cancel</button></form>')
    if job.state in ("draft", "cancelled", "failed", "done") and idle:
        buttons.append(form + '<button class="danger" name="action" value="delete">Delete job and token</button></form>')
    arm = ""
    if job.state in ("draft", "cancelled", "failed", "done") and idle:
        arm = (
            '<div class="card"><form method="post" action="/jobs/%s/action">'
            '<input type="hidden" name="csrf" value="{csrf}">'
            "<strong>Arm a live bid</strong><p class=\"hint\">The bot launches %d minutes before the opening "
            "and sends real bids of the value below. Nothing is sent before then; you can cancel until 2 s "
            "before the opening.</p>"
            '<label><input type="checkbox" name="confirm" value="yes"> I confirm real bids will be sent</label>'
            '<label>Retype the bid value</label><input type="text" name="retyped_bid" style="max-width:200px">'
            '<p><button class="primary" name="action" value="arm">Arm</button></p></form></div>'
            % (jid, job.settings.arm_minutes_before)
        )
    return '<div class="row">%s</div>%s' % ("".join(buttons), arm)


def _summary_block(summary: Optional[Dict[str, Any]]) -> str:
    if not summary:
        return '<p class="hint">No run yet.</p>'
    clock = summary.get("clock") or {}
    event_log = clock.get("event_log") or {}
    record = summary.get("secop_record") or {}
    rows = [
        ("Outcome", summary.get("outcome")),
        ("Bids sent", "%s of %s" % (summary.get("submissions_sent"), summary.get("submissions_planned"))),
        ("SECOP clock", "%s ms (±%s)" % (event_log.get("offset_ms"), event_log.get("residual_width_ms"))),
        ("Bids moved by", "%s ms" % clock.get("shift_ms") if clock.get("applied") else "not moved"),
    ]
    attempts = summary.get("attempts") or []
    rows.append(("Sent at (ms from T)", ", ".join(
        "%+.1f" % a["actual_offset_ms"] for a in attempts if a.get("actual_offset_ms") is not None) or "—"))
    rows.append(("Answers", ", ".join(
        str(a.get("status") or a.get("failure")) for a in attempts) or "—"))
    out = '<table class="kv">%s</table>' % "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in rows)
    if record:
        out += _record_block(record)
    return out


def _record_block(record: Dict[str, Any]) -> str:
    from secop_firstbid import report  # the bot's own wording for the same lines

    lines = report._secop_record_lines({"secop_record": record})
    return "<h3>SECOP says</h3><pre>%s</pre>" % esc("\n".join(lines))


def job_page(job: Job, token: Any, ref: Any, ref_problem: Optional[str],
             outputs: Dict[str, str], summary: Optional[Dict[str, Any]],
             record: Optional[Dict[str, Any]], user: Optional[str] = None,
             message: Optional[str] = None, kind: str = "error") -> str:
    editable = job.state in ("draft", "cancelled", "failed", "done") and not job.busy
    events = "".join(
        "<tr><td>%s</td><td>%s</td></tr>" % (esc(iso_bogota(e["at"])), esc(e["message"]))
        for e in reversed(job.events[-40:])
    )
    sections = []
    for title, key in (("Bot run", "run.out"), ("Clock probe", "probe.out"),
                       ("Preflight", "preflight.out"), ("SECOP record", "record.out")):
        text = outputs.get(key)
        if text:
            sections.append("<details%s><summary>%s</summary><pre>%s</pre></details>"
                            % (" open" if key == "run.out" else "", esc(title), esc(text[-20000:])))
    body = (
        "<h1>%s <span class=\"state s-%s\">%s</span></h1>%s"
        '<div class="grid"><div class="card">%s</div><div class="card">%s</div></div>'
        "<h2>Result</h2><div class=\"card\">%s%s</div>"
        "<h2>Settings</h2><div class=\"card\">%s</div>"
        "<h2>Output</h2><div class=\"card\">%s</div>"
        "<h2>Events</h2><div class=\"card\"><table>%s</table></div>"
        % (
            esc(job.settings.label or job.id), esc(job.state),
            esc(job.state + (" · " + job.busy if job.busy else "")),
            flash(message, kind),
            _facts(job, token, ref, ref_problem), _actions(job),
            _summary_block(summary),
            _record_block(record) if record else "",
            settings_form(job, editable),
            "".join(sections) or '<p class="hint">Nothing has run yet.</p>',
            events,
        )
    )
    active = job.state in ("armed", "probing", "running") or bool(job.busy)
    return page(job.settings.label or job.id, body, user, refresh=5 if active else None)


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str)
