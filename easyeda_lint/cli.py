"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .checks import Severity, run_all
from .parser import parse_file
from .report import render_document


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

    return exit_bad


if __name__ == "__main__":
    raise SystemExit(main())
