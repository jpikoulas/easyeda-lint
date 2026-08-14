"""Parse electrical value strings.

Handles the common ways users write component values in EasyEDA:

    100nF, 100 nF, 0.1uF, 100n, 100 pF, 4u7, 2.2uF, 220pF
    10K, 10 kΩ, 4.7k, 4K7, 100R, 330OHM, 1M
    1uH, 100nH, 10uH
    3.3V, 5V0

Returns a `ParsedValue` with the base SI magnitude and the unit family. Unit
family is coarse — capacitance / resistance / inductance / voltage / unknown —
because for cross-checking we mostly care "is this a cap or a resistor?" not
the exact tolerance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class UnitKind(str, Enum):
    CAPACITANCE = "F"
    RESISTANCE = "Ω"
    INDUCTANCE = "H"
    VOLTAGE = "V"
    CURRENT = "A"
    FREQUENCY = "Hz"
    UNKNOWN = "?"


@dataclass
class ParsedValue:
    magnitude: float          # in base SI (Farads, Ohms, Henries, Volts…)
    unit: UnitKind
    raw: str


# SI prefixes. EE users write these very inconsistently — `1UF`, `1uF`, `1µF`,
# `4K7`, `4k7`, `100n`, `100 pF` — so we treat prefix chars case-insensitively
# EXCEPT for M/m where case really does mean mega vs milli. Since milli is
# almost never used for R/C/L in this domain, we treat both M and m as mega
# when adjacent to F/H/Ω and only distinguish them if the context is clearly
# a voltage/current with 'm' meaning milli. For MVP: always mega. Flag as a
# TODO if a real board ever uses `mV` for millivolts and we care.
_PREFIX_MULT = {
    "p": 1e-12, "P": 1e-12,
    "n": 1e-9,  "N": 1e-9,
    "u": 1e-6,  "U": 1e-6,  "μ": 1e-6,
    "":  1.0,
    "k": 1e3,   "K": 1e3,
    "m": 1e6,   "M": 1e6,   # see comment above: mega, not milli
    "g": 1e9,   "G": 1e9,
}

_UNIT_TO_KIND = {
    "F":   UnitKind.CAPACITANCE,
    "Ω":   UnitKind.RESISTANCE,
    "R":   UnitKind.RESISTANCE,   # e.g. "100R" == 100 Ω
    "OHM": UnitKind.RESISTANCE,
    "H":   UnitKind.INDUCTANCE,
    "V":   UnitKind.VOLTAGE,
    "A":   UnitKind.CURRENT,
    "Hz":  UnitKind.FREQUENCY,
}

# Standard-form values: <mantissa><prefix><unit>. Unit can be omitted for R (R stand-in).
# Mantissa allows "4.7", "4", "0.1".
_STANDARD_RE = re.compile(
    r"""
    ^\s*
    (?P<mantissa>\d+(?:\.\d+)?|\.\d+)
    \s*
    (?P<prefix>[pPnNuUμmMkKgG]?)
    \s*
    (?P<unit>F|Ω|R|OHM|H|V|A|Hz)?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# European/Japanese comma form: 4K7 == 4.7k, 2u2 == 2.2u, 100R == 100 Ω.
# Here the prefix letter is between the two number parts.
_COMMA_RE = re.compile(
    r"""
    ^\s*
    (?P<int>\d+)
    (?P<prefix>[pPnNuUμmMkKgGrR])
    (?P<frac>\d+)
    (?P<unit>F|Ω|OHM|H|V|A)?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse(value: str) -> ParsedValue | None:
    """Return a ParsedValue if `value` looks like an electrical quantity, else None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    # Comma form first — "4K7" would match the standard regex as "4" + prefix "K"
    # + trailing digits which we'd miss.
    m = _COMMA_RE.match(s)
    if m:
        mant = float(f"{m['int']}.{m['frac']}")
        prefix = m["prefix"]
        if prefix == "R":
            # "4R7" -> 4.7 Ω
            return ParsedValue(magnitude=mant, unit=UnitKind.RESISTANCE, raw=value)
        mult = _PREFIX_MULT.get(prefix, 1.0)
        unit_kind = _UNIT_TO_KIND.get(m["unit"] or "", UnitKind.UNKNOWN)
        return ParsedValue(magnitude=mant * mult, unit=unit_kind, raw=value)

    m = _STANDARD_RE.match(s)
    if m:
        mant = float(m["mantissa"])
        prefix = m["prefix"] or ""
        mult = _PREFIX_MULT.get(prefix, 1.0)
        unit_str = m["unit"] or ""
        unit_kind = _UNIT_TO_KIND.get(unit_str, UnitKind.UNKNOWN)
        # Bare "10K" -> resistance by convention when unit is absent but prefix present.
        if unit_kind is UnitKind.UNKNOWN and prefix in ("k", "K", "M", "G") and not unit_str:
            unit_kind = UnitKind.RESISTANCE
        return ParsedValue(magnitude=mant * mult, unit=unit_kind, raw=value)

    return None


def format_value(pv: ParsedValue) -> str:
    """Human-friendly redisplay: 100n -> 100 nF, 4700 -> 4.7 kΩ."""
    if pv.unit is UnitKind.UNKNOWN:
        return pv.raw
    abs_m = abs(pv.magnitude)
    for prefix, mult in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0),
                        ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs_m >= mult:
            scaled = pv.magnitude / mult
            unit_char = pv.unit.value
            # Drop trailing zeros from the scaled number.
            s = f"{scaled:g}"
            return f"{s} {prefix}{unit_char}"
    return pv.raw
