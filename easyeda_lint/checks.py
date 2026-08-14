"""Rules that run against a parsed Document and produce Findings.

Each check is a small function that yields Findings. Adding a new rule = write
a function + append to `ALL_CHECKS` (single-document rules) or `CROSS_CHECKS`
(rules that need a schematic AND a PCB to compare). Keep them focused: one
rule = one thing to report.

Naming convention follows salitronic/eda-agent's `LINT_AUDIT_LIST`
(Apache-2.0). Where our own idea has no direct equivalent, we use a short
kebab-case slug.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Iterator, Optional

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

    Only meaningful on PCB docs where we have real net info from pads. We
    skip GND (usually poured, single-pad-per-LIB is normal) and skip nets
    with EasyEDA-internal auto-generated names (`S$...`) — those are given
    to unlabelled silkscreen segments and other non-electrical fragments.
    """
    if doc.kind is not DocKind.PCB:
        return
    for net, pads in doc.nets.items():
        if net.upper() == "GND":
            continue
        if net.startswith("S$"):
            continue
        if len(pads) < 2:
            comp_hint = pads[0][0] if pads else "<none>"
            yield Finding(
                severity=Severity.WARN,
                rule="dangling-net",
                message=f"net {net!r} only connects to one pad ({comp_hint} pin {pads[0][1] if pads else '?'})",
                where=doc.kind.value,
            )


# ---------- Tier 3: geometry rules ----------

# Distance thresholds tuned for EasyEDA coordinate units (schematic grid is
# typically 5 or 10 units; PCB units are 0.254 mm each). Being a bit generous
# reduces false positives from placements that are on-grid but not pixel-exact.
_SCH_EPSILON = 1.0   # schematic units for "same point"
_VIA_EPSILON = 1.5   # PCB units around a via center
_PAD_EPSILON = 3.0   # PCB units around a pad center for track-connectivity


def _points_touch(a: tuple[float, float], b: tuple[float, float], eps: float) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def _wire_endpoints(doc: Document) -> Iterable[tuple[float, float]]:
    """Every endpoint of every schematic wire — both terminals of the polyline
    are returned so we can check whether each end lands on something."""
    for w in doc.wires:
        if not w.points:
            continue
        yield w.points[0]
        yield w.points[-1]


def _all_wire_vertices(doc: Document) -> Iterable[tuple[float, float]]:
    """Every vertex of every wire (endpoints AND interior corners)."""
    for w in doc.wires:
        for pt in w.points:
            yield pt


def _pad_positions(doc: Document) -> Iterable[tuple[float, float]]:
    for c in doc.components:
        for p in c.pins:
            yield (p.x, p.y)


def rule_find_orphan_net_labels(doc: Document) -> Iterator[Finding]:
    """A schematic net label sitting in empty space isn't attached to any
    wire, so whatever the reader thinks it's naming actually has no name."""
    if doc.kind is not DocKind.SCHEMATIC:
        return
    if not doc.net_labels:
        return
    # Only interested in plain net labels here; power objects have their own rule.
    labels = [n for n in doc.net_labels if n.kind == "net"]
    if not labels:
        return
    wire_pts = list(_all_wire_vertices(doc)) + list(_pad_positions(doc))
    for lbl in labels:
        if not any(_points_touch((lbl.x, lbl.y), wp, _SCH_EPSILON) for wp in wire_pts):
            yield Finding(
                severity=Severity.WARN,
                rule="find_orphan_net_labels",
                message=(
                    f"net label {lbl.text!r} at ({lbl.x:g}, {lbl.y:g}) is not attached "
                    f"to any wire or pin — it names nothing"
                ),
                where=doc.kind.value,
            )


