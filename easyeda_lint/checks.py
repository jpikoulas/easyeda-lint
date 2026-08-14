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

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum

from . import values
from .models import Component, DocKind, Document


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
        yield from w.points


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


def _is_gnd_net(net: str | None) -> bool:
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


# ---------- Tier 5: PCB physical hygiene ----------

# Empirically tuned to EasyEDA's 10-mil (0.254 mm) unit. Real designs
# typically place components on a 100-mil or 50-mil grid; anything off-grid
# by more than a hair is nearly always a slip.
_GRID_UNITS = 10.0        # placement grid — 100 mil in EasyEDA units
_GRID_TOLERANCE = 0.01    # how close to a grid intersection counts as "on grid"
_EDGE_MARGIN = 3.0        # pad-to-board-edge minimum, in EasyEDA units (~0.76 mm)
_ACUTE_ANGLE_DEG = 89.0   # angles at or below this are flagged as acid traps


def _distance_point_to_segment(px: float, py: float,
                               ax: float, ay: float,
                               bx: float, by: float) -> float:
    """Perpendicular distance from point (px, py) to segment (a, b)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _point_in_polygon(px: float, py: float, polygon: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    if len(polygon) < 3:
        return True  # no polygon = accept anything (silent for schematics)
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _min_distance_to_polygon_edge(px: float, py: float,
                                  polygon: list[tuple[float, float]]) -> float:
    """Shortest distance from (px, py) to any edge of the polygon."""
    if len(polygon) < 2:
        return float("inf")
    best = float("inf")
    for i in range(len(polygon) - 1):
        ax, ay = polygon[i]
        bx, by = polygon[i + 1]
        d = _distance_point_to_segment(px, py, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def rule_find_off_grid_components(doc: Document) -> Iterator[Finding]:
    """Component placed off the design grid — usually a nudge slip, and it
    makes autorouters and future tweaks harder."""
    if doc.kind is not DocKind.PCB:
        return
    for c in doc.components:
        # Skip components whose position we couldn't determine.
        if c.x == 0 and c.y == 0:
            continue
        rem_x = abs(c.x - round(c.x / _GRID_UNITS) * _GRID_UNITS)
        rem_y = abs(c.y - round(c.y / _GRID_UNITS) * _GRID_UNITS)
        if rem_x > _GRID_TOLERANCE or rem_y > _GRID_TOLERANCE:
            yield Finding(
                severity=Severity.INFO,
                rule="find_off_grid_components",
                message=(
                    f"placed at ({c.x:g}, {c.y:g}) — off the {_GRID_UNITS:g}-unit grid "
                    f"by ({rem_x:g}, {rem_y:g})"
                ),
                component=c.designator or f"<line {c.source_line}>",
                where=doc.kind.value,
            )


def rule_find_components_outside_board_outline(doc: Document) -> Iterator[Finding]:
    """A component whose origin falls outside the board polygon — either the
    outline wasn't updated after moving parts, or the part is dangling
    off-board and won't be manufactured."""
    if doc.kind is not DocKind.PCB or not doc.board_outline:
        return
    for c in doc.components:
        if c.x == 0 and c.y == 0:
            continue
        if not _point_in_polygon(c.x, c.y, doc.board_outline):
            yield Finding(
                severity=Severity.ERROR,
                rule="find_components_outside_board_outline",
                message=f"origin at ({c.x:g}, {c.y:g}) is outside the board outline polygon",
                component=c.designator or f"<line {c.source_line}>",
                where=doc.kind.value,
            )


def rule_find_pads_near_board_edge(doc: Document) -> Iterator[Finding]:
    """Pads that sit within a fabrication-margin of the board edge — the
    routing tool won't route to them cleanly, and copper too near the edge
    can chip off during depaneling."""
    if doc.kind is not DocKind.PCB or not doc.board_outline:
        return
    for c, p in doc.pads():
        if not p.net:
            # Mechanical pads may legitimately hug the edge.
            continue
        if _is_gnd_net(p.net):
            # Ground pins on edge-mounted connectors regularly sit right
            # on the outline — that's the design, not a defect.
            continue
        d = _min_distance_to_polygon_edge(p.x, p.y, doc.board_outline)
        if d < _EDGE_MARGIN:
            yield Finding(
                severity=Severity.WARN,
                rule="find_pads_near_board_edge",
                message=(
                    f"pin {p.number} on net {p.net!r} is {d:.1f} units from the board "
                    f"edge (< {_EDGE_MARGIN:g} unit safe margin) — check fab clearance"
                ),
                component=c.designator,
                where=doc.kind.value,
            )


