"""Rule tests. Each rule gets a positive case (should fire) and a negative
case (should stay silent). Documents are constructed by hand — see
`conftest.py` — so tests don't depend on parser behaviour.
"""
from __future__ import annotations

from easyeda_lint.checks import (
    Severity,
    rule_dangling_net,
    rule_duplicate_designator,
    rule_find_missing_i2c_pullups,
    rule_find_missing_reset_pullup,
    rule_find_orphan_net_labels,
    rule_find_orphan_power_objects,
    rule_find_placeholder_values,
    rule_find_schematic_pcb_mismatch,
    rule_find_unconnected_ic_pins,
    rule_find_via_antennas,
    rule_missing_designator,
    rule_missing_value,
    rule_value_designator_mismatch,
)
from easyeda_lint.models import NetLabel, Via, Wire

from .conftest import make_component, make_pcb, make_schematic

# ---------- Tier 1 ----------

def test_duplicate_designator_fires_on_repeat():
    doc = make_pcb([
        make_component("R1", value="10k", package="R0603"),
        make_component("R1", value="4.7k", package="R0603"),
    ])
    findings = list(rule_duplicate_designator(doc))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].rule == "duplicate-designator"
    assert "R1" in findings[0].component


def test_duplicate_designator_silent_on_unique_designators():
    doc = make_pcb([
        make_component("R1", value="10k", package="R0603"),
        make_component("R2", value="4.7k", package="R0603"),
    ])
    assert list(rule_duplicate_designator(doc)) == []


def test_missing_designator_fires():
    doc = make_pcb([make_component(designator="", value="100nF", package="C0603")])
    findings = list(rule_missing_designator(doc))
    assert len(findings) == 1
    assert findings[0].rule == "missing-designator"


def test_missing_value_fires():
    doc = make_pcb([make_component("R1", value="", package="R0603")])
    findings = list(rule_missing_value(doc))
    assert len(findings) == 1
    assert findings[0].rule == "missing-value"


# ---------- Tier 2 ----------

def test_value_designator_mismatch_catches_1NF_vs_100nF():
    doc = make_pcb([make_component("1NF", value="100nF", package="C0603")])
    findings = list(rule_value_designator_mismatch(doc))
    assert len(findings) == 1
    assert findings[0].rule == "value-designator-mismatch"


def test_value_designator_mismatch_silent_when_matching():
    doc = make_pcb([make_component("100NF", value="100nF", package="C0603")])
    assert list(rule_value_designator_mismatch(doc)) == []


def test_value_designator_mismatch_silent_on_non_value_designator():
    """`R1` isn't a value hint, so nothing to compare."""
    doc = make_pcb([make_component("R1", value="10k", package="R0603")])
    assert list(rule_value_designator_mismatch(doc)) == []


# ---------- Tier 3 (single-doc) ----------

def test_dangling_net_fires_on_single_pad_net():
    doc = make_pcb([
        make_component("R1", value="10k", package="R0603",
                       pins=[("1", "ONLY_ME"), ("2", "GND")]),
    ])
    findings = list(rule_dangling_net(doc))
    assert any(f.rule == "dangling-net" and "ONLY_ME" in f.message for f in findings)


def test_dangling_net_ignores_S_prefix_and_gnd():
    doc = make_pcb([
        make_component("R1", value="10k", package="R0603",
                       pins=[("1", "S$1234"), ("2", "GND")]),
    ])
    assert list(rule_dangling_net(doc)) == []


def test_placeholder_values_fires():
    doc = make_pcb([make_component("R1", value="?", package="R0603")])
    findings = list(rule_find_placeholder_values(doc))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_placeholder_values_silent_on_real_value():
    doc = make_pcb([make_component("R1", value="10k", package="R0603")])
    assert list(rule_find_placeholder_values(doc)) == []


def test_orphan_net_label_fires_when_no_wire_nearby():
    doc = make_schematic()
    doc.net_labels.append(NetLabel(x=100, y=100, text="SDA", kind="net"))
    findings = list(rule_find_orphan_net_labels(doc))
    assert len(findings) == 1


def test_orphan_net_label_silent_when_wire_touches():
    doc = make_schematic()
    doc.wires.append(Wire(points=[(100.0, 100.0), (200.0, 100.0)]))
    doc.net_labels.append(NetLabel(x=100, y=100, text="SDA", kind="net"))
    assert list(rule_find_orphan_net_labels(doc)) == []


def test_orphan_power_object_is_error_level():
    doc = make_schematic()
    doc.net_labels.append(NetLabel(x=100, y=100, text="GND", kind="ground"))
    findings = list(rule_find_orphan_power_objects(doc))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_via_antenna_fires_when_no_track_nearby():
    doc = make_pcb()
    doc.vias.append(Via(x=50, y=50, net="MYNET"))
    findings = list(rule_find_via_antennas(doc))
    assert len(findings) == 1
    assert findings[0].rule == "find_via_antennas"