def rule_find_orphan_power_objects(doc: Document) -> Iterator[Finding]:
    """VCC/GND symbols placed in empty space have the same problem as net
    labels but silently break power delivery, which is worse."""
    if doc.kind is not DocKind.SCHEMATIC:
        return
    power_flags = [n for n in doc.net_labels if n.kind in ("power", "ground")]
    if not power_flags:
        return
    wire_pts = list(_all_wire_vertices(doc)) + list(_pad_positions(doc))
    for pf in power_flags:
        if not any(_points_touch((pf.x, pf.y), wp, _SCH_EPSILON) for wp in wire_pts):
            yield Finding(
                severity=Severity.ERROR,
                rule="find_orphan_power_objects",
                message=(
                    f"{pf.kind} symbol {pf.text or '?'} at ({pf.x:g}, {pf.y:g}) is not "
                    f"attached to any wire — power/ground connection is broken"
                ),
                where=doc.kind.value,
            )


def rule_find_placeholder_values(doc: Document) -> Iterator[Finding]:
    """Components carrying a placeholder value/designator like `?`, `<value>`
    or `TBD` are ones the designer forgot to fill in before ordering."""
    _PLACEHOLDERS = {"?", "??", "???", "tbd", "todo", "xxx", "<value>", "value"}
    for c in doc.components:
        for label, text in (("value", c.value), ("designator", c.designator)):
            if text and text.strip().lower() in _PLACEHOLDERS:
                yield Finding(
                    severity=Severity.ERROR,
                    rule="find_placeholder_values",
                    message=f"{label} field is a placeholder ({text!r}) — fill it in before ordering",
                    component=c.designator or f"<line {c.source_line}>",
                    where=doc.kind.value,
                )


def rule_find_via_antennas(doc: Document) -> Iterator[Finding]:
    """A via is an antenna if no track segment or pad on its net comes near
    it — it's a stub, a leftover of removed routing, or a mistake."""
    if doc.kind is not DocKind.PCB:
        return
    for via in doc.vias:
        if not via.net:
            continue
        # Look for any track endpoint (or interior vertex) on the same net
        # within a via-radius of the via center.
        touched = False
        for t in doc.tracks:
            if t.net != via.net:
                continue
            for pt in t.points:
                if _points_touch((via.x, via.y), pt, _VIA_EPSILON):
                    touched = True
                    break
            if touched:
                break
        if touched:
            continue
        # Also OK if a pad on the same net sits under the via.
        for _c, p in doc.pads():
            if p.net == via.net and _points_touch((via.x, via.y), (p.x, p.y), _VIA_EPSILON):
                touched = True
                break
        if not touched:
            yield Finding(
                severity=Severity.WARN,
                rule="find_via_antennas",
                message=(
                    f"via at ({via.x:g}, {via.y:g}) on net {via.net!r} has no track "
                    f"or pad within {_VIA_EPSILON:g} units on the same net — "
                    f"unused stub or orphaned routing"
                ),
                where=doc.kind.value,
            )


def rule_find_pads_center_not_connected(doc: Document) -> Iterator[Finding]:
    """A pad whose net has copper elsewhere on the board but nothing coming
    into the pad itself is an unrouted connection — the netlist thinks it
    connects, the copper says otherwise."""
    if doc.kind is not DocKind.PCB:
        return
    # For every pad with a net, check whether any track (or via) on that same
    # net has a point close to the pad center.
    for c, p in doc.pads():
        if not p.net or p.net.upper() == "GND":
            # GND is normally poured, not routed — skip to avoid noise.
            continue
        # Ignore nets carried by only this pad; the dangling-net rule handles it.
        neighbours_on_net = [
            (nc.designator, np.number, np.x, np.y)
            for nc, np in doc.pads()
            if np.net == p.net and (nc.designator, np.number) != (c.designator, p.number)
        ]
        if not neighbours_on_net:
            continue
        connected = False
        for t in doc.tracks:
            if t.net != p.net:
                continue
            for pt in t.points:
                if _points_touch((p.x, p.y), pt, _PAD_EPSILON):
                    connected = True
                    break
            if connected:
                break
        if connected:
            continue
        for via in doc.vias:
            if via.net == p.net and _points_touch((p.x, p.y), (via.x, via.y), _PAD_EPSILON):
                connected = True
                break
        if not connected:
            yield Finding(
                severity=Severity.WARN,
                rule="find_pads_center_not_connected",
                message=(
                    f"pad {c.designator}:{p.number} on net {p.net!r} has no track or "
                    f"via within {_PAD_EPSILON:g} units — pad may be unrouted"
                ),
                component=c.designator,
                where=doc.kind.value,
            )


