"""Stage 1 — extract the auction identifiers from a saved SECOP page (plan §5).

Two pages are supported and they yield their identifiers by two different mechanisms, so
the extractor that produced a result is recorded on :class:`~secop_firstbid.models.AuctionRef`:

===================  =====================  ==============  ===========================
Page                 ``<title>``            Fixture         Extractor
===================  =====================  ==============  ===========================
Opportunity list     ``Oportunidades``      beforebid.html  A — ``onclick``
Auction launch       ``Integración``        bid.html        B — ``iframe``
===================  =====================  ==============  ===========================

**Order of operations is load-bearing.** Read the file as utf-8 with ``errors="replace"``,
then ``html.unescape``, then de-concatenate, then ``urllib.parse.parse_qs``. Every public
function here unescapes its own input exactly once, so callers always pass the raw page
text; nothing in this module is safe to feed already-unescaped text twice.

The opportunity page's ``onclick`` is a JavaScript string-concatenation **expression**, not
a URL::

    getAction('/CO1BusinessLine/.../RedirectToEAuction' + '?' + 'docUniqueIdentifier=' +
              'CO1.EAREQ.937510' + '&' + 'market=' + 'GovCo' + '&' + 'externalId=' + '…')

``parse_qs`` applies ``unquote_plus``, so each ``+`` operator decodes to a space and the
keys come back as ``"'   'externalId"`` — ``params.get("externalId")`` is ``None``.
:data:`DECONCAT` collapses the concatenation first; only then is the remainder a real
query string. Both fixtures are DOM serializations (pretty-printed, one attribute per
line), so every pattern uses ``\\s*\\+\\s*`` and never a literal ``' + '``.

**A bare-GUID fallback is forbidden.** ``beforebid.html`` holds 75 GUID occurrences across
25 distinct GUIDs; the target GUID is 72nd in document order and is tied for least
frequent. "First GUID" and "most frequent GUID" therefore both select a context-menu id —
such a fallback does not degrade gracefully, it silently bids on the wrong auction. For
the same reason :data:`FB_DOC`'s ``CO1\\.EAREQ\\.`` anchor must never be relaxed:
``docUniqueIdentifier`` occurs 21 times in that page and 20 of them are the parent
opportunity dossier ``CO1.OPDOS.31397606``.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import Failure, SecopError
from .models import AuctionRef

DECONCAT = re.compile(r"'\s*\+\s*'")
ANCHOR = re.compile(r"RedirectToEAuction[A-Za-z]*'.*?,\s*true\s*\)\s*;", re.S)
FB_EXT = re.compile(r"externalId=(?:'\s*\+\s*')?([0-9a-fA-F-]{36})")
FB_DOC = re.compile(r"docUniqueIdentifier=(?:'\s*\+\s*')?(CO1\.EAREQ\.\d+)")
IFRAME = re.compile(r"<iframe[^>]*\bsrc=\"([^\"]*eauctions\.site/\?[^\"]*)\"", re.I | re.S)
STATE_TD = re.compile(r"<td[^>]*id=\"[^\"]*thEAuctionStateCol\"[^>]*>(.*?)</td>", re.S | re.I)

DEFAULT_LANG = "es-CO"
DEFAULT_MARKET = "GovCo"

_ANCHOR_TAIL = re.compile(r"'?\s*,\s*true\s*\)\s*;?\s*\Z")
_GUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_ROW_TR = re.compile(r"id=\"[^\"]*_tr(\d+)\"")
_ROW_SUFFIX = re.compile(r"id=\"[^\"]*_(\d+)\"")
_SCRIPT_BLOCK = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")
_LANGUAGE_SELECT = re.compile(
    r"<select\b[^>]*\b(?:id|name)=\"languageSelector\"[^>]*>(.*?)</select\s*>", re.I | re.S
)
_OPTION_SELECTED = re.compile(
    r"<option\b(?=[^>]*\bselected\b)(?=[^>]*\bvalue=\"([^\"]+)\")[^>]*>", re.I | re.S
)
_LANGUAGE_CODE_PARAM = re.compile(r"languageCode=([A-Za-z]{2}(?:-[A-Za-z]{2})?)\b")
_LOCALE_VALUE = re.compile(r"\A[a-z]{2}(?:-[A-Za-z]{2})?\Z")
_LOCALE_JSON = re.compile(r"/([a-z]{2}(?:-[A-Z]{2})?)\.json")
_ANCHOR_PREFIX_WINDOW = 512
_ROW_WINDOW = 8192


@dataclass(frozen=True)
class Candidate:
    external_id: str
    doc_id: Optional[str]
    market: Optional[str]
    mkey: Optional[str]
    redirect_path: Optional[str]
    row_index: Optional[int]
    state_text: Optional[str]
    extractor: str

    def describe(self) -> Dict[str, Optional[str]]:
        """The operator-facing shape used when listing an ambiguous page's candidates."""
        return {
            "external_id": self.external_id,
            "doc_id": self.doc_id,
            "row_index": None if self.row_index is None else str(self.row_index),
            "state_text": self.state_text,
            "extractor": self.extractor,
        }


