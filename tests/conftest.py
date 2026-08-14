"""Shared pytest helpers.

Most rule tests construct `Document` / `Component` objects directly rather
than parsing a JSON file — the parser has its own dedicated tests, and
keeping rule tests JSON-free means they don't tangle up as the parser
evolves.
"""
from __future__ import annotations

from easyeda_lint.models import Component, DocKind, Document, Pin


def make_component(
    designator: str = "",
    *,
    value: str = "",
    package: str = "",
    device: str = "",
    supplier_part: str = "",
    manufacturer_part: str = "",
    x: float = 0.0,
    y: float = 0.0,
    rotation: float = 0.0,
    pins: list[tuple[str, str | None]] | None = None,
    pin_labels: dict[str, str] | None = None,
) -> Component:
    """Build a Component with the minimum fields any rule cares about.

    `pins` is a list of `(pin_number, net_name)` tuples; a net of `None`
    means the pin is unrouted. `pin_labels` maps pin number → schematic
    label (VCC/GND/etc.) for rules that key on it.
    """
    c = Component(
        designator=designator,
        value=value,
        package=package,
        device=device,
        supplier_part=supplier_part,
        manufacturer_part=manufacturer_part,
        x=x,
        y=y,
        rotation=rotation,
    )
    if pins:
        labels = pin_labels or {}
        for num, net in pins:
            c.pins.append(Pin(
                number=num,
                net=net,
                x=x,
                y=y,
                label=labels.get(num, ""),
            ))
    return c


def make_pcb(components: list[Component] | None = None) -> Document:
    doc = Document(kind=DocKind.PCB, title="test-pcb")
    for c in components or []:
        doc.components.append(c)
        for p in c.pins:
            if p.net:
                doc.nets.setdefault(p.net, []).append((c.designator, p.number))
    return doc


def make_schematic(components: list[Component] | None = None) -> Document:
    doc = Document(kind=DocKind.SCHEMATIC, title="test-schematic")
    for c in components or []:
        doc.components.append(c)
    return doc