# ---------- Tier 4: electrical intent ----------

# Net-name patterns for "this is a power rail" identification.
# Uppercase matching — user boards have all-caps net names in practice, but
# we lowercase before comparing to be safe.
_POWER_NET_TOKENS = (
    "vcc", "vdd", "vbus", "vbat", "vin", "vout",
    "vddio", "vdda", "vdd_", "avcc", "avdd", "dvdd", "iovdd",
    "5v", "3v3", "3.3v", "1v8", "1.8v", "12v", "24v",
    "pwr", "power",
)


def _is_power_net(net: str) -> bool:
    """Heuristic: does the net name suggest a power rail (not GND)?

    We deliberately exclude GND-family names — those are handled separately
    as the reference, not as a rail that needs decoupling.
    """
    if not net:
        return False
    lc = net.lower()
    if "gnd" in lc or lc.startswith("agnd") or lc.startswith("dgnd"):
        return False
    # Rail names starting with + or - (like +3V3, -5V) are unambiguous.
    if net[0] in "+-" and any(ch.isdigit() for ch in net):
        return True
    return any(tok in lc for tok in _POWER_NET_TOKENS)


def _is_gnd_net(net: Optional[str]) -> bool:
    if not net:
        return False
    lc = net.lower()
    return "gnd" in lc


def _is_capacitor(c: Component) -> bool:
    """Heuristic: is this component a decoupling-capable capacitor?

    Prefers package tag (C0402/C0603/...) because that's unambiguous; falls
    back to device-string sniffing when the package is generic.
    """
    if _PASSIVE_PACKAGE_RE.match(c.package):
        prefix = c.package.strip()[0].upper()
        if prefix == "C":
            return True
    device_lc = c.device.strip().lower()
    return device_lc in ("cap", "capacitor") or "cap_" in device_lc


def _is_ic_like(c: Component) -> bool:
    """Heuristic: is this component something that needs decoupling?

    ICs, modules, and multi-terminal devices — but not caps, resistors,
    diodes, headers, LEDs, or single-transistor packages.
    """
    if _is_capacitor(c):
        return False
    if _PASSIVE_PACKAGE_RE.match(c.package):
        return False
    if len(c.pins) < 3:
        return False
    hints = " ".join([c.package, c.device]).lower()
    non_ic = ("hdr-", "pinheader", "conn-", "key-", "led-", "sot-23-3")
    if any(h in hints for h in non_ic):
        return False
    return True


def rule_find_unconnected_ic_pins(doc: Document) -> Iterator[Finding]:
    """IC pin with no net — the router forgot to wire it up, or the design
    truly meant to leave it floating and should say so.

    Modules (≥20 pins — ESP32 boards, MCU stamps, WiFi mini-modules) have
    many GPIOs that are legitimately unused; on those we downgrade to INFO
    so the fact is visible but doesn't scream. Small ICs still get WARN
    because unused pins on a 5-pin part are almost always a mistake.
    """
    if doc.kind is not DocKind.PCB:
        return
    for c in doc.components:
        if not _is_ic_like(c):
            continue
        is_module = len(c.pins) >= 20
        sev = Severity.INFO if is_module else Severity.WARN
        for p in c.pins:
            if not p.net:
                yield Finding(
                    severity=sev,
                    rule="find_unconnected_ic_pins",
                    message=(
                        f"pin {p.number} has no net assigned"
                        + (" — unused module GPIO" if is_module else " — floating input?")
                    ),
                    component=c.designator,
                    where=doc.kind.value,
                )


