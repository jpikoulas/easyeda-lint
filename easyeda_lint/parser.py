"""Parse EasyEDA schematic and PCB JSON exports into our Document model.

EasyEDA files come in two shapes we care about:

  1. A *project* file that embeds one or more schematics under `schematics[i].dataStr`
     and often has PCBs elsewhere. `docType: "5"` at the top level.
  2. A *sheet* file: schematic (`docType: "1"`) or PCB (`docType: "3"`) at the top
     level with a `shape` array of tilde-delimited strings.

The `shape` array is where all the geometry lives. Each entry is a string like:

    "LIB~x~y~backtick`separated`properties~rotation~...~#@$SUBSHAPE~...~#@$SUBSHAPE~..."

`LIB` entries are placed component instances and carry sub-shapes glued on with
`#@$`. Everything else (`W` wire, `TRACK`, `PAD`, `VIA`, ...) sits alongside.

Only the fields we actually use in checks and reports are extracted. When we hit
something unexpected we skip it rather than crash — the JSON format has a lot of
version-dependent shape and we want to tolerate future additions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .models import (
    Component,
    DocKind,
    Document,
    Junction,
    NetLabel,
    Pin,
    Track,
    Via,
    Wire,
)


# ---------- top-level entry ----------

def parse_file(path: str | Path) -> list[Document]:
    """Load one EasyEDA JSON file, return the Documents it contains.

    A project file yields one Document per embedded schematic. A sheet file
    yields a single Document.
    """
    p = Path(path)
    with p.open() as fh:
        raw = json.load(fh)

    docs = _parse_root(raw)
    for d in docs:
        d.source_path = str(p)
    return docs


def _parse_root(raw: dict[str, Any]) -> list[Document]:
    # Project wrapper: has `schematics` list of embedded sheets.
    if "schematics" in raw:
        out: list[Document] = []
        for sheet in raw["schematics"]:
            doc = _parse_sheet(sheet)
            if doc is not None:
                out.append(doc)
        return out

    # Sheet file: schematic or PCB at top level.
    doc = _parse_sheet(raw)
    return [doc] if doc is not None else []


def _parse_sheet(sheet: dict[str, Any]) -> Optional[Document]:
    """Parse a schematic sheet or PCB. `dataStr` wrapper is unwrapped if present."""
    if "dataStr" in sheet:
        data = sheet["dataStr"]
        title = sheet.get("title", "")
    else:
        data = sheet
        title = sheet.get("title", "")

    doc_type = str(sheet.get("docType", data.get("head", {}).get("docType", "")))
    kind = _doc_kind(doc_type)

    shape = data.get("shape") or []
    if not isinstance(shape, list):
        return None

    components: list[Component] = []
    nets: dict[str, list[tuple[str, str]]] = {}
    wires: list[Wire] = []
    junctions: list[Junction] = []
    net_labels: list[NetLabel] = []
    tracks: list[Track] = []
    vias: list[Via] = []

    for idx, entry in enumerate(shape):
        if not isinstance(entry, str):
            continue
        head = entry.split("~", 1)[0]
        if head == "LIB":
            comp = _parse_lib(entry, source_line=idx, kind=kind)
            if comp is not None:
                components.append(comp)
                for pin in comp.pins:
                    if pin.net:
                        nets.setdefault(pin.net, []).append((comp.designator, pin.number))
        elif head == "W":
            w = _parse_wire(entry)
            if w is not None:
                wires.append(w)
        elif head == "J":
            j = _parse_junction(entry)
            if j is not None:
                junctions.append(j)
        elif head == "F":
            f = _parse_flag(entry)
            if f is not None:
                net_labels.append(f)
        elif head == "TRACK":
            t = _parse_track(entry)
            if t is not None:
                tracks.append(t)
                # Only copper-layer tracks contribute to electrical nets.
                # Layers 1 (Top), 2 (Bottom), 11 (Multi-Layer) and 21..52
                # (inner) carry real signals; anything else (silk, paste,
                # mask, assembly, keepout) is a drawing, not a connection.
                if t.net and _is_copper_layer(t.layer):
                    nets.setdefault(t.net, [])
        elif head == "VIA":
            v = _parse_via(entry)
            if v is not None:
                vias.append(v)
                if v.net:
                    nets.setdefault(v.net, [])

    editor_version = data.get("head", {}).get("editorVersion", "") \
        if isinstance(data.get("head"), dict) else ""

    return Document(
        kind=kind,
        title=title,
        editor_version=editor_version,
        components=components,
        nets=nets,
        wires=wires,
        junctions=junctions,
        net_labels=net_labels,
        tracks=tracks,
        vias=vias,
    )


def _doc_kind(doc_type: str) -> DocKind:
    # EasyEDA docType: 1 = schematic sheet, 3 = PCB, 5 = project wrapper.
    if doc_type == "1":
        return DocKind.SCHEMATIC
    if doc_type == "3":
        return DocKind.PCB
    return DocKind.UNKNOWN


# ---------- LIB parsing ----------

def _parse_lib(entry: str, *, source_line: int, kind: DocKind) -> Optional[Component]:
    """Turn a LIB shape string into a Component.

    Sub-shapes are glued to the LIB header with `#@$` — split those off first,
    then parse the header (backtick-encoded properties) and each sub-shape as its
    own tilde-delimited record.
    """
    parts = entry.split("#@$")
    header = parts[0]
    subshapes = parts[1:]

    fields = header.split("~")
    if len(fields) < 4 or fields[0] != "LIB":
        return None

    try:
        x = float(fields[1])
        y = float(fields[2])
    except (ValueError, IndexError):
        x = y = 0.0

    props = _parse_backtick_kv(fields[3])

    # Filter out schematic frame / border libraries — these carry no real
    # component data (Manufacturer Part = "?", package = "NONE") and would
    # only pollute the report.
    if _is_frame_lib(props, fields):
        return None

    # Rotation lives at different indexes in schematic vs PCB LIB. Try a couple.
    rotation = 0.0
    for candidate in (fields[4], fields[5] if len(fields) > 5 else ""):
        try:
            rotation = float(candidate)
            break
        except (ValueError, TypeError):
            continue

    # gge id — useful for cross-file diffing, sits somewhere in the tail. Just
    # grab the first token that looks like one and move on.
    lib_id = ""
    for token in reversed(fields):
        if token.startswith("gge"):
            lib_id = token
            break

    comp = Component(
        package=props.get("package", ""),
        device=props.get("Device", props.get("Symbol", "")),
        manufacturer=props.get("Manufacturer", ""),
        manufacturer_part=props.get("Manufacturer Part", ""),
        supplier=props.get("Supplier", ""),
        supplier_part=props.get("Supplier Part", ""),
        x=x,
        y=y,
        rotation=rotation,
        lib_id=lib_id,
        source_line=source_line,
    )

    # Sub-shapes: T~N~... is the value text, T~P~... is the designator text,
    # P~... (schematic) or PAD~... (PCB) is a pin/pad.
    for sub in subshapes:
        sub_fields = sub.split("~")
        if not sub_fields:
            continue
        head = sub_fields[0]

        if head in ("T", "TEXT"):
            _apply_text_field(comp, sub_fields, head=head)
        elif head == "P":
            pin = _parse_schematic_pin(sub)
            if pin is not None:
                comp.pins.append(pin)
        elif head == "PAD":
            pin = _parse_pcb_pad(sub_fields)
            if pin is not None:
                comp.pins.append(pin)

    # If designator/value never showed up in sub-shapes, EasyEDA sometimes stashes
    # them under c_para or Prefix/Name at the project level; those are harder to
    # get to. For now, empty string is acceptable and the checks will flag it.
    return comp


def _apply_text_field(comp: Component, fields: list[str], *, head: str) -> None:
    """Set component value/designator from a text sub-shape.

    Two shapes to handle:
      Schematic `T`:    T~KIND~x~y~rot~color~font~size~?~?~align~kind~TEXT~...
      PCB `TEXT`:       TEXT~KIND~x~y~size~rot~mirror~layer~font~lineHeight~TEXT~path
    """
    if len(fields) < 3:
        return
    kind = fields[1]
    text = _text_value(fields, head=head)
    if text is None:
        return
    if kind == "N":
        # Name text = value display in EasyEDA.
        if not comp.value:
            comp.value = text
    elif kind == "P":
        # Prefix text = reference designator.
        if not comp.designator:
            comp.designator = text


# Tokens that show up as styling/alignment/hint values and are never the text.
_STYLING_TOKENS = frozenset({
    "", "start", "middle", "end", "central", "top", "bottom",
    "comment", "text-after-edge", "text-before-edge", "hanging",
    "pinpart", "label", "flag", "none",
})


def _text_value(fields: list[str], *, head: str) -> Optional[str]:
    """Extract the human-readable text from a text sub-shape.

    Layout depends on which sub-shape header we're in:

      * Schematic `T`:   text is at index 12
      * PCB `TEXT`:      text is at index 10

    We prefer the fixed index (fast and correct for well-formed data) and
    fall back to a scan only if the fixed index looks wrong.
    """
    fixed_idx = 12 if head == "T" else 10
    if fixed_idx < len(fields):
        tok = fields[fixed_idx]
        if tok and tok not in _STYLING_TOKENS:
            return tok

    # Fallback: scan the plausible window and return the first meaningful string.
    for tok in fields[8:15]:
        if not tok or tok in _STYLING_TOKENS:
            continue
        # Reject obvious style hints like hex colours or "6.5pt".
        if tok.startswith("#") or tok.endswith("pt") or tok.startswith("gge"):
            continue
        return tok
    return None


def _parse_schematic_pin(sub: str) -> Optional[Pin]:
    """Schematic pin encoding is a long compound record delimited by `^^`.

    Format: `P~show~<hyperlink>~<number>~<x>~<y>~<rot>~<ggeId>~...^^...^^...`
    We only need pin number, coordinates, and — from an attached net label if
    any — the net name. Nets on the schematic normally flow via wires so we
    don't attempt to derive them here yet.
    """
    fields = sub.split("~")
    if len(fields) < 6:
        return None
    number = fields[3] if len(fields) > 3 else ""
    try:
        x = float(fields[4])
        y = float(fields[5])
    except ValueError:
        return None
    return Pin(number=number, net=None, x=x, y=y)


def _parse_pcb_pad(fields: list[str]) -> Optional[Pin]:
    """PCB pad encoding: `PAD~SHAPE~x~y~w~h~layer~net~pin_number~hole~...`."""
    if len(fields) < 9:
        return None
    try:
        x = float(fields[2])
        y = float(fields[3])
    except ValueError:
        return None
    net = fields[7] or None
    pin_number = fields[8]
    return Pin(number=pin_number, net=net, x=x, y=y)


# ---------- helpers ----------

# ---------- non-LIB shape parsing ----------

def _parse_wire(entry: str) -> Optional[Wire]:
    """Schematic wire: `W~x1 y1 x2 y2 ...~color~width~dashLine~fill~ggeId~vis`.

    The point list is space-separated within field[1]. We tolerate an odd
    number of tokens by dropping the trailing orphan.
    """
    fields = entry.split("~")
    if len(fields) < 2:
        return None
    coords = fields[1].split()
    points: list[tuple[float, float]] = []
    for i in range(0, len(coords) - 1, 2):
        try:
            points.append((float(coords[i]), float(coords[i + 1])))
        except ValueError:
            continue
    if len(points) < 2:
        return None
    gge = next((f for f in fields if f.startswith("gge")), "")
    return Wire(points=points, gge_id=gge)


def _parse_junction(entry: str) -> Optional[Junction]:
    """Schematic junction: `J~x~y~size~color~ggeId~vis`."""
    fields = entry.split("~")
    if len(fields) < 3:
        return None
    try:
        x = float(fields[1])
        y = float(fields[2])
    except ValueError:
        return None
    gge = next((f for f in fields if f.startswith("gge")), "")
    return Junction(x=x, y=y, gge_id=gge)


def _parse_flag(entry: str) -> Optional[NetLabel]:
    """Schematic flag / net label / power object.

    Format: `F~part_netLabel_KIND~x~y~rot~ggeId~props~vis^^x~y^^TEXT~color~...`

    We only need position, the KIND (to tell power/ground apart from a plain
    net label), and the text if we can decode it.
    """
    # Split off the multi-part sub-record for the text/label body.
    main, *rest = entry.split("^^", 2)
    fields = main.split("~")
    if len(fields) < 4:
        return None
    kind_marker = fields[1] if len(fields) > 1 else ""
    try:
        x = float(fields[2])
        y = float(fields[3])
    except ValueError:
        return None

    kind = "net"
    lower = kind_marker.lower()
    if "power" in lower or "gnd" in lower or "ground" in lower:
        kind = "power" if "power" in lower or "vcc" in lower else "ground"

    # The label text usually appears in the third `^^` chunk (index 2 of the
    # rest split). Best-effort — fall back to empty.
    text = ""
    if len(rest) >= 1:
        # rest[0] may be "x~y" for the anchor; rest[1] starts with the label.
        for chunk in rest:
            for tok in chunk.split("~"):
                if tok and not tok.startswith("#") and not tok.replace(".", "").replace("-", "").isdigit() \
                   and tok not in ("start", "middle", "end", "central") \
                   and not tok.startswith("gge") and not tok.startswith("flag_"):
                    text = tok
                    break
            if text:
                break

    gge = next((f for f in fields if f.startswith("gge")), "")
    return NetLabel(x=x, y=y, text=text, kind=kind, gge_id=gge)


def _parse_track(entry: str) -> Optional[Track]:
    """PCB copper track: `TRACK~width~layer~net~x1 y1 x2 y2 ...~ggeId~vis`."""
    fields = entry.split("~")
    if len(fields) < 5:
        return None
    try:
        width = float(fields[1])
    except ValueError:
        width = 0.0
    try:
        layer = int(float(fields[2]))
    except ValueError:
        layer = 0
    net = fields[3] or None
    coords = fields[4].split()
    points: list[tuple[float, float]] = []
    for i in range(0, len(coords) - 1, 2):
        try:
            points.append((float(coords[i]), float(coords[i + 1])))
        except ValueError:
            continue
    if len(points) < 2:
        return None
    gge = next((f for f in fields if f.startswith("gge")), "")
    return Track(layer=layer, net=net, width=width, points=points, gge_id=gge)


def _parse_via(entry: str) -> Optional[Via]:
    """PCB via: `VIA~x~y~outerDiameter~net~innerRadius~ggeId~vis`."""
    fields = entry.split("~")
    if len(fields) < 6:
        return None
    try:
        x = float(fields[1])
        y = float(fields[2])
        outer = float(fields[3])
    except ValueError:
        return None
    net = fields[4] or None
    try:
        inner = float(fields[5])
    except ValueError:
        inner = 0.0
    gge = next((f for f in fields if f.startswith("gge")), "")
    return Via(x=x, y=y, outer_diameter=outer, inner_diameter=inner, net=net, gge_id=gge)


# ---------- LIB helpers (unchanged) ----------

def _is_copper_layer(layer: int) -> bool:
    """True for copper layers per EasyEDA's layer numbering:
    1 = TopLayer, 2 = BottomLayer, 11 = Multi-Layer, 21..52 = Inner1..Inner32.
    Everything else is silk/paste/mask/assembly/keepout — non-electrical.
    """
    return layer in (1, 2, 11) or 21 <= layer <= 52


def _is_frame_lib(props: dict[str, str], fields: list[str]) -> bool:
    """A schematic frame LIB carries no real component data — skip it."""
    if props.get("package", "").upper() == "NONE":
        return True
    if props.get("Manufacturer Part") == "?":
        return True
    if any("frame_lib" in f for f in fields):
        return True
    return False


def _parse_backtick_kv(s: str) -> dict[str, str]:
    """Parse `key1`val1`key2`val2`...` into a dict, tolerating empty values."""
    tokens = s.split("`")
    out: dict[str, str] = {}
    i = 0
    while i + 1 < len(tokens):
        key = tokens[i]
        val = tokens[i + 1]
        if key:  # skip empty keys (leading/trailing backticks)
            out[key] = val
        i += 2
    return out