def rule_find_acute_angles(doc: Document) -> Iterator[Finding]:
    """Acute-angle bends in copper are historically bad — acid traps during
    etching, and stress concentration at the corner. Modern fabs handle
    them but they're still a code smell."""
    if doc.kind is not DocKind.PCB:
        return
    seen: set[tuple[str, int]] = set()  # dedupe multiple bends on one track
    for t in doc.tracks:
        if t.layer not in (1, 2):
            continue
        pts = t.points
        for i in range(1, len(pts) - 1):
            a, b, c = pts[i - 1], pts[i], pts[i + 1]
            # Vector from b→a and b→c
            v1x, v1y = a[0] - b[0], a[1] - b[1]
            v2x, v2y = c[0] - b[0], c[1] - b[1]
            n1 = (v1x * v1x + v1y * v1y) ** 0.5
            n2 = (v2x * v2x + v2y * v2y) ** 0.5
            if n1 == 0 or n2 == 0:
                continue
            cos_angle = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
            # cos(89°) ≈ 0.0175 — anything with cos_angle >= that has the
            # two vectors bending back on themselves less than 89°, i.e.
            # the corner interior angle is acute.
            import math
            angle_deg = math.degrees(math.acos(cos_angle))
            if angle_deg < _ACUTE_ANGLE_DEG:
                key = (t.gge_id or "unknown", i)
                if key in seen:
                    continue
                seen.add(key)
                yield Finding(
                    severity=Severity.WARN,
                    rule="find_acute_angles",
                    message=(
                        f"track on net {t.net or '?'} has a {angle_deg:.0f}° bend "
                        f"at ({b[0]:g}, {b[1]:g}) — acid-trap / stress-concentration risk"
                    ),
                    where=doc.kind.value,
                )


def rule_find_inconsistent_track_widths(doc: Document) -> Iterator[Finding]:
    """The same net using very different track widths on different segments —
    often a copy-paste artefact or a partial-rewire that missed some spans.
    High-current nets deliberately use different widths from signal ones,
    so we only flag when the ratio is >2× and there are multiple widths."""
    if doc.kind is not DocKind.PCB:
        return
    widths_by_net: dict[str, list[float]] = {}
    for t in doc.tracks:
        if t.layer not in (1, 2) or not t.net or t.net.startswith("S$"):
            continue
        widths_by_net.setdefault(t.net, []).append(t.width)
    for net, widths in widths_by_net.items():
        unique = sorted(set(widths))
        if len(unique) < 2:
            continue
        if unique[-1] / unique[0] > 2.0:
            yield Finding(
                severity=Severity.WARN,
                rule="find_inconsistent_track_widths",
                message=(
                    f"net {net!r} routed at {len(unique)} different widths "
                    f"(min {unique[0]:g}, max {unique[-1]:g}) — check for stale segments"
                ),
                where=doc.kind.value,
            )


def rule_find_mixed_designator_rotation(doc: Document) -> Iterator[Finding]:
    """Designator text at unusual rotations (not 0° or 90° / 180° / 270°) —
    usually a stale nudge. Silk becomes harder to read on-board."""
    if doc.kind is not DocKind.PCB:
        return
    for c in doc.components:
        # Normalise into [0, 360)
        r = c.rotation % 360
        if not any(abs(r - target) < 0.5 for target in (0, 90, 180, 270)):
            yield Finding(
                severity=Severity.INFO,
                rule="find_mixed_designator_rotation",
                message=f"component rotated at {c.rotation:g}° — outside the 0/90/180/270 grid",
                component=c.designator or f"<line {c.source_line}>",
                where=doc.kind.value,
            )


