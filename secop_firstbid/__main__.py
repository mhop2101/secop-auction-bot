"""Command-line entry point: the four subcommands of plan §13 plus ``clock-probe``, and the
one place a :class:`Failure` becomes a process exit code.

``parse-page`` and ``plan`` never open a socket. ``plan`` is the only rehearsal in this app
with no server-side footprint at all — it builds the schedule, the payload and the audit
accounting from the saved page and the config alone, so it constructs no
:class:`~secop_firstbid.session.HttpClient` rather than merely declining to send.
``run --set dry_run=true`` is a different thing: it skips ``CreateBid`` but still writes
buyer-visible rows. ``clock-probe`` writes exactly one: a start-bidding row, read back to
place the clock SECOP stamps its log with.

``--yes-i-mean-it`` is the only source of ``confirm_live``. A config file that sets it is
reported and overruled, so arming a live bid cannot become a property of a file somebody
edited last week.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import sys
import time
import traceback
import warnings
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Filed before `requests` pulls in urllib3, which emits the warning on import — from
# `main()` it would already be too late. Only the CLI touches the warning filters;
# importing the package must not. The macOS system Python links LibreSSL, so urllib3
# warns on every invocation about something neither the operator nor this app can act
# on, directly above the schedule and footprint lines that have to be read carefully.
warnings.filterwarnings("ignore", message=r".*OpenSSL 1\.1\.1.*")

import requests  # noqa: E402

from . import clock, endpoints, numbers, page_parse, records, report
from .auth import TokenInfo, inspect_token, preflight_token
from .bidder import (
    FireOutcome,
    StartAudit,
    first_release_mono_ns,
    require_live_confirmation,
    run_schedule,
)
from .config import (
    CLOCK_FALLBACK_DISAGREE_MS,
    Config,
    audit_footprint,
    load,
    start_audit_planned,
    validate,
)
from .detail import baseline_note, build_detail, fetch_detail
from .errors import Failure, SecopError
from .gate import GateVerdict, run_gate
from .logging_setup import RunLogger
from .models import (
    AuctionDetail,
    AuctionRef,
    BidPayload,
    ClockEstimate,
    ClockSample,
    ItemMeta,
    Wave,
)
from .payload import (
    SUPPORTED_AUCTION_VERSION,
    BuildReport,
    build_payload,
    js_iso_now,
)
from .schedule import (
    Armed,
    arm,
    build_waves,
    check_fire_time_vs_start_date,
    parse_fire_time,
    schedule_span_ms,
    shift_t,
    sleep_until_mono,
)
from .session import HttpClient, keep_warm, prewarm

PROG = "secop-firstbid"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_PARSE = 3
EXIT_AUTH = 4
EXIT_ALL_REJECTED = 5
EXIT_NETWORK = 6

# The taxonomy is wider than the six documented exit codes, so codes without one of their
# own join the family an operator would act on the same way: re-read the config, re-save the
# page, re-harvest the token, re-price the bid, or wait for the network.
EXIT_BY_FAILURE = {
    Failure.OK: EXIT_OK,
    Failure.CONFIG_INVALID: EXIT_CONFIG,
    Failure.FIRE_TIME_PAST: EXIT_CONFIG,
    Failure.FIRE_TIME_TOO_FAR: EXIT_CONFIG,
    Failure.UNSUPPORTED_AUCTION_VERSION: EXIT_CONFIG,
    Failure.PARSE_FAILED: EXIT_PARSE,
    Failure.PARSE_AMBIGUOUS: EXIT_PARSE,
    Failure.MALFORMED_PAYLOAD: EXIT_PARSE,
    Failure.AUTH_EXPIRED: EXIT_AUTH,
    Failure.AUTH_EXPIRING: EXIT_AUTH,
    Failure.BID_ERR_MIN: EXIT_ALL_REJECTED,
    Failure.VALIDATION_FAILED: EXIT_ALL_REJECTED,
    Failure.DUPLICATE: EXIT_ALL_REJECTED,
    Failure.STATE_NOT_STARTED: EXIT_ALL_REJECTED,
    Failure.STATE_PAUSED: EXIT_ALL_REJECTED,
    Failure.STATE_FINISHED: EXIT_ALL_REJECTED,
    Failure.STATE_UNKNOWN: EXIT_ALL_REJECTED,
    Failure.NETWORK: EXIT_NETWORK,
    Failure.RATE_LIMIT: EXIT_NETWORK,
    Failure.WAF_BLOCK: EXIT_NETWORK,
    Failure.CLOCK_SYNC_FAILED: EXIT_NETWORK,
    Failure.UNKNOWN: EXIT_ERROR,
}  # type: Dict[Failure, int]

PLACEHOLDER_ITEM_ID = "<itemId from GetAuction>"
PLACEHOLDER_QUANTITY = "1.000000"
PLACEHOLDER_CURRENCY = "COP"

BUILD_STAGE = "build"
CLOCK_STAGE = "clock"

# On top of clock_window_ms and the gate's lead: what moving T needs to leave between the
# decision and the first release, so every thread wakes into the new schedule.
_MOVE_T_SETTLE_MS = 1000

# How long the run waits on the start audit's thread after the schedule, on top of the
# audit's own request budget: two log reads and the pause between them.
_AUDIT_JOIN_MARGIN_S = 5.0
CONFIG_FILENAME = "config.json"
PAYLOAD_FILENAME = "payload.json"

PROVISIONAL_SOURCE = "provisional (no saved bodies)"
FETCHED_SOURCE = "GetAuctionSimple + GetAuction fetched by this run"

PROVISIONAL_PAYLOAD_NOTE = (
    "PROVISIONAL — built with no server. Offline there is no GetAuction, so the per-item "
    "fields below are placeholders and will differ on the wire: itemId, itemUnit, "
    "itemInternalReference, lotInternalId, lotName, and itemQuantityUnitPrice (0 because "
    "the item's initial unit price is unknown). currency is %r, the only value ever "
    "observed, and quantity is assumed %s, the only quantity ever captured — so totalValue "
    "is right only if the real quantity is 1. Pass --auction-simple/--auction with response "
    "bodies saved from a previous run's http/ directory to build the real payload."
    % (PLACEHOLDER_CURRENCY, PLACEHOLDER_QUANTITY)
)

_FIELD_TYPES = dict(
    (field.name, field.type) for field in dataclasses.fields(Config)
)  # type: Dict[str, Any]

_TRUE_WORDS = ("true", "yes", "on", "1")
_FALSE_WORDS = ("false", "no", "off", "0")
_NULL_WORDS = ("", "null", "none")


def exit_code_for(failure: Failure) -> int:
    """The process exit code for one taxonomy tag. The single mapping, used everywhere."""
    return EXIT_BY_FAILURE.get(failure, EXIT_ERROR)


def _out(line: str = "") -> None:
    print(line)


def _warn(line: str) -> None:
    print("WARNING: %s" % line)


def _print_warnings(messages: Sequence[str]) -> None:
    for message in messages:
        _warn(message)


def _config_error(message: str, detail: Optional[Dict[str, Any]] = None) -> SecopError:
    return SecopError(Failure.CONFIG_INVALID, message, detail)


def _coerce_override(key: str, text: str) -> Any:
    kind = _FIELD_TYPES[key]
    lowered = text.strip().lower()
    if kind == "bool":
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise _config_error(
            "--set %s=%r is not a boolean; write one of %s or %s"
            % (key, text, "/".join(_TRUE_WORDS), "/".join(_FALSE_WORDS))
        )
    if kind == "int":
        try:
            return int(text.strip(), 10)
        except ValueError:
            raise _config_error("--set %s=%r is not an integer" % (key, text))
    if kind == "str":
        return text
    if kind == "Optional[str]":
        return None if lowered in _NULL_WORDS else text
    if kind == "Optional[int]":
        if lowered in _NULL_WORDS:
            return None
        try:
            return int(text.strip(), 10)
        except ValueError:
            raise _config_error("--set %s=%r is not an integer or null" % (key, text))
    if kind == "List[int]":
        parts = [part.strip() for part in text.split(",") if part.strip()]
        try:
            return [int(part, 10) for part in parts]
        except ValueError:
            raise _config_error(
                "--set %s=%r is not a comma-separated list of integers, e.g. "
                "-100,-50,-20,0,20" % (key, text)
            )
    raise _config_error(
        "config key %r is annotated %r and --set has no rule for that type; set it in the "
        "config file instead" % (key, kind)
    )


def parse_set_overrides(pairs: Optional[Sequence[str]]) -> Dict[str, Any]:
    """Turn ``--set key=value`` strings into typed config overrides.

    An unknown key is fatal rather than ignored: a silently dropped override leaves the run
    armed with the file's value, which is exactly the failure a typo in ``fire_time`` must
    not be allowed to produce.
    """
    out = {}  # type: Dict[str, Any]
    for pair in pairs or []:
        key, separator, text = pair.partition("=")
        key = key.strip()
        if not separator:
            raise _config_error(
                "--set expects KEY=VALUE, got %r; e.g. --set dry_run=true" % (pair,)
            )
        if key not in _FIELD_TYPES:
            close = difflib.get_close_matches(key, list(_FIELD_TYPES), n=1)
            raise _config_error(
                "--set names unknown config key %r%s. It is refused rather than ignored: "
                "an ignored override would arm the run with the config file's value."
                % (key, (" (did you mean %r?)" % close[0]) if close else ""),
                {"unknown_key": key},
            )
        out[key] = _coerce_override(key, text)
    return out


def _resolve_confirm_live(cfg: Config, yes_i_mean_it: bool) -> None:
    if yes_i_mean_it:
        cfg.confirm_live = True
        return
    if cfg.confirm_live:
        _warn(
            "confirm_live is set in the config but --yes-i-mean-it is not on the command "
            "line: it is overruled and left False. The flag is the only thing that arms a "
            "real CreateBid, so that a stale file cannot arm one."
        )
    cfg.confirm_live = False


def _load_config(args: argparse.Namespace) -> Tuple[Config, List[str]]:
    """Config file + ``--set`` overrides + the ``--yes-i-mean-it`` resolution, validated."""
    cfg = load(args.config, parse_set_overrides(getattr(args, "set_pairs", None)))
    if hasattr(args, "yes_i_mean_it"):
        _resolve_confirm_live(cfg, bool(args.yes_i_mean_it))
    return (cfg, validate(cfg))


def _read_json_body(path: str, what: str) -> Dict[str, Any]:
    """Load a saved response body, accepting a run directory's logged exchange wrapper."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _config_error("cannot read %s snapshot %s: %s" % (what, path, exc))
    if isinstance(data, dict) and isinstance(data.get("response_body"), dict):
        data = data["response_body"]
    if not isinstance(data, dict):
        raise _config_error(
            "%s snapshot %s is not a JSON object; pass the response body, or the "
            "http/NNN_*.json exchange file that contains it" % (what, path)
        )
    return data


