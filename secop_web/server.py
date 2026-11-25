"""HTTP(S) server and routes.

Routes::

    GET  /login                login form
    POST /login                check user/password, set the session cookie
    POST /logout
    GET  /                     jobs and this server's clock
    POST /jobs                 create a job
    GET  /jobs/<id>            the job: facts, actions, result, settings, output
    POST /jobs/<id>/settings   save settings, page and token
    POST /jobs/<id>/action     preflight | probe | record | arm | cancel | delete

Every page but ``/login`` needs a session; every POST needs the session's CSRF token.
The HTTP listener only redirects to HTTPS.
"""

from __future__ import annotations

import os
import re
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

from . import auth, views
from .jobs import (
    JobError,
    Runner,
    Store,
    clean_token,
    latest_summary,
    page_ref,
    record_summary,
    server_clock,
    settings_from_form,
    token_info,
    write_config,
)

MAX_BODY = 4 * 1024 * 1024
_JOB_PATH = re.compile(r"^/jobs/([0-9A-Za-z_-]+)(/settings|/action)?$")


class App:
    """Shared state for every request handler."""

    def __init__(self, store: Store, runner: Runner, user: auth.UserRecord, secure: bool) -> None:
        self.store = store
        self.runner = runner
        self.user = user
        self.secure = secure
        self.sessions = auth.Sessions()


def make_handler(app: App) -> Any:
    class Handler(BaseHTTPRequestHandler):
        server_version = "secop-web"
        sys_version = ""

        # -- plumbing -------------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # never log query strings
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: str, headers: Optional[Dict[str, str]] = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            if app.secure:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, headers: Optional[Dict[str, str]] = None) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _form(self) -> Dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise JobError("request too large")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _session(self) -> Optional[auth.Session]:
            cookies = auth.parse_cookies(self.headers.get("Cookie"))
            return app.sessions.get(cookies.get(auth.COOKIE_NAME))

        def _render(self, html: str, session: auth.Session, code: int = 200) -> None:
            self._send(code, html.replace("{csrf}", session.csrf))

        # -- routes -----------------------------------------------------------------

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/login":
                self._send(200, views.login_page())
                return
            session = self._session()
            if session is None:
                self._redirect("/login")
                return
            if path == "/":
                self._dashboard(session)
                return
            match = _JOB_PATH.match(path)
            if match and not match.group(2):
                self._job(session, match.group(1))
                return
            self._send(404, views.page("Not found", "<h1>Not found</h1>", session.username))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                form = self._form()
            except JobError as exc:
                self._send(413, views.page("Too large", views.flash(str(exc))))
                return
            if path == "/login":
                self._login(form)
                return
            session = self._session()
            if session is None:
                self._redirect("/login")
                return
            if not auth.csrf_ok(session, form.get("csrf")):
                self._send(403, views.page("Forbidden", views.flash("stale form; reload the page")))
                return
            if path == "/logout":
                app.sessions.drop(session.token)
                self._redirect("/login", {"Set-Cookie": auth.clear_cookie(app.secure)})
                return
            if path == "/jobs":
                job = app.store.create()
                self._redirect("/jobs/%s" % job.id)
                return
            match = _JOB_PATH.match(path)
            if match and match.group(2) == "/settings":
                self._save_settings(session, match.group(1), form)
                return
            if match and match.group(2) == "/action":
                self._action(session, match.group(1), form)
                return
            self._send(404, views.page("Not found", "<h1>Not found</h1>"))

        # -- handlers ---------------------------------------------------------------

        def _login(self, form: Dict[str, str]) -> None:
            address = self.client_address[0]
            if app.sessions.throttled(address):
                self._send(429, views.login_page("Too many failed attempts; wait five minutes."))
                return
            if app.user.check(form.get("username", ""), form.get("password", "")):
                app.sessions.clear_failures(address)
                session = app.sessions.create(app.user.username)
                self._redirect("/", {"Set-Cookie": auth.session_cookie(session, app.secure)})
                return
            app.sessions.record_failure(address)
            self._send(401, views.login_page("Wrong user or password."))

        def _dashboard(self, session: auth.Session, message: Optional[str] = None) -> None:
            html = views.dashboard(app.store.all(), server_clock(), session.username, message)
            self._render(html, session)

        def _job(self, session: auth.Session, job_id: str, message: Optional[str] = None,
                 kind: str = "error", code: int = 200) -> None:
            try:
                job = app.store.load(job_id)
            except JobError:
                self._send(404, views.page("Not found", "<h1>No such job</h1>"))
                return
            ref, problem = page_ref(app.store, job)
            outputs = {name: app.store.read_text(job_id, name)
                       for name in ("run.out", "probe.out", "preflight.out", "record.out")}
            html = views.job_page(
                job, token_info(app.store, job_id), ref, problem, outputs,
                latest_summary(app.store, job_id), record_summary(app.store, job_id),
                session.username, message, kind,
            )
            self._render(html, session, code)

        def _save_settings(self, session: auth.Session, job_id: str, form: Dict[str, str]) -> None:
            try:
                job = app.store.load(job_id)
                if job.state not in ("draft", "cancelled", "failed", "done") or job.busy:
                    raise JobError("settings are locked while the job is %s" % (job.busy or job.state))
                settings = settings_from_form(form, job.settings)
                page_html = (form.get("page_html") or "").strip()
                token_text = (form.get("token") or "").strip()
                token = clean_token(token_text) if token_text else None
                if page_html:
                    app.store.write_private(job_id, "page.html", page_html)
                if token:
                    app.store.write_private(job_id, "token.txt", token)

                def apply(j: Any) -> None:
                    j.settings = settings
                    if j.state in ("cancelled", "failed", "done"):
                        j.state = "draft"
                    j.note("settings saved")

                job = app.store.update(job_id, apply)
                warnings = []
                try:
                    _path, warnings = write_config(app.store, job)
                except JobError:
                    pass  # incomplete until page, token and time are all in
                self._job(session, job_id, "Saved." + (
                    " Bot warnings: " + " | ".join(w.split(":")[0] for w in warnings) if warnings else ""),
                    "ok")
            except JobError as exc:
                self._job(session, job_id, str(exc), code=400)

        def _action(self, session: auth.Session, job_id: str, form: Dict[str, str]) -> None:
            action = form.get("action", "")
            try:
                if action == "preflight":
                    app.runner.preflight(job_id)
                    message = "Preflight started (reads only, no bid)."
                elif action == "probe":
                    app.runner.clock_probe(job_id)
                    message = "Clock probe started (one start-bidding row in the auction log)."
                elif action == "record":
                    app.runner.read_record(job_id)
                    message = "Reading SECOP's record (reads only)."
                elif action == "arm":
                    app.runner.arm(job_id, form.get("confirm") == "yes", form.get("retyped_bid", ""))
                    message = "Armed."
                elif action == "cancel":
                    app.runner.cancel(job_id)
                    message = "Cancelled."
                elif action == "delete":
                    job = app.store.load(job_id)
                    if job.state in ("armed", "probing", "running") or job.busy:
                        raise JobError("cancel the job first")
                    app.store.delete(job_id)
                    self._redirect("/")
                    return
                else:
                    raise JobError("unknown action")
                self._job(session, job_id, message, "ok")
            except JobError as exc:
                self._job(session, job_id, str(exc), code=400)

    return Handler