def _strip_tags(fragment: str) -> Optional[str]:
    text = _SCRIPT_BLOCK.sub(" ", fragment)
    text = _TAG.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text or None


def _state_cells(unescaped: str) -> List[Optional[str]]:
    return [_strip_tags(cell) for cell in STATE_TD.findall(unescaped)]


def _state_for(cells: List[Optional[str]], row_index: Optional[int]) -> Optional[str]:
    if row_index is not None and 0 <= row_index < len(cells):
        return cells[row_index]
    if len(cells) == 1:
        return cells[0]
    return None


def _row_index(unescaped: str, start: int) -> Optional[int]:
    window = unescaped[max(0, start - _ROW_WINDOW):start]
    rows = _ROW_TR.findall(window)
    if rows:
        return int(rows[-1])
    suffixes = _ROW_SUFFIX.findall(window)
    if suffixes:
        return int(suffixes[-1])
    return None


def _redirect_path(unescaped: str, start: int, head: str) -> Optional[str]:
    window = unescaped[max(0, start - _ANCHOR_PREFIX_WINDOW):start]
    quote = window.rfind("'")
    prefix = window[quote + 1:] if quote != -1 else ""
    path = prefix + head
    if path.startswith("/"):
        return path
    return head or None


def _single_valued(query: str) -> Dict[str, str]:
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    return dict((key, values[0]) for key, values in parsed.items() if values)


def extract_candidates_onclick(text: str) -> List[Candidate]:
    """Extractor A — the concatenated ``RedirectToEAuction`` ``onclick`` expression.

    :data:`ANCHOR` matches twice per row on the real page: ``RedirectToEAuctionIFrame``
    contains ``RedirectToEAuction``. Both anchors carry the same ``externalId`` and only
    the plain one carries ``docUniqueIdentifier``; both candidates are returned here and
    :func:`parse_auction_page` merges them.
    """
    unescaped = html.unescape(text)
    cells = _state_cells(unescaped)
    out = []  # type: List[Candidate]
    for match in ANCHOR.finditer(unescaped):
        joined = DECONCAT.sub("", match.group(0))
        head, separator, tail = joined.partition("?")
        if not separator:
            continue
        query = _ANCHOR_TAIL.sub("", tail).rstrip("'")
        params = _single_valued(query)
        external_id = params.get("externalId")
        if not external_id or not _GUID.match(external_id):
            continue
        row_index = _row_index(unescaped, match.start())
        out.append(
            Candidate(
                external_id=external_id,
                doc_id=params.get("docUniqueIdentifier"),
                market=params.get("market"),
                mkey=params.get("mkey"),
                redirect_path=_redirect_path(unescaped, match.start(), head),
                row_index=row_index,
                state_text=_state_for(cells, row_index),
                extractor="onclick",
            )
        )
    return out


def extract_candidates_iframe(text: str) -> List[Candidate]:
    """Extractor B — the launch page's ``iframe`` ``src``, which is a genuine URL.

    This is the only place ``lang`` and ``market`` appear in their API-side spelling
    (``languageCode`` / ``marketCode``), so it doubles as the cross-check on the Referer.
    """
    unescaped = html.unescape(text)
    cells = _state_cells(unescaped)
    out = []  # type: List[Candidate]
    for match in IFRAME.finditer(unescaped):
        split = urllib.parse.urlsplit(match.group(1))
        params = _single_valued(split.query)
        external_id = params.get("auctionId")
        if not external_id or not _GUID.match(external_id):
            continue
        out.append(
            Candidate(
                external_id=external_id,
                doc_id=None,
                market=params.get("marketCode"),
                mkey=None,
                redirect_path=split.path or None,
                row_index=None,
                state_text=_state_for(cells, None),
                extractor="iframe",
            )
        )
    return out