def rule_find_missing_decoupling(doc: Document) -> Iterator[Finding]:
    """Every power rail feeding an IC should have at least one decoupling
    capacitor between it and GND. If the rail has IC consumers but zero
    caps to GND, flag it — that's a classic \"forgot the bypass\" bug.

    Not proximity-checked in this MVP — we only care that *a* cap exists.
    A Tier 5 rule can later enforce \"within N mm of the pin.\"
    """
    if doc.kind is not DocKind.PCB:
        return

    # 1. Build the set of power rails that IC pins actually consume.
    ic_power_nets: set[str] = set()
    for c in doc.components:
        if not _is_ic_like(c):
            continue
        for p in c.pins:
            if p.net and _is_power_net(p.net):
                ic_power_nets.add(p.net)

    if not ic_power_nets:
        return

    # 2. Build the set of power rails that already have a cap to GND.
    decoupled: set[str] = set()
    for c in doc.components:
        if not _is_capacitor(c):
            continue
        nets = [p.net for p in c.pins if p.net]
        if len(nets) < 2:
            continue
        has_gnd = any(_is_gnd_net(n) for n in nets)
        if not has_gnd:
            continue
        for n in nets:
            if not _is_gnd_net(n) and _is_power_net(n):
                decoupled.add(n)

    # 3. Flag every IC-consumed power rail without a matching cap.
    for net in sorted(ic_power_nets - decoupled):
        # Name the ICs consuming this rail so the finding is actionable.
        consumers = [
            (c.designator, p.number)
            for c in doc.components if _is_ic_like(c)
            for p in c.pins if p.net == net
        ]
        summary = ", ".join(f"{d}:{n}" for d, n in consumers[:3])
        if len(consumers) > 3:
            summary += f", … (+{len(consumers) - 3} more)"
        yield Finding(
            severity=Severity.WARN,
            rule="find_missing_decoupling",
            message=(
                f"power net {net!r} feeds IC pins ({summary}) but has no "
                f"capacitor to GND anywhere on the board"
            ),
            where=doc.kind.value,
        )


# ---------- registry + entry point ----------

ALL_CHECKS: list[CheckFn] = [
    # Tier 1
    rule_duplicate_designator,
    rule_missing_designator,
    rule_missing_value,
    rule_missing_part_number,
    # Tier 2
    rule_unparseable_value,
    rule_value_designator_mismatch,
    # Tier 3
    rule_dangling_net,
    rule_find_orphan_net_labels,
    rule_find_orphan_power_objects,
    rule_find_placeholder_values,
    rule_find_via_antennas,
    rule_find_pads_center_not_connected,
    # Tier 4
    rule_find_unconnected_ic_pins,
    rule_find_missing_decoupling,
]


# ---------- cross-document checks (schematic vs PCB) ----------

CrossCheckFn = Callable[[Document, Document], Iterator[Finding]]


def _match_by_identity(
    only_sch: set[str],
    only_pcb: set[str],
    sch_by_des: dict[str, Component],
    pcb_by_des: dict[str, Component],
) -> list[tuple[str, str]]:
    """Pair unmatched schematic/PCB designators that clearly belong together.

    Two components with different designators but the *same* supplier part
    number and package are almost certainly a rename (`BATT_CONN` on the
    schematic, `BATT` on the PCB). Returning those as pairs lets the caller
    downgrade the noisy "missing on one side" errors to a single "renamed"
    finding.
    """
    def key(c: Component) -> Optional[tuple[str, str]]:
        if c.supplier_part and c.package:
            return (c.supplier_part, c.package)
        if c.manufacturer_part and c.package:
            return (c.manufacturer_part, c.package)
        return None

    sch_lookup: dict[tuple[str, str], list[str]] = {}
    for des in only_sch:
        k = key(sch_by_des[des])
        if k:
            sch_lookup.setdefault(k, []).append(des)

    pairs: list[tuple[str, str]] = []
    matched_pcb: set[str] = set()
    for des in only_pcb:
        k = key(pcb_by_des[des])
        if not k:
            continue
        candidates = sch_lookup.get(k, [])
        if len(candidates) == 1 and candidates[0] not in matched_pcb:
            pairs.append((candidates[0], des))
            matched_pcb.add(candidates[0])
    return pairs


