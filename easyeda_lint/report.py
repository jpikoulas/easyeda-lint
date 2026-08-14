"""Format parsed Documents and Findings for the terminal.

Two things are printed for each file:

  1. A component summary table — every LIB placement with designator, value,
     package, and part number. Skimming this is the "confidence pass": did I
     forget anything, are the values what I meant.

  2. Findings grouped by severity. Errors first, then warnings, then info.
"""
from __future__ import annotations

from .checks import Finding, Severity
from .models import Document

# --------- ANSI colour ----------

def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m"


_SEV_COLOUR = {
    Severity.ERROR: lambda s: _c("31;1", s),  # bold red
    Severity.WARN: lambda s: _c("33", s),      # yellow
    Severity.INFO: lambda s: _c("36", s),      # cyan
}


# --------- component summary table ----------

def render_components(doc: Document) -> str:
    if not doc.components:
        return _c("2", "  (no components parsed)")

    # Column widths tuned to fit typical terminals — long strings still truncate
    # rather than blowing out the layout.
    cols = [
        ("Designator", 24, lambda c: c.designator or "-"),
        ("Value", 16, lambda c: c.value or "-"),
        ("Package", 22, lambda c: c.package or "-"),
        ("Device", 22, lambda c: c.device or "-"),
        ("Supplier Part", 14, lambda c: c.supplier_part or "-"),
        ("Pins", 5, lambda c: str(len(c.pins))),
    ]

    header = "  ".join(f"{name:<{w}}" for name, w, _ in cols)
    sep = "  ".join("-" * w for _, w, _ in cols)

    lines = [_c("1", header), sep]
    for c in sorted(doc.components, key=lambda x: (x.designator or "zzz").lower()):
        row = "  ".join(_truncate(fmt(c), w).ljust(w) for _, w, fmt in cols)
        lines.append(row)

    return "\n".join(lines)


def _truncate(s: str, width: int) -> str:
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


# --------- findings ----------

def render_findings(findings: list[Finding]) -> str:
    if not findings:
        return _c("32", "  no findings")

    # Group by severity, most severe first.
    order = [Severity.ERROR, Severity.WARN, Severity.INFO]
    sections: list[str] = []
    for sev in order:
        subset = [f for f in findings if f.severity is sev]
        if not subset:
            continue
        label = _SEV_COLOUR[sev](f"{sev.value.upper()} ({len(subset)})")
        sections.append(label)
        for f in subset:
            head = f"  [{f.rule}]"
            body = f.message
            if f.component:
                body = f"{f.component}: {body}"
            sections.append(f"{head} {body}")
        sections.append("")

    return "\n".join(sections).rstrip()


# --------- top-level per-document ----------

def render_document(doc: Document, findings: list[Finding]) -> str:
    header = _c("1;4", f"{doc.kind.value.upper()}: {doc.title or '(untitled)'}"
                       f"  —  {doc.source_path}")
    if doc.editor_version:
        header += _c("2", f"  [EasyEDA {doc.editor_version}]")

    parts = [
        header,
        "",
        _c("1", "Components:"),
        render_components(doc),
        "",
        _c("1", "Nets:") + f"  {len(doc.nets)} named net(s), "
                          f"{sum(len(v) for v in doc.nets.values())} pad connections",
        "",
        _c("1", "Findings:"),
        render_findings(findings),
        "",
    ]
    return "\n".join(parts)