def extract_candidates_fallback(text: str) -> List[Candidate]:
    """Concatenation-aware last resort.

    The optional ``(?:'\\s*\\+\\s*')?`` group in :data:`FB_EXT` / :data:`FB_DOC` is what
    makes these work on the concatenated *and* the plain form; the naive
    ``externalId=([0-9a-f-]{36})`` matches zero bytes on either page we have. A GUID is
    only accepted here when it is anchored to a literal ``externalId=`` — scanning for a
    bare GUID would pick a context-menu id (see the module docstring).

    ``doc_id`` is attached only when the page yields exactly one auction and exactly one
    ``CO1.EAREQ`` identifier; with several of either there is no evidence about which
    belongs to which.
    """
    unescaped = html.unescape(text)
    external_ids = []  # type: List[str]
    for guid in FB_EXT.findall(unescaped):
        if _GUID.match(guid) and guid not in external_ids:
            external_ids.append(guid)
    doc_ids = []  # type: List[str]
    for doc in FB_DOC.findall(unescaped):
        if doc not in doc_ids:
            doc_ids.append(doc)
    pairable = len(external_ids) == 1 and len(doc_ids) == 1
    cells = _state_cells(unescaped)
    return [
        Candidate(
            external_id=external_id,
            doc_id=doc_ids[0] if pairable else None,
            market=None,
            mkey=None,
            redirect_path=None,
            row_index=None,
            state_text=_state_for(cells, None),
            extractor="fallback",
        )
        for external_id in external_ids
    ]


def extract_state_text(text: str) -> Optional[str]:
    """The auction state label, e.g. ``"Terminado"``.

    The value sits inside a nested ``<a>`` in ``td[id$='thEAuctionStateCol']``, so the
    inner tags are stripped. ``bid.html`` has no such cell at all: ``None`` there means
    ``STATE_UNKNOWN``, never "not finished". This is a convenience check — the server-side
    state read in §9.4 is the real guard.
    """
    cells = _state_cells(html.unescape(text))
    for cell in cells:
        if cell:
            return cell
    return None


def extract_lang(text: str) -> Optional[str]:
    """Resolve the page's locale.

    Three sources, in decreasing authority: the selected ``languageSelector`` option
    (the opportunity page), the ``languageCode`` parameter on the ``eauctions.site`` URL
    (the launch page — the API's own spelling), and the ``…/es-CO.json`` translation
    paths (68 hits in ``beforebid.html``, 19 in ``bid.html``, no competing locale in
    either).

    The option search is scoped to the ``languageSelector`` element and every result is
    checked against a locale shape: both pages carry an earlier, unrelated
    ``<option selected="true" value="900000000">`` (the company selector), which a
    document-wide "first selected option" would return as the language.
    """
    unescaped = html.unescape(text)
    select = _LANGUAGE_SELECT.search(unescaped)
    if select:
        selected = _OPTION_SELECTED.search(select.group(1))
        if selected and _LOCALE_VALUE.match(selected.group(1)):
            return selected.group(1)
    for match in IFRAME.finditer(unescaped):
        code = _LANGUAGE_CODE_PARAM.search(match.group(1))
        if code:
            return code.group(1)
    counts = {}  # type: Dict[str, int]
    order = []  # type: List[str]
    for locale in _LOCALE_JSON.findall(unescaped):
        if locale not in counts:
            counts[locale] = 0
            order.append(locale)
        counts[locale] += 1
    if not order:
        return None
    return max(order, key=lambda locale: (counts[locale], -order.index(locale)))


