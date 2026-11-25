"""Decimal parsing, formatting and the two bid formulas (plan §8: §8.1 formats, §8.4 formulas).

Two number formats meet here and must never be mixed: the wire format (dot decimal, no
thousands separator, JSON strings) and the Colombian es-CO format the UI and every server
*message* use (dot thousands, comma decimal, optional ``COP`` suffix).

Everything is :class:`decimal.Decimal` under :data:`CTX`. Scale is load-bearing: the wire
strings are ``str(Decimal)`` with scale addition on multiply and trailing zeros intact, so
nothing in this module calls ``.normalize()`` and nothing strips a zero.
"""

from __future__ import annotations

import decimal
import re
from decimal import Decimal
from typing import Tuple

from .errors import Failure, SecopError

CTX = decimal.Context(prec=50)
IQUP_SCALE = 12

_IQUP_EXPONENT = Decimal("1E-%d" % IQUP_SCALE)
_ZERO_IQUP = Decimal("0.000000000000")
_CURRENCY_SUFFIX = "COP"

_DIGITS = re.compile(r"^[0-9]+$")
_GROUPED = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{3})+$")


def install_context() -> None:
    """Install :data:`CTX` as the calling thread's decimal context.

    Decimal contexts are thread-local, so every worker thread that does arithmetic outside
    this module needs its own call. The functions here do not depend on it: each one runs
    its arithmetic inside ``decimal.localcontext(CTX)``, so a caller that never installs
    anything still gets prec=50 results.
    """
    decimal.setcontext(CTX)


def _reject(code: Failure, value: str, why: str) -> "SecopError":
    return SecopError(code, "%s: %s" % (why, value), {"value": value})


def _strip_currency(s: str) -> str:
    text = s.strip()
    if text[-3:].upper() == _CURRENCY_SUFFIX:
        text = text[:-3].strip()
    return text


def _split_sign(text: str) -> Tuple[str, str]:
    if text[:1] in ("+", "-"):
        return ("-" if text[0] == "-" else "", text[1:])
    return ("", text)


def _group_thousands(digits: str) -> str:
    groups = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    return ".".join(reversed(groups))


def parse_user_decimal(s: str) -> Decimal:
    """Parse an operator-supplied amount in either format, refusing the undecidable one.

    Accepts ``"1"``, ``"1234567.89"``, ``"7813974.32"``, ``"7.813.974,32"`` (with or without
    a ``COP`` suffix) and ``"390.698.716"``.

    The rule is decidability, not best-effort. A comma means Colombian, so dots are
    thousands separators. Without a comma, a *single* dot followed by exactly three digits
    has two readings and no tiebreaker — ``"1.234"`` is 1234 written in Colombian thousands
    or 1.234 written for the wire — so it is rejected as ``CONFIG_INVALID`` rather than
    guessed. Two or more dots can only be thousands grouping. Grouped integer parts are
    checked (1-3 digits, then groups of exactly 3), so ``"1.23.456"`` is rejected instead of
    silently becoming 123456.

    The value's scale is preserved: ``"1.500000"`` parses to a six-decimal Decimal, which is
    what ``to_wire`` will later emit verbatim.
    """
    text = _strip_currency(s)
    if not text:
        raise _reject(Failure.CONFIG_INVALID, s, "empty amount")
    sign, body = _split_sign(text)
    if not body:
        raise _reject(Failure.CONFIG_INVALID, s, "sign with no digits")

    if "," in body:
        integer, _, fraction = body.partition(",")
        if "," in fraction:
            raise _reject(Failure.CONFIG_INVALID, s, "more than one decimal comma")
        if not _DIGITS.match(fraction):
            raise _reject(Failure.CONFIG_INVALID, s, "no digits after the decimal comma")
        if "." in integer:
            if not _GROUPED.match(integer):
                raise _reject(
                    Failure.CONFIG_INVALID, s, "malformed Colombian thousands grouping"
                )
            integer = integer.replace(".", "")
        elif not _DIGITS.match(integer):
            raise _reject(Failure.CONFIG_INVALID, s, "not a decimal amount")
        return Decimal(sign + integer + "." + fraction)

    dots = body.count(".")
    if dots == 0:
        if not _DIGITS.match(body):
            raise _reject(Failure.CONFIG_INVALID, s, "not a decimal amount")
        return Decimal(sign + body)

    if dots == 1:
        integer, _, fraction = body.partition(".")
        if not _DIGITS.match(integer) or not _DIGITS.match(fraction):
            raise _reject(Failure.CONFIG_INVALID, s, "not a decimal amount")
        if len(fraction) == 3:
            raise SecopError(
                Failure.CONFIG_INVALID,
                "ambiguous amount %r: one dot with a 3-digit group reads as %s in Colombian "
                "thousands or %s on the wire. Write it unambiguously — %s,00 or %s0 — "
                "and re-run." % (s, integer + fraction, body, body, body),
                {
                    "value": s,
                    "colombian": integer + fraction,
                    "wire": body,
                },
            )
        return Decimal(sign + body)

    if not _GROUPED.match(body):
        raise _reject(Failure.CONFIG_INVALID, s, "malformed Colombian thousands grouping")
    return Decimal(sign + body.replace(".", ""))


