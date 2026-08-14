"""Data model for parsed EasyEDA files.

Intentionally minimal — just what we need for the reports and checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DocKind(str, Enum):
    SCHEMATIC = "schematic"
    PCB = "pcb"
    UNKNOWN = "unknown"


@dataclass
class Pin:
    number: str          # "1", "2", ... (schematic) or pad name (PCB)
    net: Optional[str]   # the electrical net this pin is connected to
    x: float
    y: float


@dataclass
class Component:
    """A single instance placed on schematic or PCB."""
    designator: str = ""            # e.g. "1UF-1", "R3", "AP2280-DRV8833"
    value: str = ""                 # e.g. "100nF", "10K"
    package: str = ""               # e.g. "C0603", "SOT-23-5_..."
    device: str = ""                # e.g. "CAP", "Res", "AP2280-2WG-7"
    manufacturer: str = ""
    manufacturer_part: str = ""
    supplier: str = ""              # usually "LCSC"
    supplier_part: str = ""         # e.g. "C128639"
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    pins: list[Pin] = field(default_factory=list)
    lib_id: str = ""                # the gge... internal id, for diffing
    source_line: int = -1           # index in the shape array, for error messages

    @property
    def has_value(self) -> bool:
        return bool(self.value.strip())

    @property
    def has_part(self) -> bool:
        return bool(self.manufacturer_part or self.supplier_part)


@dataclass
class Document:
    """A parsed EasyEDA file — schematic or PCB."""
    kind: DocKind
    title: str = ""
    editor_version: str = ""
    components: list[Component] = field(default_factory=list)
    # Net → list of (component_designator, pin_number) tuples
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    source_path: str = ""

    def component_by_designator(self, designator: str) -> Optional[Component]:
        for c in self.components:
            if c.designator == designator:
                return c
        return None
