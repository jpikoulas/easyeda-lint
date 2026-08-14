"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .checks import Severity, run_all, run_cross
from .models import DocKind, Document
from .parser import parse_file
from .report import render_document, render_findings, _c  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="easyeda-lint",
        description="Sanity-check EasyEDA schematic and PCB JSON exports.",
    )
    ap.add_argument("files", nargs="+", type=Path,
                    help="One or more EasyEDA JSON files (schematic or PCB).")
    ap.add_argument("--fail-on", choices=["error", "warn", "never"], default="error",
                    help="Exit non-zero if any finding at or above this severity is produced.")
    ap.add_argument("--version", action="version", version=f"easyeda-lint {__version__}")
    args = ap.parse_args(argv)

    fail_threshold = {
        "error": [Severity.ERROR],
        "warn": [Severity.ERROR, Severity.WARN],
        "never": [],
    }[args.fail_on]

    exit_bad = 0
    all_docs: list[Document] = []

    for path in args.files:
        if not path.exists():
            print(f"error: {path} not found", file=sys.stderr)
            exit_bad = 2
            continue
        try:
            docs = parse_file(path)
        except Exception as e:
            print(f"error: could not parse {path}: {e}", file=sys.stderr)
            exit_bad = 2
            continue
        if not docs:
            print(f"warning: {path} produced no documents (unrecognised format?)",
                  file=sys.stderr)
            continue
        for doc in docs:
            findings = run_all(doc)
            print(render_document(doc, findings))
            for sev in fail_threshold:
                if any(f.severity is sev for f in findings):
                    exit_bad = max(exit_bad, 1)
                    break
            all_docs.append(doc)

    # If the user passed both a schematic and a PCB, cross-check them so
    # designator/part-number mismatches are surfaced. We pair the *first*
    # of each kind — if you're auditing a project with multiple sheets or
    # boards, run those cross-checks in separate invocations.
    sch = next((d for d in all_docs if d.kind is DocKind.SCHEMATIC), None)
    pcb = next((d for d in all_docs if d.kind is DocKind.PCB), None)
    if sch is not None and pcb is not None:
        cross_findings = run_cross(sch, pcb)
        print(_c("1;4", "SCHEMATIC ↔ PCB cross-check"))
        print()
        print(render_findings(cross_findings))
        print()
        for sev in fail_threshold:
            if any(f.severity is sev for f in cross_findings):
                exit_bad = max(exit_bad, 1)
                break

    return exit_bad


if __name__ == "__main__":
    raise SystemExit(main())