def rule_find_schematic_pcb_mismatch(sch: Document, pcb: Document) -> Iterator[Finding]:
    """Every component in the schematic should have a matching one in the PCB
    (by designator) and vice versa. Mismatched supplier part numbers or
    packages between the two sides are a strong signal that a footprint or
    part change didn't propagate.

    Rename detection: components that appear on only one side but share a
    supplier part + package with an unmatched component on the other side
    are treated as a *renamed* pair and reported as a single WARN rather
    than two ERRORs. Cuts a lot of noise on real boards where designators
    drift between revisions.
    """
    if sch.kind is not DocKind.SCHEMATIC or pcb.kind is not DocKind.PCB:
        return
    sch_by_des = {c.designator: c for c in sch.components if c.designator}
    pcb_by_des = {c.designator: c for c in pcb.components if c.designator}

    only_sch = set(sch_by_des) - set(pcb_by_des)
    only_pcb = set(pcb_by_des) - set(sch_by_des)

    # Fuzzy-match renamed pairs so we don't double-report them.
    renamed = _match_by_identity(only_sch, only_pcb, sch_by_des, pcb_by_des)
    renamed_sch = {a for a, _ in renamed}
    renamed_pcb = {b for _, b in renamed}

    for sch_des, pcb_des in sorted(renamed):
        yield Finding(
            severity=Severity.WARN,
            rule="find_schematic_pcb_mismatch",
            message=(
                f"designator differs between schematic ({sch_des!r}) and PCB "
                f"({pcb_des!r}) — same part & package suggests one side got renamed"
            ),
            component=f"{sch_des} ↔ {pcb_des}",
            where="schematic↔pcb",
        )

    for des in sorted(only_sch - renamed_sch):
        yield Finding(
            severity=Severity.ERROR,
            rule="find_schematic_pcb_mismatch",
            message="designator present in schematic but missing from PCB",
            component=des,
            where="schematic↔pcb",
        )
    for des in sorted(only_pcb - renamed_pcb):
        yield Finding(
            severity=Severity.ERROR,
            rule="find_schematic_pcb_mismatch",
            message="designator present in PCB but missing from schematic",
            component=des,
            where="schematic↔pcb",
        )

    # Same designator on both sides — check part-number & package agreement.
    for des in sorted(set(sch_by_des) & set(pcb_by_des)):
        s = sch_by_des[des]
        p = pcb_by_des[des]
        if s.supplier_part and p.supplier_part and s.supplier_part != p.supplier_part:
            yield Finding(
                severity=Severity.WARN,
                rule="find_schematic_pcb_mismatch",
                message=(
                    f"supplier part number differs between schematic ({s.supplier_part!r}) "
                    f"and PCB ({p.supplier_part!r})"
                ),
                component=des,
                where="schematic↔pcb",
            )
        if s.package and p.package and s.package != p.package:
            yield Finding(
                severity=Severity.WARN,
                rule="find_schematic_pcb_mismatch",
                message=(
                    f"package differs between schematic ({s.package!r}) "
                    f"and PCB ({p.package!r})"
                ),
                component=des,
                where="schematic↔pcb",
            )


def rule_find_pin_net_name_mismatches(sch: Document, pcb: Document) -> Iterator[Finding]:
    """A schematic pin whose *label* says VCC/GND/SDA/SCL but whose PCB *net*
    doesn't match is very likely a wiring mistake. Common cause: pin swapped
    on the schematic without re-annotating.

    Only warns on cases where the label is unambiguous (power/ground/I2C).
    Signal pins with vendor-specific names (\"D2\", \"CLKO\") are ignored.
    """
    if sch.kind is not DocKind.SCHEMATIC or pcb.kind is not DocKind.PCB:
        return

    # Pairs by designator OR by fuzzy-matched rename.
    sch_by_des = {c.designator: c for c in sch.components if c.designator}
    pcb_by_des = {c.designator: c for c in pcb.components if c.designator}
    pairs: list[tuple[Component, Component]] = []
    for des in set(sch_by_des) & set(pcb_by_des):
        pairs.append((sch_by_des[des], pcb_by_des[des]))
    only_sch = set(sch_by_des) - set(pcb_by_des)
    only_pcb = set(pcb_by_des) - set(sch_by_des)
    for sch_des, pcb_des in _match_by_identity(only_sch, only_pcb, sch_by_des, pcb_by_des):
        pairs.append((sch_by_des[sch_des], pcb_by_des[pcb_des]))

    for sch_c, pcb_c in pairs:
        # Build pin-number → PCB pad-net map for this component.
        pcb_net_by_num = {p.number: p.net for p in pcb_c.pins}
        for sp in sch_c.pins:
            label = sp.label.strip().upper()
            if not label:
                continue
            pcb_net = pcb_net_by_num.get(sp.number)
            if not pcb_net:
                continue
            expected_ok = _pin_label_net_agrees(label, pcb_net)
            if expected_ok is False:
                yield Finding(
                    severity=Severity.WARN,
                    rule="find_pin_net_name_mismatches",
                    message=(
                        f"pin {sp.number} labelled {label!r} in the schematic sits on "
                        f"PCB net {pcb_net!r} — label and net disagree"
                    ),
                    component=sch_c.designator,
                    where="schematic↔pcb",
                )