def _offset_label(offset_ms: float) -> str:
    if offset_ms == 0:
        return "T"
    return "T%+.1f ms" % offset_ms


def _countdown(target: datetime, now: Optional[datetime] = None) -> str:
    seconds = (target - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds < 0:
        return "%.1f s in the past" % (-seconds,)
    if seconds < 90:
        return "in %.1f s" % seconds
    return "in %dm %02ds" % (int(seconds) // 60, int(seconds) % 60)


def _schedule_lines(cfg: Config, waves: Sequence[Wave], t_wall: datetime) -> List[str]:
    lines = [
        "schedule   %s, %d wave(s), span %.1f ms, lead_ms %d, validate_lead_ms %d"
        % (
            cfg.mode,
            len(waves),
            schedule_span_ms(list(waves)),
            cfg.lead_ms,
            cfg.validate_lead_ms,
        ),
        "audit      %s"
        % (
            "one start audit for the run, %d ms before the first release, on a "
            "connection no bid uses" % cfg.start_audit_lead_ms
            if start_audit_planned(cfg, list(waves))
            else "no start audit"
        ),
        "keep-alive %s"
        % (
            "every bid connection re-read at %s ms from the first release (%d ms timeout)"
            % (
                ", ".join("%d" % offset for offset in sorted(cfg.keepalive_offsets_ms)),
                cfg.keepalive_timeout_ms,
            )
            if cfg.keepalive_offsets_ms
            else "off — connections warmed at arming are left to idle"
        ),
    ]
    if cfg.apply_clock_offset:
        lines.append(
            "clock      T will be moved onto SECOP's log clock if the start audit's row "
            "is read back in time; the release times below are before any such move"
        )
    for wave in waves:
        release = t_wall + timedelta(milliseconds=wave.offset_ms - cfg.lead_ms)
        lines.append(
            "  wave %-2d  %-14s  release %s  D%d V%d P%d"
            % (
                wave.index,
                _offset_label(wave.offset_ms),
                release.isoformat(),
                wave.direct,
                wave.validate,
                wave.validate_only,
            )
        )
    return lines


def _footprint_line(cfg: Config, waves: Sequence[Wave]) -> str:
    footprint = audit_footprint(cfg, list(waves))
    return (
        "footprint  %d CreateBid submission(s), %d audited request(s) — every audited "
        "request is a row the auction owner can read, carrying this host's public IP and "
        "the token's company code"
        % (footprint["submissions"], footprint["audited_requests"])
    )


def _run_headline(cfg: Config) -> str:
    if cfg.dry_run:
        return "DRY RUN — no CreateBid; every ValidateBid still writes an audit row"
    if cfg.confirm_live:
        return "LIVE RUN — a real CreateBid WILL be sent"
    return (
        "NOT ARMED — confirm_live is False; the run stops at the fire stage "
        "(--yes-i-mean-it to send, --set dry_run=true to rehearse)"
    )


def _banner(
    cfg: Config,
    ref: AuctionRef,
    detail: Optional[AuctionDetail],
    payload: Optional[BidPayload],
    waves: Sequence[Wave],
    t_wall: datetime,
    headline: str,
) -> str:
    rule = "=" * 78
    title = None if detail is None else detail.title
    total = None if payload is None else payload.total_value
    items = 0 if payload is None else len(payload.items)

    lines = [rule, headline, ""]
    lines.append(
        "auction    %s%s"
        % (ref.auction_id, "  " + ref.eauction_doc_id if ref.eauction_doc_id else "")
    )
    if title:
        lines.append("           %s" % title)
    if detail is not None and detail.test_auction:
        lines.append("           testAuction=true")
    lines.append(
        "bid value  %s%s"
        % (
            cfg.bid_value,
            ""
            if total is None
            else "  ->  totalValue %s across %d item(s)" % (total, items),
        )
    )
    lines.append(
        "fire time  %s  (%s)" % (t_wall.isoformat(), _countdown(t_wall))
    )
    lines.extend(_schedule_lines(cfg, waves, t_wall))
    lines.append(_footprint_line(cfg, waves))
    unverified = report.unverified_routes(cfg, ref, list(waves))
    if unverified:
        lines.append(
            "unverified %s — never seen on the wire; a casing miss is a 404 at T"
            % ", ".join(unverified)
        )
    lines.append(rule)
    return "\n".join(lines)


def _print_payload(payload: BidPayload, build_report: BuildReport) -> None:
    _out(json.dumps(payload.to_dict(), indent=2, ensure_ascii=False))
    _out(
        "%d bytes on the wire (compact JSON); this Date was stamped when the payload was "
        "built, and a run re-stamps it and re-serializes once at arming, so the bytes it "
        "sends carry a later value than these" % len(payload.to_bytes())
    )
    _print_warnings(build_report.warnings)
    if build_report.inexact_items:
        _warn(
            "itemQuantityUnitPrice was rounded for item(s) %s; the server's rounding mode "
            "for that division is undocumented"
            % ", ".join(build_report.inexact_items)
        )


def _build_report_detail(build_report: BuildReport) -> Dict[str, Any]:
    return {
        "invariant_ok": build_report.invariant_ok,
        "warnings": list(build_report.warnings),
        "inexact_items": list(build_report.inexact_items),
        "residuals": dict(build_report.residuals),
        "excluded_items": list(build_report.excluded_items),
        "excluded_total_value": build_report.excluded_total_value,
    }


def _log_payload_built(
    logger: RunLogger, payload: BidPayload, build_report: BuildReport, source: str
) -> None:
    """Put the build report where a run under cron or nohup can still be read.

    The excluded items, the unverified quantity factor and the inexact divisions are what
    an operator needs after the fact, and stdout is the one sink a scheduled run does not
    keep.
    """
    detail = {
        "payload_source": source,
        "auction_id": payload.auction_id,
        "currency": payload.currency,
        "items": len(payload.items),
        "total_value": payload.total_value,
        "payload_bytes": len(payload.to_bytes()),
    }  # type: Dict[str, Any]
    detail.update(_build_report_detail(build_report))
    logger.event(
        stage=BUILD_STAGE,
        event="payload_built",
        level="WARNING" if build_report.warnings else "INFO",
        detail=detail,
    )


def _write_payload(
    logger: RunLogger, payload: BidPayload, build_report: BuildReport, source: str
) -> None:
    """Write what would have been bid into the run directory.

    ``wire`` carries the serialized bytes rather than a nested object because the run
    directory's whole-file sink sorts keys, and the wire key order is fixed: an object here
    would be recorded in an order the server never sees.
    """
    wire = payload.to_bytes()
    record = {
        "payload_source": source,
        "wire": wire.decode("utf-8"),
        "wire_bytes": len(wire),
    }  # type: Dict[str, Any]
    record.update(_build_report_detail(build_report))
    logger.write_json(PAYLOAD_FILENAME, record)


def _token_claims(info: TokenInfo) -> Dict[str, Any]:
    """The decoded, non-secret half of the token, to sit beside the redacted config.

    :class:`TokenInfo` holds no raw token, so no field read here can be one.
    """
    return {
        "exp": None if info.exp is None else info.exp.isoformat(),
        "nbf": None if info.nbf is None else info.nbf.isoformat(),
        "seconds_remaining": info.seconds_remaining,
        "aud": info.aud,
        "iss": info.iss,
        "company_code": info.company_code,
        "company_name": info.company_name,
        "user_id": info.user_id,
        "user_name": info.user_name,
        "login_guid": info.login_guid,
        "claim_names": list(info.raw_claim_names),
    }


def _provisional_detail() -> AuctionDetail:
    """The stand-in auction ``plan`` builds a payload from when it has no saved bodies."""
    item = ItemMeta(
        item_id=PLACEHOLDER_ITEM_ID,
        quantity=Decimal(PLACEHOLDER_QUANTITY),
        item_unit="",
        item_internal_reference="",
        lot_internal_id="",
        lot_name="",
        initial_value=Decimal(0),
        quantity_str=PLACEHOLDER_QUANTITY,
    )
    return AuctionDetail(
        version_code=SUPPORTED_AUCTION_VERSION,
        type_code="",
        currency=PLACEHOLDER_CURRENCY,
        round_type="",
        decimal_num=0,
        total_num=0,
        minimum_bid_type=None,
        start_price=Decimal(0),
        start_date=None,
        end_date=None,
        state_code=None,
        exec_state_code=None,
        allow_bids_below_best=False,
        test_auction=False,
        items=[item],
    )


def _offline_detail(args: argparse.Namespace) -> Tuple[AuctionDetail, Optional[str]]:
    """``(detail, provenance)``; provenance is ``None`` when the detail is the stand-in."""
    if args.auction_simple is None and args.auction is None:
        return (_provisional_detail(), None)
    if args.auction_simple is None or args.auction is None:
        raise _config_error(
            "--auction-simple and --auction go together: GetAuctionSimple carries the "
            "auctionVersionCode and currencyCode, GetAuction carries the items, and a "
            "payload needs both"
        )
    detail = build_detail(
        _read_json_body(args.auction_simple, "GetAuctionSimple"),
        _read_json_body(args.auction, "GetAuction"),
        None,
    )
    return (detail, args.auction_simple + " + " + args.auction)


def cmd_parse_page(args: argparse.Namespace) -> int:
    """Stage 1 only: resolve the auction from a saved page. No config, no network, no logs."""
    ref = page_parse.parse_auction_page(args.page, args.auction_id)
    _out("page           %s" % args.page)
    _out("auction_id     %s" % ref.auction_id)
    _out("extractor      %s" % ref.source_extractor)
    _out("language       %s%s" % (ref.lang, "  (defaulted)" if ref.lang_defaulted else ""))
    _out(
        "market         %s%s"
        % (ref.market, "  (defaulted)" if ref.market_defaulted else "")
    )
    _out("doc id         %s" % (ref.eauction_doc_id or "<absent>"))
    _out("redirect path  %s" % (ref.redirect_path or "<absent>"))
    _out("mkey           %s" % ("present" if ref.mkey else "absent"))
    if ref.state_text:
        _out("state text     %s" % ref.state_text)
    else:
        _out(
            "state text     <absent> — the page carries no state cell, so this auction's "
            "state is UNKNOWN, which is not the same as 'not finished'"
        )
    _out("referer        %s" % endpoints.referer(ref))
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    """Rehearse the whole run offline: schedule, payload, footprint. Constructs no client."""
    cfg, notes = _load_config(args)
    _print_warnings(notes)

    ref = page_parse.parse_auction_page(cfg.auction_page_html_path, cfg.auction_id)
    waves = build_waves(cfg)
    t_wall = parse_fire_time(cfg.fire_time)
    bid_value = numbers.parse_user_decimal(cfg.bid_value)
    detail, provenance = _offline_detail(args)
    payload, build_report = build_payload(ref, detail, bid_value)

    source = provenance or PROVISIONAL_SOURCE

    logger = RunLogger(cfg)
    try:
        logger.event(
            stage="schedule",
            event="schedule_planned",
            detail={
                "auction_id": ref.auction_id,
                "mode": cfg.mode,
                "fire_time": t_wall.isoformat(),
                "waves": [
                    {
                        "index": wave.index,
                        "offset_ms": wave.offset_ms,
                        "direct": wave.direct,
                        "validate": wave.validate,
                        "validate_only": wave.validate_only,
                    }
                    for wave in waves
                ],
                "audit_footprint": audit_footprint(cfg, waves),
                "payload_source": source,
                "offline": True,
            },
        )
        _log_payload_built(logger, payload, build_report, source)
        _write_payload(logger, payload, build_report, source)

        # A provisional payload's own warnings describe the stand-in item, not the auction,
        # so the summary carries the note that the payload was a stand-in instead. The
        # payload_built event above keeps them verbatim either way.
        build_notes = (
            list(build_report.warnings) if provenance else [PROVISIONAL_PAYLOAD_NOTE]
        )
        summary = report.build_summary(
            cfg,
            run_id=logger.run_id,
            ref=ref,
            detail=detail if provenance else None,
            payload=payload if provenance else None,
            waves=waves,
            warnings=notes + build_notes,
            started_at=logger.started_at,
        )
        report.write_summary(logger, summary)

        _out("run directory  %s" % logger.run_dir)
        _out()
        _out(
            _banner(
                cfg,
                ref,
                detail if provenance else None,
                payload,
                waves,
                t_wall,
                "PLAN — offline rehearsal; nothing is sent and no audit row is written",
            )
        )
        _out()
        _out(
            "--- PAYLOAD ---"
            if provenance is None
            else "--- PAYLOAD (from %s) ---" % provenance
        )
        if provenance is None:
            _warn(PROVISIONAL_PAYLOAD_NOTE)
        _print_payload(payload, build_report)
        _out()
        _out(
            "plan is offline: no request left this process, so no row was written into the "
            "auction's event log. A dry run is not the same thing."
        )
    finally:
        logger.close()
    return EXIT_OK


def _clock_diagnostics(
    cfg: Config, ref: AuctionRef, client: HttpClient, logger: RunLogger
) -> Tuple[Optional[ClockEstimate], List[float]]:
    """``rtt_samples`` Date-header samples, intersected. Diagnostics; ``T`` never moves."""
    if cfg.clock_probe in ("signalr", "both"):
        try:
            clock.probe_signalr()
        except NotImplementedError as exc:
            _warn(str(exc))
    if cfg.clock_probe in ("none", "signalr"):
        return (None, [])

    path = endpoints.get_auction_simple(ref.auction_id)
    samples = []  # type: List[ClockSample]
    elapsed_ms = []  # type: List[float]
    for index in range(max(cfg.rtt_samples, 0)):
        # The wall clock is read outside the call, so each Marzullo interval is wider than
        # the true one rather than tighter: a conservative bound stays a bound.
        t_send_wall = datetime.now(timezone.utc)
        outcome = client.get(
            path, stage="preflight", thread_id="clock-%02d" % index, role="clock"
        )
        t_recv_wall = datetime.now(timezone.utc)
        if outcome.rtt_ms is not None:
            elapsed_ms.append(outcome.rtt_ms)
        samples.append(
            ClockSample(
                date_header=outcome.date_header,
                t_send_mono_ns=outcome.t_send_mono_ns,
                t_recv_mono_ns=outcome.t_recv_mono_ns or outcome.t_send_mono_ns,
                t_send_wall=t_send_wall,
                t_recv_wall=t_recv_wall,
                endpoint="GetAuctionSimple",
            )
        )

    estimate = clock.marzullo(samples)
    logger.event(
        stage="preflight",
        event="clock_offset",
        level="WARNING" if estimate.offset_ms is None else "INFO",
        detail={
            "source": estimate.source,
            "samples": estimate.samples,
            "lo_ms": estimate.lo_ms,
            "hi_ms": estimate.hi_ms,
            "offset_ms": estimate.offset_ms,
            "residual_width_ms": estimate.width_ms,
            "empty_intersection": estimate.empty_intersection,
            "applied_to_t": False,
            "note": "diagnostic only: T is the operator-supplied fire_time and no "
            "measurement here moves it",
        },
    )
    return (estimate, elapsed_ms)


def _check_token(cfg: Config, logger: RunLogger) -> TokenInfo:
    """Print the token's identity and lifetime, then refuse a dead or dying one."""
    info = inspect_token(cfg.jwt)
    logger.event(
        stage="preflight",
        event="token",
        detail={
            "exp": None if info.exp is None else info.exp.isoformat(),
            "seconds_remaining": info.seconds_remaining,
            "company_code": info.company_code,
            "company_name": info.company_name,
            "user_id": info.user_id,
            "login_guid": info.login_guid,
            "claims": len(info.raw_claim_names),
        },
    )
    # Rewritten rather than appended to: the run directory holds one config record, and the
    # claims are what tie it to the company code on every audit row the run produces.
    snapshot = dataclasses.asdict(cfg)
    snapshot["token_claims"] = _token_claims(info)
    logger.write_json(CONFIG_FILENAME, snapshot)

    _out("--- TOKEN ---")
    _out("company        %s (%s)" % (info.company_name or "?", info.company_code or "?"))
    _out("user           %s (%s)" % (info.user_name or "?", info.user_id or "?"))
    _out("expires        %s" % (info.exp.isoformat() if info.exp else "<no exp claim>"))
    if info.seconds_remaining is not None:
        _out(
            "remaining      %.0f s (safety margin %d s)"
            % (info.seconds_remaining, cfg.auth_safety_margin_s)
        )
    _out(
        "company_code is the number the server writes on every buyer-visible audit row "
        "this run produces."
    )
    preflight_token(info, cfg.auth_safety_margin_s)
    return info


def _report_header_divergence(logger: RunLogger) -> None:
    """Name every header that does not reproduce the capture, or say that none does."""
    divergence = endpoints.header_divergence()
    _out("--- HEADERS ---")
    if not divergence:
        _out("every header this run sends matches the captured browser request")
    for name, (sent, captured) in divergence.items():
        _out("%-14s sent %s" % (name, sent))
        _out("%-14s capture %s" % ("", captured))
    if "Accept-Encoding" in divergence:
        _out(
            "Accept-Encoding advertises only what this install can decode. A coding the "
            "server honours and this process cannot decode makes every response body "
            "unreadable, which is certain; a WAF cross-checking it against the spoofed "
            "User-Agent is speculative — no 403 was ever observed. Install brotli and "
            "zstandard to close the gap."
        )
    logger.event(
        stage="preflight",
        event="header_divergence",
        detail=dict(
            (name, {"sent": sent, "captured": captured})
            for name, (sent, captured) in divergence.items()
        ),
    )


def _print_detail(detail: AuctionDetail) -> None:
    _out("--- AUCTION ---")
    _out("title          %s" % (detail.title or "?"))
    _out("version        %s" % detail.version_code)
    _out("currency       %s" % detail.currency)
    _out("state          %s / %s" % (detail.state_code, detail.exec_state_code))
    _out(
        "window         %s -> %s (timeZoneCode %s)"
        % (
            detail.start_date.isoformat() if detail.start_date else "?",
            detail.end_date.isoformat() if detail.end_date else "?",
            detail.time_zone_code,
        )
    )
    _out("start price    %s" % numbers.to_wire(detail.start_price))
    _out("test auction   %s" % detail.test_auction)
    _out("items          %d" % len(detail.items))
    _out(
        "my last bid    %s   last bid %s"
        % (
            "?" if detail.my_last_bid is None else numbers.to_wire(detail.my_last_bid),
            "?" if detail.last_bid is None else numbers.to_wire(detail.last_bid),
        )
    )
    note = baseline_note(detail)
    if note:
        _warn(note)


def cmd_preflight(args: argparse.Namespace) -> int:
    """Token, detail, RTT and clock diagnostics. Sends no bid and no audit POST."""
    cfg, notes = _load_config(args)
    _print_warnings(notes)

    ref = page_parse.parse_auction_page(cfg.auction_page_html_path, cfg.auction_id)
    waves = build_waves(cfg)
    t_wall = parse_fire_time(cfg.fire_time)
    bid_value = numbers.parse_user_decimal(cfg.bid_value)

    logger = RunLogger(cfg)
    client = None  # type: Optional[HttpClient]
    try:
        _out("run directory  %s" % logger.run_dir)
        _out()
        _check_token(cfg, logger)
        _report_header_divergence(logger)

        client = HttpClient(cfg, ref, requests.cookies.RequestsCookieJar(), logger)
        detail = fetch_detail(client, ref, logger)
        _out()
        _print_detail(detail)

        payload, build_report = build_payload(ref, detail, bid_value)
        _log_payload_built(logger, payload, build_report, FETCHED_SOURCE)
        _write_payload(logger, payload, build_report, FETCHED_SOURCE)
        notes.extend(build_report.warnings)
        _out()
        _out("--- PAYLOAD ---")
        _print_payload(payload, build_report)

        mismatch = check_fire_time_vs_start_date(
            t_wall, detail.start_date, cfg.fire_time_mismatch_warn_s
        )
        if mismatch:
            _warn(mismatch)

        estimate, elapsed_ms = _clock_diagnostics(cfg, ref, client, logger)
        _out()
        _out("--- TIMING ---")
        span_ms = schedule_span_ms(waves)
        _out("schedule span  %.1f ms" % span_ms)
        if elapsed_ms:
            stats = clock.rtt_stats(elapsed_ms)
            _out(
                "GetAuctionSimple elapsed ms: n=%d min=%.1f p50=%.1f p95=%.1f max=%.1f"
                % (stats["n"], stats["min"], stats["p50"], stats["p95"], stats["max"])
            )
            _out(
                "advisory lead_ms %.1f — elapsed time, not the server-side wait the "
                "recommendation is defined on, and never applied on its own (lead_ms is %d)"
                % (clock.recommended_lead_ms(elapsed_ms), cfg.lead_ms)
            )
        if estimate is not None:
            _out(
                "Date offset    midpoint %s ms, residual width %s ms over %d sample(s)"
                % (estimate.offset_ms, estimate.width_ms, estimate.samples)
            )
            guard = clock.clock_guard(estimate, span_ms)
            if guard:
                _warn(guard)
            _out(
                "T is the operator-supplied fire_time; nothing measured here moved it "
                "(apply_clock_offset=%s)." % cfg.apply_clock_offset
            )

        summary = report.build_summary(
            cfg,
            run_id=logger.run_id,
            ref=ref,
            detail=detail,
            payload=payload,
            waves=waves,
            clock_estimate=estimate,
            clock_applied=False,
            warnings=notes,
            started_at=logger.started_at,
        )
        report.write_summary(logger, summary)
        _out()
        _out(
            _banner(
                cfg,
                ref,
                detail,
                payload,
                waves,
                t_wall,
                "PREFLIGHT — token, detail and diagnostics only; no bid was sent",
            )
        )
    finally:
        if client is not None:
            client.close()
        logger.close()
    return EXIT_OK


def _secop_clock_text(estimate: ClockEstimate) -> str:
    """One line placing SECOP's log clock, with the interval it was read to."""
    if estimate.offset_ms is None or estimate.lo_ms is None or estimate.hi_ms is None:
        return "not placed — the start audit's row could not be read back"
    behind = "behind" if estimate.offset_ms < 0 else "ahead of"
    return "%.1f ms %s this host (feasible %+.1f .. %+.1f ms, %d candidate row(s))" % (
        abs(estimate.offset_ms),
        behind,
        estimate.lo_ms,
        estimate.hi_ms,
        estimate.samples,
    )


def _move_t_deadline_mono_ns(cfg: Config, armed: Armed) -> Optional[int]:
    """The last instant at which ``T`` may still be moved, or ``None`` with no thread.

    A move can pull the first release earlier by up to ``clock_window_ms``, and the gate
    reads ``gate_lead_ms`` before that; past this instant a thread could wake into a
    schedule that had already left it behind.
    """
    first_release = first_release_mono_ns(cfg, armed)
    if first_release is None:
        return None
    lead_ms = cfg.clock_window_ms + _MOVE_T_SETTLE_MS
    if cfg.gate_mode == "once_before":
        lead_ms += cfg.gate_lead_ms
    return first_release - lead_ms * 1000000


def _place_t_on_secop_clock(
    cfg: Config, armed: Armed, audit: StartAudit, logger: RunLogger, notes: List[str]
) -> Tuple[Armed, bool]:
    """Move ``T`` onto SECOP's log clock when the operator asked and the measurement allows.

    Does nothing unless ``apply_clock_offset`` is set. Then it waits for the start audit's
    read-back, but only until the last instant a move is still safe, and declines — saying
    why, on stdout, in the log and in the summary's warnings — whenever the clock was not
    placed, the interval is wider than ``clock_max_width_ms``, or time ran out. A declined
    move leaves ``T`` exactly where ``fire_time`` put it — unless ``clock_fallback_shift_ms``
    is set, in which case ``T`` is moved by that instead and the run says it did and why.

    The live reading always wins when it is usable: it was taken by this run, seconds ago,
    from this host. When it and the fallback disagree by more than
    ``CLOCK_FALLBACK_DISAGREE_MS`` the disagreement is reported, because one of them is
    wrong or SECOP's clock moved between the two.

    The shift is minus the midpoint: with SECOP's clock 945 ms behind (offset -945), the
    instant it calls ``T`` arrives 945 ms later here.
    """
    if not cfg.apply_clock_offset:
        return (armed, False)

    reason = None  # type: Optional[str]
    estimate = None  # type: Optional[ClockEstimate]
    deadline = _move_t_deadline_mono_ns(cfg, armed)
    if not audit.planned or deadline is None:
        reason = "this run sends no start audit, so there was nothing to measure from"
    else:
        remaining_s = (deadline - time.monotonic_ns()) / 1e9
        finished = audit.join(timeout_s=max(remaining_s, 0.0))
        if isinstance(audit.readback, ClockEstimate):
            estimate = audit.readback
        if not finished:
            reason = (
                "the start audit's row had not been read back by the last moment T could "
                "still be moved"
            )
        elif estimate is None or estimate.offset_ms is None or estimate.width_ms is None:
            reason = "SECOP's clock could not be placed from the start audit"
        elif estimate.width_ms > cfg.clock_max_width_ms:
            reason = (
                "the feasible interval is %.1f ms wide, above clock_max_width_ms=%d"
                % (estimate.width_ms, cfg.clock_max_width_ms)
            )
            if estimate.samples > 1:
                reason += (
                    " — %d start-bidding rows of this company fall inside the window and "
                    "this run's cannot be told from the others. Give each run under one "
                    "company code a start_audit_lead_ms at least %d ms apart"
                    % (estimate.samples, 2 * cfg.clock_window_ms + 1000)
                )
        elif abs(estimate.offset_ms) > cfg.clock_window_ms:
            reason = "the offset %+.1f ms is beyond clock_window_ms=%d" % (
                estimate.offset_ms,
                cfg.clock_window_ms,
            )
        elif time.monotonic_ns() > deadline:
            reason = "the measurement arrived after the last moment T could still be moved"

    fallback = cfg.clock_fallback_shift_ms
    if reason is not None or estimate is None or estimate.offset_ms is None:
        reason = reason or "no measurement"
        if fallback is not None and deadline is not None:
            moved = shift_t(armed, float(fallback))
            message = (
                "this run could not place SECOP's clock (%s), so T was moved %+d ms by "
                "clock_fallback_shift_ms, the clock-probe reading in the config."
                % (reason, fallback)
            )
            _warn(message)
            notes.append(message)
            logger.event(
                stage=CLOCK_STAGE,
                event="clock_applied",
                level="WARNING",
                detail={
                    "applied_to_t": True,
                    "source": "clock_fallback_shift_ms",
                    "shift_ms": float(fallback),
                    "live_reading_declined": reason,
                    "note": "T is now fire_time moved by the operator's clock-probe "
                    "reading; every offset reported after this point is measured from "
                    "the moved T",
                },
            )
            _out("T moved %+d ms by the clock-probe fallback." % fallback)
            return (moved, True)
        message = (
            "apply_clock_offset is set but T was NOT moved: %s. The schedule fires on "
            "fire_time as given." % reason
        )
        _warn(message)
        notes.append(message)
        logger.event(
            stage=CLOCK_STAGE,
            event="clock_not_applied",
            level="WARNING",
            detail={"reason": reason, "applied_to_t": False},
        )
        return (armed, False)

    shift_ms = -estimate.offset_ms
    if fallback is not None and abs(shift_ms - fallback) > CLOCK_FALLBACK_DISAGREE_MS:
        message = (
            "this run's own reading moves T %+.1f ms but clock_fallback_shift_ms says %+d "
            "ms, %.0f ms apart. The live reading was used. One of the two is wrong, or "
            "SECOP's clock moved between them; check the run's secop_clock event."
            % (shift_ms, fallback, abs(shift_ms - fallback))
        )
        _warn(message)
        notes.append(message)
        logger.event(
            stage=CLOCK_STAGE,
            event="clock_disagreement",
            level="WARNING",
            detail={"live_shift_ms": shift_ms, "fallback_shift_ms": fallback},
        )
    moved = shift_t(armed, shift_ms)
    logger.event(
        stage=CLOCK_STAGE,
        event="clock_applied",
        detail={
            "applied_to_t": True,
            "source": "start_audit",
            "fallback_shift_ms": fallback,
            "shift_ms": shift_ms,
            "offset_ms": estimate.offset_ms,
            "lo_ms": estimate.lo_ms,
            "hi_ms": estimate.hi_ms,
            "width_ms": estimate.width_ms,
            "candidates": estimate.samples,
            "note": "T is now the instant SECOP's log clock reads fire_time; every "
            "offset reported after this point is measured from the moved T",
        },
    )
    _out("SECOP clock    %s" % _secop_clock_text(estimate))
    _out("T moved %+.1f ms onto SECOP's log clock." % shift_ms)
    return (moved, True)


def _read_secop_record(
    cfg: Config,
    ref: AuctionRef,
    jar: requests.cookies.RequestsCookieJar,
    armed: Armed,
    detail: Optional[AuctionDetail],
    info: TokenInfo,
    fire: FireOutcome,
    logger: RunLogger,
    side_clients: List[HttpClient],
) -> Optional[Dict[str, Any]]:
    """Read back what SECOP recorded, once it has had time to process what was sent.

    Not before ``secop_record_delay_s`` after the last release: on 2026-11-20 the server
    was still working through this app's bids 35 seconds after the opening, and a read
    taken the moment the sockets gave up would have found none of them.

    Skipped for a run that sent nothing. An interrupt during the wait costs the record,
    not the run's summary.
    """
    if not cfg.read_secop_record or not fire.results:
        return None
    try:
        last_release = max(
            armed.wave_release_mono_ns(wave, cfg.lead_ms) for wave in armed.waves
        )
        read_at = last_release + cfg.secop_record_delay_s * 1000000000
        wait_s = (read_at - time.monotonic_ns()) / 1e9
        if wait_s > 0:
            _out()
            _out("reading SECOP's own record in %.0f s (Ctrl-C to skip it) ..." % wait_s)
            sleep_until_mono(read_at, spin_ms=0.0)
        client = HttpClient(cfg, ref, jar, logger)
        side_clients.append(client)
        reference = armed.t_wall
        source = "fire_time"
        if detail is not None and detail.start_date is not None:
            start = detail.start_date
            if start.tzinfo is not None and start.utcoffset() is not None:
                reference = start.astimezone(timezone.utc)
                source = "startDate"
        return records.read_result(
            ref, client, info.company_code, reference, logger, reference_source=source
        )
    except KeyboardInterrupt:
        _warn("interrupted while waiting to read SECOP's record; the summary goes without it")
        return None


def cmd_clock_probe(args: argparse.Namespace) -> int:
    """Place SECOP's log clock now. Writes exactly one buyer-visible start-bidding row."""
    cfg, notes = _load_config(args)
    _print_warnings(notes)
    ref = page_parse.parse_auction_page(cfg.auction_page_html_path, cfg.auction_id)

    logger = RunLogger(cfg)
    client = None  # type: Optional[HttpClient]
    try:
        _out("run directory  %s" % logger.run_dir)
        _out()
        info = _check_token(cfg, logger)
        _out()
        _out(
            "clock-probe sends ONE SetStartBiddingAudit. It writes a start-bidding row "
            "the auction owner can read, under this host's public IP and company code "
            "%s. No bid is sent." % (info.company_code or "?")
        )

        now_wall = datetime.now(timezone.utc)
        now_mono_ns = time.monotonic_ns()
        # Only an anchor for wall/monotonic conversion: there is no schedule here.
        anchor = Armed(
            t_wall=now_wall,
            t_mono_ns=now_mono_ns,
            armed_at_wall=now_wall,
            armed_at_mono_ns=now_mono_ns,
            waves=[],
        )
        client = HttpClient(cfg, ref, requests.cookies.RequestsCookieJar(), logger)
        outcome = client.post_empty(
            endpoints.set_start_bidding_audit(ref.auction_id),
            stage=CLOCK_STAGE,
            thread_id="clock-probe",
            role="audit",
        )
        estimate = records.clock_from_start_audit(
            cfg, anchor, ref, client, outcome, info.company_code, logger
        )
        _out()
        _out("--- SECOP CLOCK ---")
        _out("start audit    status %s, %s ms" % (
            outcome.status,
            "?" if outcome.rtt_ms is None else "%.1f" % outcome.rtt_ms,
        ))
        _out("SECOP clock    %s" % _secop_clock_text(estimate))
        if estimate.offset_ms is None:
            _warn(
                "the clock was not placed; see the secop_clock event in %s for why"
                % (logger.run_dir / "events.jsonl")
            )
            return EXIT_NETWORK
        _out(
            "An instant on SECOP's clock is reached %+.1f ms from the same reading on this "
            "host. A run with apply_clock_offset=true measures this again from its own "
            "start audit and moves T by it; otherwise add it to schedule_offsets_ms "
            "yourself. This is one measurement, and whether it holds until the auction "
            "is not known." % -estimate.offset_ms
        )
        _out(
            "As the fallback for a run whose own reading fails, set "
            "clock_fallback_shift_ms: %d" % int(round(-estimate.offset_ms))
        )
        if estimate.width_ms is not None and estimate.width_ms > cfg.clock_max_width_ms:
            _warn(
                "the interval is %.1f ms wide, above clock_max_width_ms=%d: a run would "
                "decline to move T on a measurement like this one"
                % (estimate.width_ms, cfg.clock_max_width_ms)
            )
    finally:
        if client is not None:
            client.close()
        logger.close()
    return EXIT_OK


def _outcome_exit_code(outcome: FireOutcome) -> int:
    """Exit code for a schedule that ran: 0 won, 5 everything bounced, 4/6 if that is why."""
    if outcome.winner is not None:
        return EXIT_OK
    failures = [result.failure for result in outcome.results]
    if Failure.AUTH_EXPIRED in failures:
        return EXIT_AUTH
    if failures and all(failure is Failure.NETWORK for failure in failures):
        return EXIT_NETWORK
    if outcome.submissions_sent > 0:
        return EXIT_ALL_REJECTED
    # Nothing was submitted: a dry run, a probe-only schedule or a gate that stopped it.
    return EXIT_OK


def _execute_run(
    cfg: Config,
    ref: AuctionRef,
    waves: List[Wave],
    bid_value: Decimal,
    logger: RunLogger,
    notes: List[str],
) -> int:
    """Everything in a run that touches the network, from the token check to the summary."""
    started_at = logger.started_at
    detail = None  # type: Optional[AuctionDetail]
    payload = None  # type: Optional[BidPayload]
    armed = None  # type: Optional[Armed]
    verdict = None  # type: Optional[GateVerdict]
    fire = None  # type: Optional[FireOutcome]
    clients = []  # type: List[HttpClient]
    # Clients no fire thread uses: the start audit's, and the one the record is read on.
    side_clients = []  # type: List[HttpClient]
    audit = None  # type: Optional[StartAudit]
    clock_applied = False
    secop_record = None  # type: Optional[Dict[str, Any]]

    def finish(error: Optional[BaseException]) -> None:
        secop_clock = None  # type: Optional[ClockEstimate]
        if audit is not None and isinstance(audit.readback, ClockEstimate):
            secop_clock = audit.readback
        summary = report.build_summary(
            cfg,
            run_id=logger.run_id,
            ref=ref,
            detail=detail,
            payload=payload,
            armed=armed,
            waves=waves,
            gate=verdict,
            fire=fire,
            secop_clock=secop_clock,
            secop_record=secop_record,
            clock_applied=clock_applied,
            warnings=notes,
            error=error,
            started_at=started_at,
        )
        report.write_summary(logger, summary)
        _out()
        _out(report.human_summary(summary))

    try:
        # Before the first request: an unusable T must cost zero round trips and zero
        # buyer-visible audit rows, and every one of them below is one or both.
        t_wall = parse_fire_time(cfg.fire_time)

        info = _check_token(cfg, logger)

        jar = requests.cookies.RequestsCookieJar()
        thread_count = max(
            1,
            sum(wave.direct + wave.validate + wave.validate_only for wave in waves),
        )
        clients = [HttpClient(cfg, ref, jar, logger) for _ in range(thread_count)]
        prewarm(clients, ref, logger)

        detail = fetch_detail(clients[0], ref, logger)
        payload, build_report = build_payload(ref, detail, bid_value)
        _log_payload_built(logger, payload, build_report, FETCHED_SOURCE)
        _print_warnings(build_report.warnings)
        notes.extend(build_report.warnings)

        mismatch = check_fire_time_vs_start_date(
            t_wall, detail.start_date, cfg.fire_time_mismatch_warn_s
        )
        if mismatch:
            _warn(mismatch)
            notes.append(mismatch)

        _out()
        _out(_banner(cfg, ref, detail, payload, waves, t_wall, _run_headline(cfg)))
        _out()

        # The wire `Date` is re-stamped here and the payload serialized exactly once, so
        # the bytes every thread shares are as fresh as the countdown allows. Stamping it
        # where the payload is built leaves it stale by however long the run sat armed —
        # up to max_arm_ahead_s. The browser's Date was 1-3 ms before the send, every time.
        payload = dataclasses.replace(payload, date=js_iso_now())
        payload_bytes = payload.to_bytes()

        armed = arm(cfg, waves)

        # Before the start audit: a run that is going to refuse to submit must refuse
        # here, not after writing a row.
        require_live_confirmation(cfg, waves)

        # The start audit and its read-back run on a thread and a client of their own, so
        # nothing below waits on them unless it chooses to.
        audit_client = HttpClient(cfg, ref, jar, logger)
        side_clients.append(audit_client)
        anchor = armed
        audit = StartAudit(
            cfg,
            armed,
            ref,
            audit_client,
            logger,
            on_sent=lambda outcome: records.clock_from_start_audit(
                cfg, anchor, ref, audit_client, outcome, info.company_code, logger
            ),
        )
        audit.start()

        first_release = first_release_mono_ns(cfg, armed)
        if first_release is not None:
            keep_warm(cfg, clients, ref, logger, first_release)

        armed, clock_applied = _place_t_on_secop_clock(cfg, armed, audit, logger, notes)

        verdict = run_gate(cfg, armed, clients[0], ref, logger)
        if not verdict.proceed:
            _warn("gate stopped the run: %s" % verdict.reason)
            finish(None)
            return exit_code_for(verdict.failure or Failure.STATE_UNKNOWN)

        fire = run_schedule(
            cfg, armed, ref, detail, payload_bytes, clients, logger, verdict,
            start_audit=audit,
        )
        audit.join(
            timeout_s=3.0 * (cfg.connect_timeout_ms + cfg.read_timeout_ms) / 1000.0
            + _AUDIT_JOIN_MARGIN_S
        )
        secop_record = _read_secop_record(
            cfg, ref, jar, armed, detail, info, fire, logger, side_clients
        )
        finish(None)
        return _outcome_exit_code(fire)
    except BaseException as exc:
        try:
            finish(exc)
        except Exception as summary_error:
            logger.event(
                stage="report",
                event="error",
                level="ERROR",
                detail={
                    "exception": "%s: %s"
                    % (type(summary_error).__name__, summary_error),
                    "note": "summary.json could not be written; the failure it was "
                    "reporting on is raised unchanged so it is not lost behind this one",
                },
            )
        raise
    finally:
        if audit is not None:
            audit.cancel.set()
        for client in clients + side_clients:
            client.close()


def cmd_run(args: argparse.Namespace) -> int:
    """The full pipeline. ``CreateBid`` leaves this process only with ``--yes-i-mean-it``."""
    cfg, notes = _load_config(args)
    _print_warnings(notes)

    ref = page_parse.parse_auction_page(cfg.auction_page_html_path, cfg.auction_id)
    waves = build_waves(cfg)
    bid_value = numbers.parse_user_decimal(cfg.bid_value)

    if not cfg.confirm_live and not cfg.dry_run:
        _warn(
            "confirm_live is False and dry_run is False: this run will do everything up to "
            "the fire instant and then refuse to submit. Add --yes-i-mean-it to send the "
            "bid, or --set dry_run=true to rehearse the threading (which still writes "
            "audit rows)."
        )

    logger = RunLogger(cfg)
    try:
        return _execute_run(cfg, ref, waves, bid_value, logger, list(notes))
    finally:
        logger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Scheduled first-bid submitter for SECOP II reverse auctions. "
            "parse-page and plan are offline; preflight sends no bid; clock-probe sends "
            "one start-bidding audit and no bid; run sends a real CreateBid only with "
            "--yes-i-mean-it."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    page = subparsers.add_parser(
        "parse-page", help="resolve the auction from a saved page (offline)"
    )
    page.add_argument("--page", required=True, help="path to the saved auction HTML")
    page.add_argument(
        "--auction-id",
        default=None,
        help="resolve a multi-auction page; the id must be present on the page",
    )
    page.set_defaults(handler=cmd_parse_page)

    def with_config(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--config", required=True, help="path to the JSON config file")
        sub.add_argument(
            "--set",
            dest="set_pairs",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="override one config key; unknown keys are a hard error",
        )
        return sub

    plan = with_config(
        subparsers.add_parser(
            "plan",
            help="build the schedule, payload and footprint offline (no network, no rows)",
        )
    )
    plan.add_argument(
        "--auction-simple",
        default=None,
        help="saved GetAuctionSimple body, to build the real payload instead of a "
        "provisional one",
    )
    plan.add_argument(
        "--auction",
        default=None,
        help="saved GetAuction body (the item list); needs --auction-simple",
    )
    plan.set_defaults(handler=cmd_plan)

    preflight = with_config(
        subparsers.add_parser(
            "preflight", help="token, auction detail, RTT and clock diagnostics; no bids"
        )
    )
    preflight.set_defaults(handler=cmd_preflight)

    probe = with_config(
        subparsers.add_parser(
            "clock-probe",
            help="place SECOP's log clock; writes ONE buyer-visible start-bidding row, "
            "no bid",
        )
    )
    probe.set_defaults(handler=cmd_clock_probe)

    run = with_config(
        subparsers.add_parser("run", help="the full pipeline; arms only with the flag")
    )
    run.add_argument(
        "--yes-i-mean-it",
        dest="yes_i_mean_it",
        action="store_true",
        help="the only thing that sets confirm_live and allows a real CreateBid",
    )
    run.set_defaults(handler=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point; returns the process exit code rather than calling ``sys.exit``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_CONFIG

    try:
        return handler(args)
    except SecopError as exc:
        print("ERROR [%s] %s" % (exc.code.value, exc.message), file=sys.stderr)
        if exc.detail:
            print(json.dumps(exc.detail, indent=2, ensure_ascii=False, default=str),
                  file=sys.stderr)
        return exit_code_for(exc.code)
    except KeyboardInterrupt:
        print("interrupted; nothing further was sent", file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        traceback.print_exc()
        return EXIT_ERROR


__all__ = [
    "EXIT_ALL_REJECTED",
    "EXIT_AUTH",
    "EXIT_BY_FAILURE",
    "EXIT_CONFIG",
    "EXIT_ERROR",
    "EXIT_NETWORK",
    "EXIT_OK",
    "EXIT_PARSE",
    "build_parser",
    "exit_code_for",
    "main",
    "parse_set_overrides",
]


if __name__ == "__main__":
    sys.exit(main())