def _merge(candidates: List[Candidate]) -> Dict[str, Candidate]:
    merged = {}  # type: Dict[str, Candidate]
    for candidate in candidates:
        previous = merged.get(candidate.external_id)
        if previous is None:
            merged[candidate.external_id] = candidate
            continue
        merged[candidate.external_id] = Candidate(
            external_id=previous.external_id,
            doc_id=previous.doc_id or candidate.doc_id,
            market=previous.market or candidate.market,
            mkey=previous.mkey or candidate.mkey,
            redirect_path=previous.redirect_path or candidate.redirect_path,
            row_index=previous.row_index if previous.row_index is not None else candidate.row_index,
            state_text=previous.state_text or candidate.state_text,
            extractor=previous.extractor,
        )
    return merged


def parse_auction_page(path: str, auction_id_override: Optional[str] = None) -> AuctionRef:
    """Resolve one :class:`~secop_firstbid.models.AuctionRef` from a saved page.

    Extractors are tried in order A → B → fallback; the first one yielding candidates
    wins. More than one distinct ``externalId`` is the normal multi-row case and is never
    guessed at: it raises ``PARSE_AMBIGUOUS`` listing every candidate. An
    ``auction_id_override`` resolves that only if the id is actually among the candidates.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError as exc:
        raise SecopError(
            Failure.PARSE_FAILED,
            "cannot read auction page %r: %s" % (path, exc),
            {"path": str(path)},
        )

    candidates = []  # type: List[Candidate]
    for extractor in (
        extract_candidates_onclick,
        extract_candidates_iframe,
        extract_candidates_fallback,
    ):
        candidates = extractor(raw)
        if candidates:
            break

    if not candidates:
        raise SecopError(
            Failure.PARSE_FAILED,
            "no auction identifier in %r: none of the onclick, iframe or "
            "concatenation-aware fallback extractors matched" % (path,),
            {"path": str(path)},
        )

    merged = _merge(candidates)
    listing = [candidate.describe() for candidate in merged.values()]

    if len(merged) > 1:
        if auction_id_override is None:
            raise SecopError(
                Failure.PARSE_AMBIGUOUS,
                "%d distinct auctions on %r; set auction_id to one of: %s"
                % (len(merged), path, ", ".join(sorted(merged))),
                {"path": str(path), "candidates": listing},
            )
        if auction_id_override not in merged:
            raise SecopError(
                Failure.PARSE_AMBIGUOUS,
                "auction_id override %r is not on %r; candidates are: %s"
                % (auction_id_override, path, ", ".join(sorted(merged))),
                {
                    "path": str(path),
                    "auction_id_override": auction_id_override,
                    "candidates": listing,
                },
            )
        chosen = merged[auction_id_override]
        source_extractor = "override"
    else:
        chosen = list(merged.values())[0]
        source_extractor = chosen.extractor
        if auction_id_override is not None and auction_id_override != chosen.external_id:
            raise SecopError(
                Failure.PARSE_FAILED,
                "auction_id override %r is not on %r; the page carries %r"
                % (auction_id_override, path, chosen.external_id),
                {
                    "path": str(path),
                    "auction_id_override": auction_id_override,
                    "candidates": listing,
                },
            )

    if not _GUID.match(chosen.external_id):
        raise SecopError(
            Failure.PARSE_FAILED,
            "extracted auction id %r is not a GUID" % (chosen.external_id,),
            {"path": str(path), "candidates": listing},
        )

    lang = extract_lang(raw)
    lang_defaulted = lang is None
    if lang_defaulted:
        lang = DEFAULT_LANG

    market = chosen.market
    market_defaulted = market is None
    if market_defaulted:
        market = DEFAULT_MARKET

    return AuctionRef(
        auction_id=chosen.external_id,
        lang=lang,
        market=market,
        eauction_doc_id=chosen.doc_id,
        mkey=chosen.mkey,
        redirect_path=chosen.redirect_path,
        source_extractor=source_extractor,
        state_text=chosen.state_text,
        lang_defaulted=lang_defaulted,
        market_defaulted=market_defaulted,
    )


__all__ = [
    "DECONCAT",
    "ANCHOR",
    "FB_EXT",
    "FB_DOC",
    "IFRAME",
    "STATE_TD",
    "DEFAULT_LANG",
    "DEFAULT_MARKET",
    "Candidate",
    "extract_candidates_onclick",
    "extract_candidates_iframe",
    "extract_candidates_fallback",
    "extract_state_text",
    "extract_lang",
    "parse_auction_page",
]