import re as _re
_GPIO_NET_RE = _re.compile(r"\bgpio\d+\b|\bpa\d+\b|\bpb\d+\b|\bpc\d+\b", _re.IGNORECASE)


def _looks_like_named_signal(net: str) -> bool:
    """Does the PCB net name look like a *named signal* (not a power rail)?

    Used to distinguish "this is clearly wrong" from "the net is named after
    the source component and I can't tell." Boards frequently have net names
    like `AP2280-3_1` for the AP2280 output — that net carries power, it's
    just named after the source. We don't flag those. We DO flag when a
    label like `VCC` lands on something obviously not power.
    """
    lc = net.lower()
    if _GPIO_NET_RE.search(lc):
        return True
    signal_tokens = (
        "sda", "scl", "mosi", "miso", "sclk", "sck", "cs_", "_cs",
        "reset", "rst", "boot", "irq", "int_",
        "usb_d", "d+", "d-",
        "lora_", "wifi_", "ant_",
        "tx_", "rx_", "_tx", "_rx", "uart_",
    )
    return any(tok in lc for tok in signal_tokens)


def _pin_label_net_agrees(label: str, net: str) -> Optional[bool]:
    """Do a schematic pin label and its PCB net agree?

    Returns True if they clearly match, False *only* when the mismatch is
    unambiguous (e.g. label VCC on a net named GND, or label GND on a
    positive-voltage net). Returns None when the tool can't tell — many
    real boards name power nets after the source component (`AP2280-3_1`
    is the 3V3 rail), and we don't want to raise a warning on every one.
    """
    lc_net = net.lower()
    # Power labels
    if label in ("VCC", "VDD", "VDDA", "VDDIO", "AVDD", "DVDD", "VBUS", "VBAT", "VIN"):
        if _is_gnd_net(net):
            return False
        if _looks_like_named_signal(net):
            return False
        # Otherwise the net could be power (power_net token match OR opaquely
        # named after the source component). Not a warning.
        return None
    # Ground labels
    if label in ("GND", "AGND", "DGND", "VSS", "GNDA", "GNDD"):
        if _is_gnd_net(net):
            return True
        if _is_power_net(net) or _looks_like_named_signal(net):
            return False
        return None
    # I2C — signal names are usually preserved
    if label == "SDA":
        if "sda" in lc_net:
            return True
        if _is_gnd_net(net) or _is_power_net(net):
            return False
        return None
    if label == "SCL":
        if "scl" in lc_net:
            return True
        if _is_gnd_net(net) or _is_power_net(net):
            return False
        return None
    # SPI
    if label in ("MOSI", "MISO", "SCK", "SCLK"):
        base = label.lower()
        if base in lc_net:
            return True
        if _is_gnd_net(net) or _is_power_net(net):
            return False
        return None
    return None  # unknown label, no opinion


CROSS_CHECKS: list[CrossCheckFn] = [
    rule_find_schematic_pcb_mismatch,
    rule_find_pin_net_name_mismatches,
]


def run_cross(sch: Document, pcb: Document) -> list[Finding]:
    findings: list[Finding] = []
    for check in CROSS_CHECKS:
        findings.extend(check(sch, pcb))
    return findings


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
