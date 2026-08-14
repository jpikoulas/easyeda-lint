"""Rules that run against a parsed Document and produce Findings.

Each check is a small function that yields Findings. Adding a new rule = write
a function + append to `ALL_CHECKS`. Keep them focused: one rule = one thing to
report.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator

from .models import Component, Document, DocKind
from . import values


class Severity(str, Enum):
    INFO = "info"       # observation, not a problem
    WARN = "warn"       # probably wrong, worth reviewing
    ERROR = "error"     # almost certainly wrong


@dataclass
class Finding:
    severity: Severity
    rule: str            # short id like "missing-value"
    message: str
    component: str = ""  # designator if applicable
    where: str = ""      # e.g. "schematic" or "pcb" or a line/section hint


CheckFn = Callable[[Document], Iterator[Finding]]


# ---------- individual rules ----------

def rule_missing_value(doc: Document) -> Iterator[Finding]:
    """Any component missing a value field."""
    for c in doc.components:
        if not c.has_value:
            yield Finding(
                severity=Severity.WARN,
                rule="missing-value",
                message=f"component has no value (device={c.device or '?'}, package={c.package or '?'})",
                component=c.designator or f"<no designator @ line {c.source_line}>",
                where=doc.kind.value,
            )


def rule_missing_designator(doc: Document) -> Iterator[Finding]:
    """Any component missing a reference designator."""
    for c in doc.components:
        if not c.designator:
            yield Finding(
                severity=Severity.WARN,
                rule="missing-designator",
                message=f"component has no reference designator (device={c.device or '?'})",
                component=f"<line {c.source_line}>",
                where=doc.kind.value,
            )


def rule_duplicate_designator(doc: Document) -> Iterator[Finding]:
    """Two components with the same reference designator."""
    counts = Counter(c.designator for c in doc.components if c.designator)
    for des, n in counts.items():
        if n > 1:
            yield Finding(
                severity=Severity.ERROR,
                rule="duplicate-designator",
                message=f"designator used by {n} components",
                component=des,
                where=doc.kind.value,
            )


def rule_missing_part_number(doc: Document) -> Iterator[Finding]:
    """Purely-informational: which components have no supplier part number in BOM."""
    for c in doc.components:
        if not c.has_part:
            yield Finding(
                severity=Severity.INFO,
                rule="no-part-number",
                message="no manufacturer / supplier part number set",
                component=c.designator or f"<line {c.source_line}>",
                where=doc.kind.value,
            )


def rule_unparseable_value(doc: Document) -> Iterator[Finding]:
    """Value is present but doesn't parse as an electrical quantity."""
    for c in doc.components:
        if not c.has_value:
            continue
        # Anything without a plausible R/C/L/V hint we don't complain about —
        # transistors, ICs and connectors often carry the part number as value.
        if not _looks_like_passive(c):
            continue
        pv = values.parse(c.value)
        if pv is None:
            yield Finding(
                severity=Severity.WARN,
                rule="unparseable-value",
                message=f"value {c.value!r} on a passive-looking component isn't a recognisable electrical quantity",
                component=c.designator,
                where=doc.kind.value,
            )


def rule_value_designator_mismatch(doc: Document) -> Iterator[Finding]:
    """Designator label suggests one unit family, value parses as another.

    Catches the "1NF designator on a 100nF cap" class of mistake — the
    designator label reads as 1 nF, but the value field says 100 nF. Only
    fires when we can confidently classify both.
    """
    for c in doc.components:
        if not c.has_value or not c.designator:
            continue
        val_parsed = values.parse(c.value)
        des_parsed = values.parse(c.designator)
        if val_parsed is None or des_parsed is None:
            continue
        if val_parsed.unit is values.UnitKind.UNKNOWN or des_parsed.unit is values.UnitKind.UNKNOWN:
            continue
        if val_parsed.unit != des_parsed.unit:
            yield Finding(
                severity=Severity.WARN,
                rule="value-designator-mismatch",
                message=(
                    f"designator {c.designator!r} parses as {val_parsed.unit.name} "
                    f"but value {c.value!r} parses as {val_parsed.unit.name}"
                ),
                component=c.designator,
                where=doc.kind.value,
            )
            continue
        # Same family, but very different magnitude — likely a slip.
        if val_parsed.magnitude and des_parsed.magnitude:
            ratio = val_parsed.magnitude / des_parsed.magnitude
            if ratio > 10 or ratio < 0.1:
                yield Finding(
                    severity=Severity.WARN,
                    rule="value-designator-mismatch",
                    message=(
                        f"designator {c.designator!r} implies {values.format_value(des_parsed)}, "
                        f"value {c.value!r} parses as {values.format_value(val_parsed)}"
                    ),
                    component=c.designator,
                    where=doc.kind.value,
                )


def rule_dangling_net(doc: Document) -> Iterator[Finding]:
    """Net that only reaches one pad — probably a routing error.

    Only meaningful on PCB docs where we have real net info from pads.
    """
    if doc.kind is not DocKind.PCB:
        return
    for net, pads in doc.nets.items():
        if net.upper() == "GND":  # ground pours are fine as single-pad from any given LIB
            continue
        if len(pads) < 2:
            comp_hint = pads[0][0] if pads else "<none>"
            yield Finding(
                severity=Severity.WARN,
                rule="dangling-net",
                message=f"net {net!r} only connects to one pad ({comp_hint} pin {pads[0][1] if pads else '?'})",
                where=doc.kind.value,
            )


# ---------- registry + entry point ----------

ALL_CHECKS: list[CheckFn] = [
    rule_duplicate_designator,
    rule_missing_designator,
    rule_missing_value,
    rule_unparseable_value,
    rule_value_designator_mismatch,
    rule_missing_part_number,
    rule_dangling_net,
]


def run_all(doc: Document) -> list[Finding]:
    findings: list[Finding] = []
    for check in ALL_CHECKS:
        findings.extend(check(doc))
    return findings


# ---------- helpers ----------

import re

# Strict passive detection: the *package* names give the cleanest signal.
# R0402 / C0603 / L0603 unambiguously identify passive footprints, and any
# device explicitly typed "CAP" / "Res" / "Ind" is a passive too. Substring
# matching on the designator (e.g. "RES" inside "RESET") causes false
# positives, so we don't do that.
_PASSIVE_PACKAGE_RE = re.compile(r"^\s*([RCL])(\d{4})\b", re.IGNORECASE)
_PASSIVE_DEVICE_TOKENS = frozenset({
    "cap", "capacitor", "res", "resistor", "ind", "inductor",
})


def _looks_like_passive(c: Component) -> bool:
    """Does this component slot expect an electrical value (R/L/C footprint)?"""
    if _PASSIVE_PACKAGE_RE.match(c.package):
        return True
    device_lc = c.device.strip().lower()
    if device_lc in _PASSIVE_DEVICE_TOKENS:
        return True
    return False
