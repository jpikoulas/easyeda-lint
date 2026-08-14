"""Parser sanity tests — feed in tiny hand-crafted JSON documents and
check we extract the right components, pins, wires, tracks, and vias.

The full parser is exercised end-to-end by running the CLI against real
project files; these tests just cover the primary parse paths so a
regression on the shape-string decoder can't slip past CI.
"""
from __future__ import annotations

from pathlib import Path

from easyeda_lint.models import DocKind
from easyeda_lint.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_schematic_extracts_components_and_wires():
    docs = parse_file(FIXTURES / "tiny_schematic.json")
    assert len(docs) == 1
    doc = docs[0]
    assert doc.kind is DocKind.SCHEMATIC
    assert doc.title == "tiny-fixture-schematic"

    designators = {c.designator for c in doc.components}
    assert designators == {"R1", "C1"}

    r1 = next(c for c in doc.components if c.designator == "R1")
    assert r1.value == "4.7K"
    assert r1.package == "R0603"
    assert r1.supplier_part == "C99782"

    assert len(doc.wires) == 1
    assert len(doc.junctions) == 1


def test_parse_pcb_extracts_components_pads_tracks_vias():
    docs = parse_file(FIXTURES / "tiny_pcb.json")
    assert len(docs) == 1
    doc = docs[0]
    assert doc.kind is DocKind.PCB
    assert doc.title == "tiny-fixture-pcb"

    designators = {c.designator for c in doc.components}
    assert designators == {"R1", "C1"}

    r1 = next(c for c in doc.components if c.designator == "R1")
    assert {p.number for p in r1.pins} == {"1", "2"}
    assert {p.net for p in r1.pins} == {"SDA", "VCC"}

    assert "VCC" in doc.nets
    assert len(doc.vias) == 1
    assert doc.vias[0].net == "VCC"

    # Copper tracks land in doc.tracks; the outline (layer 10) should be
    # captured in board_outline, not counted as a signal track.
    copper_tracks = [t for t in doc.tracks if t.layer == 1]
    assert len(copper_tracks) == 1
    assert copper_tracks[0].net == "VCC"
    assert doc.board_outline, "board outline should be captured from layer 10"
