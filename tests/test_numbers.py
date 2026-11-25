"""Tests for :mod:`secop_firstbid.numbers` (plan §8).

The oracle is ``tests/fixtures/validatebid_requests.json`` — the 8 real ``ValidateBid``
bodies. Every formula assertion here is a **string** comparison against those wire values,
because the defect this module has to survive is a formatting one: a normalized Decimal is
numerically identical and byte-wrong.

What the 8 bids cannot decide is recorded too: they all carry one item at
``quantity == "1.000000"``, so the quantity factor of ``itemQuantityUnitPrice`` is
non-discriminating. ``test_quantity_two_*`` documents that gap instead of papering over it.
"""

from __future__ import annotations

import ast
import decimal
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import INITIAL_VALUE, QUANTITY_STR
from secop_firstbid import numbers
from secop_firstbid.errors import Failure, SecopError

QUANTITY = Decimal(QUANTITY_STR)


def _items(requests):
    for body in requests:
        assert len(body["items"]) == 1
        yield body, body["items"][0]


def test_fixture_holds_the_eight_captured_bids(validatebid_requests):
    assert len(validatebid_requests) == 8
    values = [item["bidValue"] for _, item in _items(validatebid_requests)]
    assert sorted(values) == ["1", "1", "10", "10", "10", "10", "200", "7813974.32"]
    assert {item["quantity"] for _, item in _items(validatebid_requests)} == {"1.000000"}


def test_total_value_reproduces_all_eight_wire_strings(validatebid_requests):
    for body, item in _items(validatebid_requests):
        bid_value = numbers.parse_wire_decimal(item["bidValue"])
        computed = numbers.total_value(QUANTITY, bid_value)
        assert numbers.to_wire(computed) == item["totalValue"]
        assert numbers.to_wire(computed) == body["totalValue"]


def test_item_quantity_unit_price_reproduces_all_eight_wire_strings(validatebid_requests):
    for _, item in _items(validatebid_requests):
        bid_value = numbers.parse_wire_decimal(item["bidValue"])
        value, is_exact, residual = numbers.item_quantity_unit_price(
            INITIAL_VALUE, QUANTITY, bid_value
        )
        assert numbers.to_wire(value) == item["itemQuantityUnitPrice"]
        assert is_exact is True
        assert residual == 0
        assert numbers.divides_exactly(INITIAL_VALUE, QUANTITY, bid_value) is True


def test_every_captured_iqup_is_twelve_decimal_places(validatebid_requests):
    for _, item in _items(validatebid_requests):
        assert len(item["itemQuantityUnitPrice"].split(".")[1]) == numbers.IQUP_SCALE


def test_invariant_holds_for_all_eight(validatebid_requests):
    for _, item in _items(validatebid_requests):
        bid_value = numbers.parse_wire_decimal(item["bidValue"])
        iqup = numbers.parse_wire_decimal(item["itemQuantityUnitPrice"])
        assert numbers.check_invariant(iqup, bid_value, INITIAL_VALUE, QUANTITY) is True


def test_scale_adds_on_multiply_and_zeros_survive():
    assert numbers.to_wire(numbers.total_value(Decimal("1.000000"), Decimal("10"))) == (
        "10.000000"
    )
    assert numbers.to_wire(
        numbers.total_value(Decimal("1.000000"), Decimal("7813974.32"))
    ) == "7813974.32000000"
    assert numbers.to_wire(Decimal("10.000000")) == "10.000000"
    assert numbers.to_wire(Decimal("390698716.000000000000")) == "390698716.000000000000"


@pytest.mark.parametrize(
    "value",
    ["0.000000000000", "0.000000001000", "0.0000001", "0.00000000000001"],
)
def test_small_values_stay_in_fixed_point(value):
    """``str(Decimal)`` would emit ``0E-12`` / ``1.000E-9`` here.

    Exponent notation has never been sent to this server. It appears exactly where a
    12-dp ``itemQuantityUnitPrice`` gets small — including the documented zero case — so a
    ``str()``-based wire conversion puts an unparseable value in the payload at the one
    moment nobody is watching.
    """
    rendered = numbers.to_wire(Decimal(value))
    assert rendered == value
    assert "E" not in rendered and "e" not in rendered