def redirect_handler(https_port: int) -> Any:
    class Redirect(BaseHTTPRequestHandler):
        server_version = "secop-web"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _go(self) -> None:
            host = (self.headers.get("Host") or "").split(":")[0] or "localhost"
            target = "https://%s%s/" % (host, "" if https_port == 443 else ":%d" % https_port)
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _go
        do_POST = _go
        do_HEAD = _go

    return Redirect


def serve(
    data_root: str,
    users_file: str,
    host: str = "0.0.0.0",
    https_port: int = 443,
    http_port: Optional[int] = 80,
    cert: Optional[str] = None,
    key: Optional[str] = None,
) -> None:
    store = Store(data_root)
    runner = Runner(store)
    secure = bool(cert and key)
    app = App(store, runner, auth.load_user_file(users_file), secure)
    main = ThreadingHTTPServer((host, https_port), make_handler(app))
    main.daemon_threads = True
    if secure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        main.socket = context.wrap_socket(main.socket, server_side=True)
    runner.start()
    if http_port and secure and http_port != https_port:
        redirect = ThreadingHTTPServer((host, http_port), redirect_handler(https_port))
        redirect.daemon_threads = True
        threading.Thread(target=redirect.serve_forever, name="http-redirect", daemon=True).start()
    sys.stderr.write("secop-web on %s://%s:%d (data %s)\n"
                     % ("https" if secure else "http", host, https_port, os.path.abspath(data_root)))
    try:
        main.serve_forever()
    finally:
        runner.stop()


__all__ = ["App", "make_handler", "redirect_handler", "serve"]