# The single-doc rule registry lives at the very end of this file so it can
# reference every rule (including Tier 6 rules defined further down). See
# the assignment there.


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

    When multiple candidates share the same (part, package) — common when
    a board has several identical connectors — we tie-break by designator
    similarity so `BATT`↔`BATT_CONN` matches even though `POGO_CONN` is
    also a candidate.
    """
    def key(c: Component) -> tuple[str, str] | None:
        if c.supplier_part and c.package:
            return (c.supplier_part, c.package)
        if c.manufacturer_part and c.package:
            return (c.manufacturer_part, c.package)
        return None

    def similarity(a: str, b: str) -> int:
        """Length of the longest common prefix on the alnum-normalised names.
        A cheap-and-cheerful proxy that catches `BATT`↔`BATT_CONN` cleanly
        without pulling in Levenshtein for two-line data."""
        na = "".join(ch for ch in a.upper() if ch.isalnum())
        nb = "".join(ch for ch in b.upper() if ch.isalnum())
        n = 0
        for ca, cb in zip(na, nb, strict=False):
            if ca != cb:
                break
            n += 1
        return n

    sch_lookup: dict[tuple[str, str], list[str]] = {}
    for des in only_sch:
        k = key(sch_by_des[des])
        if k:
            sch_lookup.setdefault(k, []).append(des)

    pairs: list[tuple[str, str]] = []
    matched_sch: set[str] = set()
    # Deterministic order so results don't depend on set iteration.
    for des in sorted(only_pcb):
        k = key(pcb_by_des[des])
        if not k:
            continue
        candidates = [c for c in sch_lookup.get(k, []) if c not in matched_sch]
        if not candidates:
            continue
        # Rank by shared-prefix similarity; a minimum-quality gate keeps us
        # from pairing wildly-different names just because the part matches.
        candidates.sort(key=lambda s: -similarity(s, des))
        best = candidates[0]
        if similarity(best, des) >= 3 or len(candidates) == 1:
            pairs.append((best, des))
            matched_sch.add(best)
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


_GPIO_NET_RE = re.compile(r"\bgpio\d+\b|\bpa\d+\b|\bpb\d+\b|\bpc\d+\b", re.IGNORECASE)


def _looks_like_named_signal(net: str) -> bool:
    """Does the PCB net name look like a *named signal* (not a power rail)?

    Used to distinguish "this is clearly wrong" from "the net is named after
    the source component and I can't tell." Boards frequently have net names
    like `U3_1` for the output of a load switch or LDO — that net carries
    power, it's just named after the source. We don't flag those. We DO flag
    when a label like `VCC` lands on something obviously not power.
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