def test_module_never_normalizes():
    tree = ast.parse(Path(numbers.__file__).read_text(encoding="utf-8"))
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert "normalize" not in attributes


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "1"),
        ("1234567.89", "1234567.89"),
        ("7.813.974,32", "7813974.32"),
        ("7.813.974,32 COP", "7813974.32"),
        ("390.698.716", "390698716"),
        ("7813974.32", "7813974.32"),
        ("1.000000", "1.000000"),
        ("  200  ", "200"),
        ("1234567,89", "1234567.89"),
    ],
)
def test_parse_user_decimal_accepts(text, expected):
    parsed = numbers.parse_user_decimal(text)
    assert numbers.to_wire(parsed) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1.234",
        "1.500",
        "390.698.716,",
        "1.23.456",
        "",
        "abc",
        "1,2,3",
        "1..2",
        "12 34",
        "1.234,56.78",
    ],
)
def test_parse_user_decimal_rejects(text):
    with pytest.raises(SecopError) as excinfo:
        numbers.parse_user_decimal(text)
    assert excinfo.value.code is Failure.CONFIG_INVALID


def test_the_ambiguous_single_dot_names_both_readings():
    with pytest.raises(SecopError) as excinfo:
        numbers.parse_user_decimal("1.234")
    assert excinfo.value.detail["colombian"] == "1234"
    assert excinfo.value.detail["wire"] == "1.234"


def test_a_four_digit_fraction_is_not_ambiguous():
    assert numbers.to_wire(numbers.parse_user_decimal("1.2345")) == "1.2345"


def test_parse_server_money_and_round_trip():
    assert numbers.parse_server_money("7.813.974,32 COP") == Decimal("7813974.32")
    assert numbers.round_trip_selftest("7.813.974,32 COP") is True
    assert numbers.round_trip_selftest("7.813.974,32") is True
    assert numbers.format_colombian(Decimal("7813974.32")) == "7.813.974,32"
    assert numbers.format_colombian(Decimal("390698716"), 0) == "390.698.716"
    assert numbers.round_trip_selftest("no soy un numero") is False


def test_min_bid_from_the_captured_responses_round_trips(validatebid_responses):
    min_bids = {body["minBid"] for body in validatebid_responses}
    assert min_bids == {"7.813.974,32 COP"}
    for text in min_bids:
        assert numbers.round_trip_selftest(text) is True


@pytest.mark.parametrize("divisor", [1, 2, 4, 5, 8, 10])
def test_divides_exactly_terminates(divisor):
    assert (
        numbers.divides_exactly(INITIAL_VALUE, QUANTITY, Decimal(divisor)) is True
    )


@pytest.mark.parametrize("divisor", [3, 6, 7, 9, 11, 12])
def test_divides_exactly_does_not_terminate(divisor):
    assert (
        numbers.divides_exactly(INITIAL_VALUE, QUANTITY, Decimal(divisor)) is False
    )


def test_half_of_the_first_twelve_integers_do_not_terminate():
    failing = [
        n
        for n in range(1, 13)
        if not numbers.divides_exactly(INITIAL_VALUE, QUANTITY, Decimal(n))
    ]
    assert failing == [3, 6, 7, 9, 11, 12]


def test_inexact_division_is_reported_with_its_residual():
    value, is_exact, residual = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, Decimal(3)
    )
    assert numbers.to_wire(value) == "130232905.333333333333"
    assert is_exact is False
    assert residual > 0
    assert numbers.divides_exactly(INITIAL_VALUE, QUANTITY, Decimal(3)) is False


def test_a_bid_value_too_small_for_prec_50_still_yields_twelve_decimal_places():
    # parse_user_decimal accepts this, so build_payload has to survive it: the quotient
    # needs 39 integer digits and quantizing to 12 dp overflows a prec-50 context.
    bid_value = numbers.parse_user_decimal("0." + "0" * 29 + "1")
    value, is_exact, residual = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, bid_value
    )

    assert numbers.to_wire(value) == "390698716" + "0" * 30 + "." + "0" * 12
    assert is_exact is True
    assert residual == 0
    assert numbers.check_invariant(value, bid_value, INITIAL_VALUE, QUANTITY)


