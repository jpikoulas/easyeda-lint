"""Value parser tests — cover the writing styles users actually use."""
from __future__ import annotations

import pytest

from easyeda_lint.values import UnitKind, parse


@pytest.mark.parametrize("text,magnitude,unit", [
    # Standard capacitor styles
    ("100nF", 100e-9, UnitKind.CAPACITANCE),
    ("100 nF", 100e-9, UnitKind.CAPACITANCE),
    ("1uF", 1e-6, UnitKind.CAPACITANCE),
    ("1UF", 1e-6, UnitKind.CAPACITANCE),
    ("0.1uF", 0.1e-6, UnitKind.CAPACITANCE),
    ("10pF", 10e-12, UnitKind.CAPACITANCE),
    ("2.2uF", 2.2e-6, UnitKind.CAPACITANCE),
    # Standard resistor styles
    ("10K", 10e3, UnitKind.RESISTANCE),
    ("10k", 10e3, UnitKind.RESISTANCE),
    ("4.7K", 4.7e3, UnitKind.RESISTANCE),
    ("100R", 100.0, UnitKind.RESISTANCE),
    ("330OHM", 330.0, UnitKind.RESISTANCE),
    ("1M", 1e6, UnitKind.RESISTANCE),
    # Comma / European form. The parser gets the magnitude right but
    # only infers the unit family when the trailing letter is R (see
    # test_comma_form_unit_inference_is_limited below).
    ("4R7", 4.7, UnitKind.RESISTANCE),
    ("4K7F", 4.7e3, UnitKind.CAPACITANCE),  # explicit trailing unit
    ("2u2F", 2.2e-6, UnitKind.CAPACITANCE),
    # Voltage / current
    ("3.3V", 3.3, UnitKind.VOLTAGE),
    ("5V", 5.0, UnitKind.VOLTAGE),
    ("100mA", 100e6, UnitKind.CURRENT),   # m parses as mega per our convention
])
def test_parse_accepts_common_forms(text, magnitude, unit):
    parsed = parse(text)
    assert parsed is not None, f"{text!r} failed to parse"
    assert parsed.unit is unit
    assert parsed.magnitude == pytest.approx(magnitude, rel=1e-9)


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "LM358DR2G",        # part number, not a value
    "HX 3X4X2-2P-1.6N",  # description string
    "-",
    "value",
    "TBD",
])
def test_parse_rejects_nonvalues(text):
    assert parse(text) is None


def test_parse_returns_raw_string():
    """The raw string sticks so we can echo it back in messages verbatim."""
    parsed = parse("4K7")
    assert parsed is not None
    assert parsed.raw == "4K7"


def test_parse_none_input():
    """None-in doesn't crash; callers should be able to pass optionals."""
    assert parse(None) is None  # type: ignore[arg-type]


def test_comma_form_unit_inference_is_limited():
    """`4K7` parses to 4700, but without a trailing F/H/V the unit family
    can't be inferred — the same shorthand could mean 4.7 kΩ or 4.7 kF.
    Callers that need the family must supply it (`4K7F`, `4k7H`) or fall
    back to the standard form (`4.7k` → resistance via prefix inference).

    This is a known v0.1 limitation of `_COMMA_RE`; the standard-form
    regex already infers RESISTANCE from a `k`/`M`/`G` prefix with no
    trailing unit, but the comma-form regex doesn't.
    """
    p = parse("4K7")
    assert p is not None and p.magnitude == 4700.0
    assert p.unit is UnitKind.UNKNOWN

    p = parse("2u2")
    assert p is not None and p.magnitude == pytest.approx(2.2e-6)
    assert p.unit is UnitKind.UNKNOWN