def test_via_antenna_silent_when_track_meets_it():
    from easyeda_lint.models import Track
    doc = make_pcb()
    doc.vias.append(Via(x=50, y=50, net="MYNET"))
    doc.tracks.append(Track(layer=1, net="MYNET",
                           points=[(50.0, 50.0), (100.0, 50.0)]))
    assert list(rule_find_via_antennas(doc)) == []


# ---------- Tier 4 ----------

def test_unconnected_ic_pin_fires_for_small_ic():
    doc = make_pcb([
        make_component("U1", value="", device="SOMETHING", package="SOT-25",
                       pins=[("1", "VCC"), ("2", "GND"), ("3", None), ("4", "OUT")]),
    ])
    findings = list(rule_find_unconnected_ic_pins(doc))
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN


def test_unconnected_ic_pin_downgrades_for_module():
    """A ≥20-pin module with unused GPIOs is normal — report as INFO, not WARN."""
    pins = [(str(i), None if i > 4 else "NET") for i in range(1, 25)]
    doc = make_pcb([
        make_component("U1", value="", device="ESP32", package="MODULE",
                       pins=pins),
    ])
    findings = list(rule_find_unconnected_ic_pins(doc))
    assert findings, "expected at least one finding"
    assert all(f.severity is Severity.INFO for f in findings)


def test_unconnected_ic_pin_skips_resistors_and_caps():
    """Passives with an unrouted pin get caught by dangling-net; don't
    duplicate the finding here."""
    doc = make_pcb([
        make_component("R1", value="10k", package="R0603",
                       pins=[("1", "SIG"), ("2", None)]),
    ])
    assert list(rule_find_unconnected_ic_pins(doc)) == []


def test_missing_i2c_pullup_fires():
    """SDA net exists but no resistor bridges it to any non-GND non-I2C net."""
    doc = make_pcb([
        make_component("U1", value="", device="MCU", package="TDFN-8",
                       pins=[("1", "VCC"), ("2", "GND"),
                             ("3", "SDA"), ("4", "SCL")]),
    ])
    findings = list(rule_find_missing_i2c_pullups(doc))
    rules = {f.rule for f in findings}
    assert "find_missing_i2c_pullups" in rules
    assert sum(1 for f in findings if f.rule == "find_missing_i2c_pullups") == 2  # SDA + SCL


def test_missing_i2c_pullup_silent_when_pullup_present():
    doc = make_pcb([
        make_component("U1", value="", device="MCU", package="TDFN-8",
                       pins=[("1", "VCC"), ("2", "GND"),
                             ("3", "SDA"), ("4", "SCL")]),
        make_component("R1", value="4.7k", package="R0603",
                       pins=[("1", "SDA"), ("2", "3V3")]),
        make_component("R2", value="4.7k", package="R0603",
                       pins=[("1", "SCL"), ("2", "3V3")]),
    ])
    assert list(rule_find_missing_i2c_pullups(doc)) == []


def test_missing_reset_pullup_is_info_not_warn():
    doc = make_pcb([
        make_component("U1", value="", device="MCU", package="TDFN-8",
                       pins=[("1", "VCC"), ("2", "RESET"), ("3", "GND")]),
    ])
    findings = list(rule_find_missing_reset_pullup(doc))
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO


# ---------- Cross-check ----------

def test_schematic_pcb_mismatch_fires_on_only_one_side():
    sch = make_schematic([make_component("R1", supplier_part="C1", package="R0603")])
    pcb = make_pcb([make_component("R2", supplier_part="C1", package="R0603")])
    findings = list(rule_find_schematic_pcb_mismatch(sch, pcb))
    # Rename detection collapses R1↔R2 to a single WARN because they share
    # supplier_part + package + are the only candidates on each side.
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN
    assert "R1" in findings[0].component and "R2" in findings[0].component


def test_schematic_pcb_mismatch_flags_true_orphan():
    sch = make_schematic([make_component("R1", supplier_part="C1", package="R0603")])
    pcb = make_pcb([make_component("U99", supplier_part="C9999", package="QFN-20")])
    findings = list(rule_find_schematic_pcb_mismatch(sch, pcb))
    rules = [(f.rule, f.severity, f.component) for f in findings]
    assert (any(sev is Severity.ERROR for _, sev, _ in rules))


def test_schematic_pcb_mismatch_package_drift():
    sch = make_schematic([make_component("U1", supplier_part="C1", package="SOT-23")])
    pcb = make_pcb([make_component("U1", supplier_part="C1", package="SOT-25")])
    findings = list(rule_find_schematic_pcb_mismatch(sch, pcb))
    assert any("package differs" in f.message for f in findings)
