"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .checks import Severity, run_all, run_cross
from .fix import KNOWN_CATEGORIES, apply_plan, plan_fixes, verify_round_trip
from .models import DocKind, Document
from .parser import parse_file
from .report import _c, render_document, render_findings  # type: ignore[attr-defined]

EXAMPLES = """\
examples:
  easyeda-lint pcb.json
      Lint a single PCB export.

  easyeda-lint schematic.json pcb.json
      Lint both files AND run the schematic↔PCB cross-check
      (designator drift, part-number/package mismatch, pin-label
      vs net-name disagreements, missing decoupling by pin label).

  easyeda-lint *.json --fail-on warn
      Also exit non-zero on WARN findings — useful in CI.

  easyeda-lint schematic.json pcb.json --fix
      DRY-RUN: show which corrections --fix could apply. Nothing is
      written until --apply is added. Schematic is treated as source
      of truth; the PCB is what gets rewritten.

  easyeda-lint schematic.json pcb.json --fix --apply
      Write the corrections to pcb.fixed.json (input pcb.json is
      never overwritten). After writing, the fixed file is re-linted
      to confirm no new findings were introduced.

  easyeda-lint schematic.json pcb.json --fix --apply \\
      --skip-rename BATT,POGO,RESET,SOLAR
      Escape hatch: skip specific rename pairs (e.g. connectors you
      intentionally shortened on the PCB for silkscreen space).
      Every other rename still applies.
"""


class _ArgParserWithExamples(argparse.ArgumentParser):
    """ArgumentParser that appends usage examples after argument errors so
    invoking the tool with no files shows the user how to actually use it."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n\n{EXAMPLES}\n")
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    ap = _ArgParserWithExamples(
        prog="easyeda-lint",
        description="Sanity-check EasyEDA schematic and PCB JSON exports.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="+", type=Path,
                    help="One or more EasyEDA JSON files (schematic or PCB).")
    ap.add_argument("--fail-on", choices=["error", "warn", "never"], default="error",
                    help="Exit non-zero if any finding at or above this severity is produced.")
    ap.add_argument("--fix", nargs="?", const="__ALL__", default=None, metavar="CATEGORY",
                    help=(
                        "Plan safe corrections. Without --apply this is a DRY-RUN "
                        "that prints what would change. Categories: "
                        + ", ".join(sorted(KNOWN_CATEGORIES))
                        + " (comma-separated). Omit CATEGORY to enable all."
                    ))
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the fixed PCB. Requires --fix. Never overwrites input.")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Write fixed PCB here instead of <input>.fixed.json.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite the output file if it already exists.")
    ap.add_argument("--skip-rename", default="", metavar="PCB_DES[,...]",
                    help=(
                        "Comma-separated PCB designators to EXCLUDE from "
                        "sync-rename-pairs. Use when the drift is intentional "
                        "(e.g. shorter designators on the PCB for silkscreen "
                        "space). Tool still reports the difference but won't "
                        "auto-rewrite."
                    ))
    ap.add_argument("--version", action="version", version=f"easyeda-lint {__version__}")
    args = ap.parse_args(argv)

    if args.apply and args.fix is None:
        ap.error("--apply requires --fix")

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
    cross_findings = []
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

    if args.fix is not None:
        fix_exit = _run_fix_flow(args, pcb, sch, cross_findings + _flatten_findings(all_docs))
        exit_bad = max(exit_bad, fix_exit)

    return exit_bad


def _flatten_findings(docs: list[Document]) -> list:
    """Re-run per-doc checks to build the pre-fix baseline used by
    verify_round_trip. Cheaper than threading findings all the way
    through the pipeline."""
    from .checks import run_all as _run_all
    out = []
    for d in docs:
        out.extend(_run_all(d))
    return out


def _run_fix_flow(args, pcb, sch, baseline_findings) -> int:
    """Plan fixes, print them, and optionally apply. Returns exit code."""
    if pcb is None:
        print("error: --fix requires a PCB file among the inputs", file=sys.stderr)
        return 2

    if args.fix == "__ALL__":
        categories = set(KNOWN_CATEGORIES)
    else:
        raw = {c.strip() for c in args.fix.split(",") if c.strip()}
        unknown = raw - KNOWN_CATEGORIES
        if unknown:
            print(
                f"error: unknown --fix categor{'y' if len(unknown) == 1 else 'ies'}: "
                f"{', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(KNOWN_CATEGORIES))}.",
                file=sys.stderr,
            )
            return 2
        categories = raw

    skip_renames = frozenset(
        s.strip() for s in args.skip_rename.split(",") if s.strip()
    )

    try:
        plan = plan_fixes(sch, pcb, categories, skip_renames=skip_renames)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(_c("1;4", f"--fix plan ({'apply' if args.apply else 'dry-run'})"))
    print()
    if not plan.fixes:
        print("  (nothing to fix)")
        print()
        return 0
    for f in plan.fixes:
        print(f"  [{f.category}] shape[{f.source_index}]: {f.description}")
    print()

    if not args.apply:
        print(f"{len(plan.fixes)} fix(es) planned. Re-run with --apply to write.")
        return 0

    pcb_path = Path(pcb.source_path)
    output = args.output or pcb_path.with_suffix(".fixed.json")
    if output.exists() and not args.force:
        print(
            f"error: {output} already exists — pass --force to overwrite, "
            f"or -o to pick a different path.",
            file=sys.stderr,
        )
        return 2
    if output.resolve() == pcb_path.resolve():
        print(f"error: refusing to overwrite the input file ({pcb_path}).", file=sys.stderr)
        return 2

    try:
        apply_plan(pcb_path, plan, output)
    except (OSError, ValueError, IndexError) as e:
        print(f"error: failed to write {output}: {e}", file=sys.stderr)
        return 2

    sch_path = Path(sch.source_path) if sch is not None else None
    ok, warnings = verify_round_trip(
        output, baseline_findings,
        schematic_path=sch_path,
        renames=plan.renames,
    )
    if not ok:
        print("error: round-trip verification failed:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)
        # Refuse to keep a suspicious file — delete and exit non-zero.
        try:
            output.unlink()
        except OSError:
            pass
        print(f"error: deleted {output} (fix produced regressions)", file=sys.stderr)
        return 2

    print(f"wrote {output} — round-trip verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