def test_the_widened_quotient_still_reports_a_non_terminating_division():
    bid_value = Decimal("3E-31")
    value, is_exact, residual = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, bid_value
    )

    assert numbers.to_wire(value).endswith(".333333333333")
    assert is_exact is False
    assert residual > 0
    assert numbers.divides_exactly(INITIAL_VALUE, QUANTITY, bid_value) is False


def test_an_unrepresentable_quotient_is_a_coded_failure_not_a_decimal_exception():
    with pytest.raises(SecopError) as excinfo:
        numbers.item_quantity_unit_price(
            Decimal("1E999990"), QUANTITY, Decimal("1E-20")
        )

    assert excinfo.value.code is Failure.MALFORMED_PAYLOAD
    assert excinfo.value.detail["bid_value"] == "1E-20"
    assert "initialValue" in excinfo.value.message


def test_rounding_mode_is_a_parameter():
    half_up, _, _ = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, Decimal(3), rounding=decimal.ROUND_HALF_UP
    )
    up, _, _ = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, Decimal(3), rounding=decimal.ROUND_UP
    )
    assert up > half_up


def test_zero_special_cases():
    assert numbers.item_quantity_unit_price(Decimal(0), QUANTITY, Decimal(10)) == (
        Decimal("0.000000000000"),
        True,
        Decimal(0),
    )
    assert numbers.item_quantity_unit_price(INITIAL_VALUE, QUANTITY, Decimal(0)) == (
        Decimal("0.000000000000"),
        True,
        Decimal(0),
    )


def test_quantity_two_separates_formulas_the_capture_cannot_choose_between():
    bid_value = Decimal("10")
    two = Decimal("2.000000")

    with_quantity, _, _ = numbers.item_quantity_unit_price(INITIAL_VALUE, two, bid_value)
    without_quantity, _, _ = numbers.item_quantity_unit_price(
        INITIAL_VALUE, Decimal(1), bid_value
    )

    assert with_quantity != without_quantity
    assert numbers.to_wire(with_quantity) == "78139743.200000000000"
    assert numbers.to_wire(without_quantity) == "39069871.600000000000"

    at_one_with, _, _ = numbers.item_quantity_unit_price(
        INITIAL_VALUE, QUANTITY, bid_value
    )
    assert at_one_with == without_quantity


def test_the_invariant_only_discriminates_the_formulas_away_from_quantity_one():
    bid_value = Decimal("10")
    two = Decimal("2.000000")
    without_quantity = INITIAL_VALUE / bid_value

    assert numbers.check_invariant(without_quantity, bid_value, INITIAL_VALUE, QUANTITY)
    assert not numbers.check_invariant(without_quantity, bid_value, INITIAL_VALUE, two)

    with_quantity, _, _ = numbers.item_quantity_unit_price(INITIAL_VALUE, two, bid_value)
    assert numbers.check_invariant(with_quantity, bid_value, INITIAL_VALUE, two)


def test_a_hostile_ambient_context_cannot_shorten_the_wire_values():
    with decimal.localcontext() as ctx:
        ctx.prec = 6
        value, is_exact, _ = numbers.item_quantity_unit_price(
            INITIAL_VALUE, QUANTITY, Decimal("10")
        )
        assert numbers.to_wire(value) == "39069871.600000000000"
        assert is_exact is True
        assert numbers.to_wire(
            numbers.total_value(QUANTITY, Decimal("7813974.32"))
        ) == "7813974.32000000"


def test_install_context_sets_prec_50():
    original = decimal.getcontext()
    try:
        numbers.install_context()
        assert decimal.getcontext().prec == 50
    finally:
        decimal.setcontext(original)


def test_parse_wire_decimal_preserves_scale_and_rejects_non_numbers():
    assert numbers.to_wire(numbers.parse_wire_decimal("10.000000")) == "10.000000"
    for text in ("NaN", "Infinity", "7.813.974,32", ""):
        with pytest.raises(SecopError) as excinfo:
            numbers.parse_wire_decimal(text)
        assert excinfo.value.code is Failure.PARSE_FAILED