def parse_server_money(s: str) -> Decimal:
    """Parse a server-rendered amount: ``"7.813.974,32 COP"`` -> ``Decimal("7813974.32")``.

    Unlike :func:`parse_user_decimal` this branch has no ambiguity to resolve: the server
    always renders es-CO, so every dot is a thousands separator whatever the group sizes.
    """
    text = _strip_currency(s)
    if not text:
        raise _reject(Failure.PARSE_FAILED, s, "empty server amount")
    sign, body = _split_sign(text)
    integer, comma, fraction = body.partition(",")
    integer = integer.replace(".", "")
    if not _DIGITS.match(integer):
        raise _reject(Failure.PARSE_FAILED, s, "not a server amount")
    if not comma:
        return Decimal(sign + integer)
    if not _DIGITS.match(fraction):
        raise _reject(Failure.PARSE_FAILED, s, "not a server amount")
    return Decimal(sign + integer + "." + fraction)


def format_colombian(d: Decimal, decimals: int = 2) -> str:
    """Render ``d`` the way the UI and the server's messages do: ``"7.813.974,32"``."""
    if decimals < 0:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "decimals must not be negative: %d" % decimals,
            {"decimals": decimals},
        )
    with decimal.localcontext(CTX):
        quantized = d.quantize(Decimal(1).scaleb(-decimals), rounding=decimal.ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    fixed = format(abs(quantized), "f")
    integer, _, fraction = fixed.partition(".")
    grouped = sign + _group_thousands(integer)
    if not decimals:
        return grouped
    return grouped + "," + fraction


def to_wire(d: Decimal) -> str:
    """The wire string for ``d``, in fixed-point notation with its scale intact.

    Trailing zeros carry the scale and are part of the wire form:
    ``Decimal("1.000000") * Decimal("10")`` is ``"10.000000"`` and
    ``* Decimal("7813974.32")`` is ``"7813974.32000000"``. Normalizing or stripping zeros
    here would produce strings the capture never contains.

    ``format(d, "f")`` rather than ``str(d)``: ``str`` switches to scientific notation once
    the adjusted exponent drops below -6, so a 12-dp zero renders as ``"0E-12"`` and a small
    ``itemQuantityUnitPrice`` as ``"1.000E-9"``. Both are exponent forms the server has never
    been sent. ``"f"`` is fixed-point always and preserves the scale identically otherwise.
    """
    return format(d, "f")


def parse_wire_decimal(s: str) -> Decimal:
    """Parse a wire string back to Decimal with its scale intact (``"10.000000"`` -> 6 dp)."""
    try:
        value = Decimal(s.strip())
    except (decimal.InvalidOperation, ValueError, AttributeError):
        raise _reject(Failure.PARSE_FAILED, str(s), "not a wire decimal")
    if not value.is_finite():
        raise _reject(Failure.PARSE_FAILED, str(s), "not a finite wire decimal")
    return value


def total_value(quantity: Decimal, bid_value: Decimal) -> Decimal:
    """``quantity × bid_value`` — scale adds, nothing is stripped (§8.4)."""
    with decimal.localcontext(CTX):
        return quantity * bid_value


def item_quantity_unit_price(
    initial_value: Decimal,
    quantity: Decimal,
    bid_value: Decimal,
    rounding: str = decimal.ROUND_HALF_UP,
) -> Tuple[Decimal, bool, Decimal]:
    """``initialValue × quantity ÷ bidValue`` at exactly 12 dp (§8.4).

    Returns ``(value, is_exact, residual)``. ``is_exact`` is True when the quotient needed no
    rounding to reach 12 dp; ``residual`` is how much the quantization moved it.
    :func:`divides_exactly` is the authoritative termination test — ``is_exact`` is computed
    from a quotient of finite precision, which is that test seen through 50 digits.

    A tiny ``bid_value`` makes the quotient large, and quantizing to 12 dp then needs one
    digit of context precision per integer digit *plus* 12: past 38 integer digits that is
    more than :data:`CTX` carries, so the precision is raised to keep 50 digits below the
    decimal point rather than 50 in total. Where even that has no answer — a quotient beyond
    the context's exponent range — the failure is ``MALFORMED_PAYLOAD``, never a bare
    :mod:`decimal` exception, because the caller's exit code has to stay in the table.

    The server's rounding mode for a non-terminating division is undocumented
    (``roundTypeCode`` is ``"Up"`` at auction level, which may or may not apply here — §15
    Q8), hence the parameter and hence the caller's obligation to WARN when ``is_exact`` is
    False rather than to assume this matches what the server would have computed.
    """
    if initial_value == 0 or bid_value == 0:
        return (_ZERO_IQUP, True, Decimal(0))
    try:
        with decimal.localcontext(CTX) as ctx:
            quotient = initial_value * quantity / bid_value
            integer_digits = quotient.adjusted() + 1
            if integer_digits + IQUP_SCALE > ctx.prec:
                ctx.prec = integer_digits + CTX.prec
                quotient = initial_value * quantity / bid_value
            quantized = quotient.quantize(_IQUP_EXPONENT, rounding=rounding)
            residual = abs(quotient - quantized)
    except decimal.DecimalException:
        # str, not to_wire: a diagnostic, and an extreme exponent is megabytes of
        # digits in fixed point.
        raise SecopError(
            Failure.MALFORMED_PAYLOAD,
            "itemQuantityUnitPrice has no %d-decimal-place value for initialValue=%s, "
            "quantity=%s, bidValue=%s" % (IQUP_SCALE, initial_value, quantity, bid_value),
            {
                "initial_value": str(initial_value),
                "quantity": str(quantity),
                "bid_value": str(bid_value),
            },
        )
    return (quantized, quotient == quantized, residual)


def divides_exactly(
    initial_value: Decimal, quantity: Decimal, bid_value: Decimal
) -> bool:
    """True iff ``initial_value × quantity ÷ bid_value`` terminates within 12 dp.

    This is **divisibility**, not integrality: a whole-number bid value guarantees nothing.
    For the captured ``initial_value`` 390698716 at quantity 1, six of the first twelve
    integers — 3, 6, 7, 9, 11, 12 — do not terminate, so "prefer whole numbers like 1 or 2"
    is not a mitigation. Decided exactly over the rationals, never by comparing quotients.

    A zero numerator or a zero ``bid_value`` is True: that is the zero special case of
    :func:`item_quantity_unit_price`, which is exact by definition.
    """
    if bid_value == 0:
        return True
    with decimal.localcontext(CTX):
        numerator = initial_value * quantity
    if numerator == 0:
        return True
    num_a, num_b = numerator.as_integer_ratio()
    den_a, den_b = bid_value.as_integer_ratio()
    return (num_a * den_b * 10 ** IQUP_SCALE) % (num_b * den_a) == 0


def check_invariant(
    iqup: Decimal, bid_value: Decimal, initial_value: Decimal, quantity: Decimal
) -> bool:
    """``itemQuantityUnitPrice × bidValue == initialValue × quantity``, exactly in Decimal.

    Consistent with all 8 captured bids — including ``1953493.58 × 200`` and
    ``50 × 7813974.32``, both exactly 390698716, with no loss.

    **The quantity factor is UNVERIFIED.** Every captured item has ``quantity == "1.000000"``
    and every captured bid has exactly one item, so the factor is non-discriminating: four
    mutually inconsistent formulas — ``init×q/bid``, ``init/bid`` (no quantity at all),
    ``init×q²/total`` and ``init/(bid×q)`` — reproduce all 8 wire values byte-for-byte. What
    the capture proves is only ``iqup × bidValue == initialValue``. Treat a pass here as a
    formatting/precision check, not as evidence that quantity is handled the way the server
    expects; at ``quantity != 1`` the caller must warn and log the alternatives side by side.
    """
    with decimal.localcontext(CTX):
        return iqup * bid_value == initial_value * quantity


def round_trip_selftest(server_money: str) -> bool:
    """Parse a server amount, re-render it, and report whether the bytes came back (§8.1).

    The re-render uses the original string's own decimal count, so the comparison is against
    the exact bytes the server sent. A parse failure is a failed self-test, not an exception:
    the caller's response is the same either way — refuse to bid.
    """
    text = _strip_currency(server_money)
    _, body = _split_sign(text)
    _, comma, fraction = body.partition(",")
    decimals = len(fraction) if comma else 0
    try:
        parsed = parse_server_money(server_money)
        return format_colombian(parsed, decimals) == text
    except SecopError:
        return False


__all__ = [
    "CTX",
    "IQUP_SCALE",
    "install_context",
    "parse_user_decimal",
    "parse_server_money",
    "format_colombian",
    "to_wire",
    "parse_wire_decimal",
    "total_value",
    "item_quantity_unit_price",
    "divides_exactly",
    "check_invariant",
    "round_trip_selftest",
]