def _pin_label_net_agrees(label: str, net: str) -> bool | None:
    """Do a schematic pin label and its PCB net agree?

    Returns True if they clearly match, False *only* when the mismatch is
    unambiguous (e.g. label VCC on a net named GND, or label GND on a
    positive-voltage net). Returns None when the tool can't tell — many
    real boards name power nets after the source component (e.g. `U3_1`
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


# ---------- Tier 6: part-database and topology rules ----------

# Schematic pin labels considered "power inputs" for decoupling purposes.
# We only care about inputs that need bypass capacitors here — outputs like
# VOUT are the *source*, not the consumer.
_POWER_INPUT_LABELS = frozenset({
    "VCC", "VDD", "VDDA", "VDDIO", "VDD_", "VDDCORE", "VDDPLL",
    "AVDD", "DVDD", "AVCC", "VBUS", "VBAT", "VIN",
    "V+", "+V", "PWR",
})

# Ratio between minimum and maximum "sensible" decoupling cap value: anything
# more than 10× smaller than the smallest recommended value is likely a slip.
# (100 nF..10 µF is the normal range; a 1 pF cap in that slot means someone
# copied wrong.) Not enforced yet — recorded here for a Tier 5 tightening.


def _components_by_net_and_gnd(doc: Document, is_component_fn) -> dict[str, list[Component]]:
    """For each net that connects to GND via a matching component (usually a
    cap), return the list of such components. Used by decoupling and
    pull-up rules that share the same shape."""
    result: dict[str, list[Component]] = {}
    for c in doc.components:
        if not is_component_fn(c):
            continue
        nets = [p.net for p in c.pins if p.net]
        if len(nets) < 2:
            continue
        has_gnd = any(_is_gnd_net(n) for n in nets)
        if not has_gnd:
            continue
        for n in nets:
            if not _is_gnd_net(n):
                result.setdefault(n, []).append(c)
    return result


def _components_between(doc: Document, is_component_fn,
                        net_a_pred, net_b_pred) -> dict[str, list[Component]]:
    """Components (usually resistors) that connect one pin to a net matching
    `net_a_pred` and another to a net matching `net_b_pred`. Returns a map
    keyed by the net_a-matching net. Used for pull-up detection: find all
    resistors that bridge net X → power rail."""
    result: dict[str, list[Component]] = {}
    for c in doc.components:
        if not is_component_fn(c):
            continue
        nets = [p.net for p in c.pins if p.net]
        if len(nets) < 2:
            continue
        # A pull-up must have EXACTLY one pin on each side. Loosely check:
        # any pin on side A, any pin on side B, no pin on both.
        a_hits = [n for n in nets if net_a_pred(n)]
        b_hits = [n for n in nets if net_b_pred(n)]
        for a in a_hits:
            for b in b_hits:
                if a == b:
                    continue
                result.setdefault(a, []).append(c)
    return result


def _is_resistor(c: Component) -> bool:
    if _PASSIVE_PACKAGE_RE.match(c.package):
        prefix = c.package.strip()[0].upper()
        if prefix == "R":
            return True
    device_lc = c.device.strip().lower()
    return device_lc in ("res", "resistor")


def rule_find_missing_decoupling_by_label(sch: Document, pcb: Document) -> Iterator[Finding]:
    """Cross-check version of `find_missing_decoupling`.

    Uses schematic pin labels to identify which PCB nets are *actually*
    power inputs (regardless of what they're named), then verifies each
    one has a cap to GND anywhere on the board. This works even when
    power rails are named after their source component
    (e.g. `U3_1` for a 3.3 V rail out of a load switch), which the
    pure-PCB heuristic misses.
    """
    if sch.kind is not DocKind.SCHEMATIC or pcb.kind is not DocKind.PCB:
        return

    # Match components by designator (or fuzzy rename).
    sch_by_des = {c.designator: c for c in sch.components if c.designator}
    pcb_by_des = {c.designator: c for c in pcb.components if c.designator}
    pairs: list[tuple[Component, Component]] = []
    for des in set(sch_by_des) & set(pcb_by_des):
        pairs.append((sch_by_des[des], pcb_by_des[des]))
    for sch_des, pcb_des in _match_by_identity(
        set(sch_by_des) - set(pcb_by_des),
        set(pcb_by_des) - set(sch_by_des),
        sch_by_des, pcb_by_des,
    ):
        pairs.append((sch_by_des[sch_des], pcb_by_des[pcb_des]))

    # For each pair, find PCB nets that are on schematic-labelled power pins.
    power_consumers: dict[str, list[tuple[str, str, str]]] = {}  # net -> [(des, pin, label)]
    for sch_c, pcb_c in pairs:
        pcb_pins_by_num = {p.number: p for p in pcb_c.pins}
        for sp in sch_c.pins:
            label_upper = sp.label.strip().upper()
            if label_upper not in _POWER_INPUT_LABELS:
                continue
            pcb_pin = pcb_pins_by_num.get(sp.number)
            if not pcb_pin or not pcb_pin.net or _is_gnd_net(pcb_pin.net):
                continue
            power_consumers.setdefault(pcb_pin.net, []).append(
                (pcb_c.designator, sp.number, label_upper)
            )

    if not power_consumers:
        return

    # Which of those nets are decoupled?
    decoupled_nets = set(_components_by_net_and_gnd(pcb, _is_capacitor).keys())

    for net in sorted(power_consumers):
        if net in decoupled_nets:
            continue
        consumers = power_consumers[net]
        summary = ", ".join(f"{d}.{n} ({lbl})" for d, n, lbl in consumers[:3])
        if len(consumers) > 3:
            summary += f", … (+{len(consumers) - 3} more)"
        yield Finding(
            severity=Severity.WARN,
            rule="find_missing_decoupling",
            message=(
                f"PCB net {net!r} feeds schematic-labelled power inputs "
                f"({summary}) but has no capacitor to GND anywhere on the board"
            ),
            where="schematic↔pcb",
        )


def rule_find_missing_i2c_pullups(doc: Document) -> Iterator[Finding]:
    """I2C is open-drain — SDA and SCL each need a pull-up to VCC/VDD, or
    the bus will never go high. Missing pull-ups is a classic silent-fail
    bug where the master's writes look fine on-scope but nothing
    acknowledges.

    A pull-up is any resistor connecting the I2C net to a net that's
    neither GND nor another I2C net. We deliberately don't require the
    other side to look like a *named* power net — boards frequently name
    rails after their source component (e.g. `U3_1` for a 3.3 V rail
    out of a load switch), and demanding a strict VCC/VDD name would
    false-positive on those.
    """
    if doc.kind is not DocKind.PCB:
        return

    def is_i2c(net_name: str) -> bool:
        lc = net_name.lower()
        return "sda" in lc or "scl" in lc

    def is_pullup_target(net_name: str) -> bool:
        # A pull-up target is any electrical net that's not GND and not the
        # same I2C net. That covers named power rails AND rails named after
        # a source component.
        if not net_name:
            return False
        if _is_gnd_net(net_name):
            return False
        if is_i2c(net_name):
            return False
        return True

    pullups_by_net = _components_between(doc, _is_resistor, is_i2c, is_pullup_target)

    i2c_nets: set[str] = set()
    for c in doc.components:
        for p in c.pins:
            if p.net and is_i2c(p.net):
                i2c_nets.add(p.net)

    for net in sorted(i2c_nets):
        if net not in pullups_by_net:
            yield Finding(
                severity=Severity.WARN,
                rule="find_missing_i2c_pullups",
                message=(
                    f"I2C net {net!r} has no pull-up resistor to any non-GND net — "
                    f"bus will not idle high"
                ),
                where=doc.kind.value,
            )


def rule_find_missing_reset_pullup(doc: Document) -> Iterator[Finding]:
    """Reset lines on active-low MCUs should sit high through a pull-up;
    without one, ESD or floating noise can spuriously reset the chip."""
    if doc.kind is not DocKind.PCB:
        return

    def is_reset(net_name: str) -> bool:
        lc = net_name.lower()
        # Full-word match — avoids catching "PRESET" or embedded RST substrings.
        for tok in ("reset", "nrst", "nrs_"):
            if tok in lc:
                return True
        # Very common "_RST" / "RST_" / bare RST at the end of a net name.
        if lc == "rst" or lc.endswith("_rst") or lc.startswith("rst_"):
            return True
        return False

    def is_pullup_target(net_name: str) -> bool:
        # As in the I2C rule — accept any non-GND, non-reset net.
        if not net_name:
            return False
        if _is_gnd_net(net_name):
            return False
        if is_reset(net_name):
            return False
        return True

    pullups = _components_between(doc, _is_resistor, is_reset, is_pullup_target)

    reset_nets: set[str] = set()
    for c in doc.components:
        for p in c.pins:
            if p.net and is_reset(p.net):
                reset_nets.add(p.net)

    # Only warn once per reset net.
    for net in sorted(reset_nets):
        if net not in pullups:
            # Reset button connectors need the pull-up too, but MCUs often
            # have an internal pull-up on nRST. Still worth surfacing so the
            # designer can confirm.
            yield Finding(
                severity=Severity.INFO,
                rule="find_missing_reset_pullup",
                message=(
                    f"reset net {net!r} has no explicit pull-up to a power rail — "
                    f"confirm the MCU/reset button design provides one internally"
                ),
                where=doc.kind.value,
            )


CROSS_CHECKS: list[CrossCheckFn] = [
    rule_find_schematic_pcb_mismatch,
    rule_find_pin_net_name_mismatches,
    rule_find_missing_decoupling_by_label,
]


# ---------- registries (kept last so every rule name is bound above) ----------

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
    # Tier 5 — note: rule_find_off_grid_components is deliberately not
    # registered by default. EasyEDA doesn't expose the "active placement
    # grid" in the JSON, so we can't meaningfully say what off-grid means.
    # Left in the module so a user can enable it explicitly.
    rule_find_components_outside_board_outline,
    rule_find_pads_near_board_edge,
    rule_find_acute_angles,
    rule_find_inconsistent_track_widths,
    rule_find_mixed_designator_rotation,
    # Tier 6
    rule_find_missing_i2c_pullups,
    rule_find_missing_reset_pullup,
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
