"""Plan and apply the small set of corrections v0.2 knows how to make.

Scope, on purpose, is narrow:

    orphan-vias         Delete vias that carry no net and have no track
                        touching them anywhere on the board. These are
                        almost always debris left from re-routing.

    sync-rename-pairs   Schematic is source of truth. When the cross-
                        check identifies a component whose designator
                        drifted between schematic and PCB (same part &
                        package, only-candidate on each side), rewrite
                        the PCB designator to match the schematic. Every
                        pad on that component whose net was auto-named
                        `<old>_<n>` gets updated in the same pass, as do
                        any TRACK/VIA records that reference the same
                        auto-generated net.

Anything more ambitious — value drift, package drift, missing decoupling
caps — stays as a report-only finding. See docs/fix-mode-design.md.

The safety story:

    - `--apply` is required to write anything.
    - Output goes to `<input>.fixed.json`, never overwrites input.
    - After writing, the fixed file is re-parsed and re-linted; if any
      intended fix is still present as a finding, or a *new* finding
      appears that wasn't there before, we treat the fix as broken and
      delete the output file.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import shape_edit
from .checks import (
    _VIA_EPSILON,
    ALL_CHECKS,
    CROSS_CHECKS,
    Finding,
    _match_by_identity,
    _points_touch,
)
from .models import Component, DocKind, Document, Via
from .parser import parse_file

# All fix categories we can plan. Anything outside this set is rejected
# by the CLI so a typo doesn't silently do nothing.
KNOWN_CATEGORIES = frozenset({"orphan-vias", "sync-rename-pairs"})


@dataclass
class Fix:
    """One concrete edit against a specific shape[] index.

    `kind` picks the low-level operation:

        "modify"   apply `modifier(shape[i])` and store the return value.
        "delete"   remove shape[i]. Deletes are applied in reverse index
                   order so earlier indices don't shift under us.
    """
    category: str
    description: str
    source_index: int
    kind: str  # "modify" | "delete"
    modifier: Callable[[str], str] | None = None


@dataclass
class FixPlan:
    """The full set of edits planned against one PCB file."""
    fixes: list[Fix] = field(default_factory=list)
    # Populated by sync-rename-pairs; verify_round_trip uses this to
    # normalise findings that merely followed a rename.
    renames: dict[str, str] = field(default_factory=dict)


# ---------- planning ----------

def plan_fixes(
    sch: Document | None,
    pcb: Document,
    categories: frozenset[str] | set[str],
    *,
    skip_renames: frozenset[str] | set[str] | None = None,
) -> FixPlan:
    """Build the FixPlan for a PCB (with optional schematic for cross-file fixes).

    `skip_renames` is the escape hatch for intentional PCB-vs-schematic
    drift — a set of PCB designators to exclude from sync-rename-pairs.
    Passing `{"BATT", "POGO"}` for example says "yes those are different
    from the schematic on purpose (silkscreen space, etc.), leave them
    alone." The tool still *reports* them as findings — this only stops
    the auto-rewrite.
    """
    plan = FixPlan()
    unknown = set(categories) - KNOWN_CATEGORIES
    if unknown:
        raise ValueError(f"unknown fix categories: {sorted(unknown)}")

    if "orphan-vias" in categories:
        _plan_orphan_via_deletes(pcb, plan)
    if "sync-rename-pairs" in categories and sch is not None:
        _plan_rename_sync(sch, pcb, plan, skip_renames=frozenset(skip_renames or ()))
    return plan


def _plan_orphan_via_deletes(pcb: Document, plan: FixPlan) -> None:
    """A via with net=None (or an EasyEDA auto-name) and no track within
    the pad-connectivity epsilon is debris. Real ground-stitch vias have
    an explicit net string, so we don't delete those even if no track is
    directly under them (the pour supplies the connection)."""
    for via in pcb.vias:
        if via.source_line < 0:
            continue
        if via.net and not via.net.startswith("S$"):
            continue
        if _via_has_any_track_nearby(via, pcb):
            continue
        plan.fixes.append(Fix(
            category="orphan-vias",
            description=(
                f"delete orphan via at ({via.x:.2f}, {via.y:.2f}) "
                f"net={via.net!r}"
            ),
            source_index=via.source_line,
            kind="delete",
        ))


def _via_has_any_track_nearby(via: Via, pcb: Document) -> bool:
    """True if any track vertex is within _VIA_EPSILON of the via center,
    regardless of net. We check without net-matching because an orphan
    via might have no net string of its own even though it's under a
    live track — that would be a rename-worthy finding, not a delete."""
    for t in pcb.tracks:
        for pt in t.points:
            if _points_touch((via.x, via.y), pt, _VIA_EPSILON):
                return True
    return False


def _plan_rename_sync(
    sch: Document,
    pcb: Document,
    plan: FixPlan,
    *,
    skip_renames: frozenset[str] = frozenset(),
) -> None:
    """For each rename pair we're confident about, rewrite the PCB's LIB
    designator (in the T~P/TEXT~P sub-record) to the schematic's name,
    and rewrite any auto-generated net that referenced the old name.

    Reuses _match_by_identity from checks.py so the fix planner and the
    reporter agree on which pairs count as renames.

    Pairs whose PCB designator is in `skip_renames` are dropped — that's
    the user's explicit "this drift is intentional, leave it alone."
    """
    if sch.kind is not DocKind.SCHEMATIC or pcb.kind is not DocKind.PCB:
        return

    sch_by_des = {c.designator: c for c in sch.components if c.designator}
    pcb_by_des = {c.designator: c for c in pcb.components if c.designator}
    only_sch = set(sch_by_des) - set(pcb_by_des)
    only_pcb = set(pcb_by_des) - set(sch_by_des)

    pairs = _match_by_identity(only_sch, only_pcb, sch_by_des, pcb_by_des)

    # Reject any plan where the target designator would collide with an
    # existing PCB component. That would corrupt the file.
    live_pcb_designators = set(pcb_by_des)
    safe_pairs: list[tuple[str, str]] = []
    for sch_des, pcb_des in pairs:
        if pcb_des in skip_renames:
            continue
        if sch_des in live_pcb_designators and sch_des != pcb_des:
            # Two schematic components can't both rename to the same PCB
            # designator either. Skip and let the human resolve.
            continue
        safe_pairs.append((sch_des, pcb_des))

    for sch_des, pcb_des in sorted(safe_pairs):
        pcb_comp = pcb_by_des[pcb_des]
        if pcb_comp.source_line < 0:
            continue

        old, new = pcb_des, sch_des
        plan.renames[new] = old

        # The LIB rewrite: swap the designator text field, and while
        # we've got the string, also rewrite any auto-generated pad
        # nets on the same component.
        def make_lib_rewrite(old_name: str, new_name: str):
            def rewrite(shape_str: str) -> str:
                s = shape_edit.replace_designator_in_lib(shape_str, new_name)
                s = shape_edit.rename_net_auto_suffix(s, old_name, new_name)
                return s
            return rewrite

        plan.fixes.append(Fix(
            category="sync-rename-pairs",
            description=f"rename PCB component {old!r} → {new!r} (schematic wins)",
            source_index=pcb_comp.source_line,
            kind="modify",
            modifier=make_lib_rewrite(old, new),
        ))

        # Now the tricky part: EasyEDA stores the SAME net name string on
        # every pad connected to that net, on every component. So renaming
        # the source component's LIB isn't enough — every OTHER component
        # whose pads reference `<old>_<n>` must have those pad-net fields
        # rewritten too, or we'd fragment the net.
        follow_targets = _collect_rename_follow_targets(
            pcb, old, exclude_shape_index=pcb_comp.source_line
        )
        for idx, kind in follow_targets:
            plan.fixes.append(Fix(
                category="sync-rename-pairs",
                description=(
                    f"update {kind}[{idx}] net "
                    f"{old!r}_* → {new!r}_* (rename-follow)"
                ),
                source_index=idx,
                kind="modify",
                modifier=(lambda old_p=old, new_p=new:
                    lambda s: shape_edit.rename_net_auto_suffix(s, old_p, new_p))(),
            ))


def _collect_rename_follow_targets(
    pcb: Document, prefix: str, *, exclude_shape_index: int
) -> list[tuple[int, str]]:
    """Return every shape[] index that needs a rename-follow rewrite.

    Includes:
        * TRACK records whose net matches `<prefix>_<digits>`
        * VIA records whose net matches `<prefix>_<digits>`
        * LIB records of *other* components (i.e. not the one being
          renamed) that have any pad on a `<prefix>_<digits>` net

    The exclude_shape_index skips the LIB of the component being
    renamed — its own pad rewrite is bundled into the primary LIB
    modifier so it happens in one atomic edit.

    Results are deduped and returned in an arbitrary but stable order
    (sorted by shape index). Since modifies chain through the same
    shape entry, duplicate indices would apply the same rewrite twice
    (no-op on the second pass), but we still dedupe for clarity in the
    dry-run listing.
    """
    def matches(net: str | None) -> bool:
        if not net or not net.startswith(prefix + "_"):
            return False
        return net[len(prefix) + 1:].isdigit()

    seen: set[int] = set()
    out: list[tuple[int, str]] = []

    for track in pcb.tracks:
        if track.source_line < 0 or track.source_line == exclude_shape_index:
            continue
        if matches(track.net) and track.source_line not in seen:
            seen.add(track.source_line)
            out.append((track.source_line, "TRACK"))

    for via in pcb.vias:
        if via.source_line < 0 or via.source_line == exclude_shape_index:
            continue
        if matches(via.net) and via.source_line not in seen:
            seen.add(via.source_line)
            out.append((via.source_line, "VIA"))

    for comp in pcb.components:
        if comp.source_line < 0 or comp.source_line == exclude_shape_index:
            continue
        if comp.source_line in seen:
            continue
        if any(matches(p.net) for p in comp.pins):
            seen.add(comp.source_line)
            out.append((comp.source_line, f"LIB({comp.designator})"))

    out.sort(key=lambda pair: pair[0])
    return out




# ---------- application ----------

def apply_plan(pcb_path: str | Path, plan: FixPlan, output_path: str | Path) -> None:
    """Load the raw PCB JSON, apply the plan, write to output_path.

    We work on the raw JSON dict — no round-trip through our parsed
    model — so any field we don't understand is preserved exactly.
    """
    src = Path(pcb_path)
    dst = Path(output_path)

    with src.open() as fh:
        raw = json.load(fh)

    shape, restore = _extract_shape(raw)
    if shape is None:
        raise ValueError(f"{src}: no shape[] array found — is this a PCB export?")

    # Apply modifies first (they don't shift indices), then deletes in
    # reverse index order so earlier ones don't slip under us.
    modifies = [f for f in plan.fixes if f.kind == "modify"]
    deletes = sorted(
        (f for f in plan.fixes if f.kind == "delete"),
        key=lambda f: -f.source_index,
    )

    for f in modifies:
        if f.modifier is None:
            continue
        if not 0 <= f.source_index < len(shape):
            raise IndexError(
                f"{f.category}: source_index {f.source_index} out of range "
                f"for shape[] of length {len(shape)}"
            )
        entry = shape[f.source_index]
        if not isinstance(entry, str):
            continue
        shape[f.source_index] = f.modifier(entry)

    for f in deletes:
        if not 0 <= f.source_index < len(shape):
            raise IndexError(
                f"{f.category}: source_index {f.source_index} out of range "
                f"for shape[] of length {len(shape)}"
            )
        del shape[f.source_index]

    restore(shape)

    with dst.open("w") as fh:
        json.dump(raw, fh, separators=(",", ":"))


def _extract_shape(raw: dict[str, Any]):
    """Return (shape_list, restore_callback). The callback puts the
    (possibly mutated) shape list back where it came from — which might
    be top-level or nested under `dataStr`."""
    if isinstance(raw.get("shape"), list):
        def restore(new):
            raw["shape"] = new
        return raw["shape"], restore
    if isinstance(raw.get("dataStr"), dict) and isinstance(raw["dataStr"].get("shape"), list):
        def restore(new):
            raw["dataStr"]["shape"] = new
        return raw["dataStr"]["shape"], restore
    return None, lambda _: None


# ---------- verification ----------

def verify_round_trip(
    output_path: str | Path,
    original_findings: list[Finding],
    schematic_path: str | Path | None = None,
    renames: dict[str, str] | None = None,
) -> tuple[bool, list[str]]:
    """Re-parse and re-lint the written file. Return (ok, warnings).

    The guarantee we check: no *new* finding appears that wasn't there
    before the fix. A fix that introduces a fresh problem is a broken
    fix — the CLI reacts by deleting the output.

    `renames` maps `new_name -> old_name`. Findings that reference a
    renamed net (e.g. a dangling-net that used to fire on `R2_1` and
    now fires on `R1_1`) get normalised back to their pre-fix wording
    before comparison, so they don't count as regressions.

    Returns ok=False + a list of one-line reasons when violations
    exist; caller should treat that as "delete the output file and
    exit non-zero."
    """
    new_docs = parse_file(output_path)
    if not new_docs:
        return False, [f"{output_path}: re-parse returned no documents"]
    new_pcb = next((d for d in new_docs if d.kind is DocKind.PCB), None)
    if new_pcb is None:
        return False, [f"{output_path}: re-parse found no PCB document"]

    new_findings: list[Finding] = []
    for rule in ALL_CHECKS:
        new_findings.extend(rule(new_pcb))

    if schematic_path is not None:
        sch_docs = parse_file(schematic_path)
        sch = next((d for d in sch_docs if d.kind is DocKind.SCHEMATIC), None)
        if sch is not None:
            for rule in CROSS_CHECKS:
                new_findings.extend(rule(sch, new_pcb))

    renames = renames or {}

    # Precompile per-rename patterns for two rewrites:
    #   1. <new>_<digits>   → <old>_<digits>   (auto-generated net names)
    #   2. bare <new> token → <old>              (designator references
    #      like `(R1 pin 1)` inside a message, or comp == "R1")
    # Token boundary uses lookaround for non-alnum-non-underscore, which
    # is what "whole EasyEDA designator" actually means — Python's \b
    # counts _ as a word char, so it wouldn't fire on `R1_1`.
    _rewriters: list[Callable[[str], str]] = []
    for new, old in renames.items():
        net_pat = re.compile(re.escape(new) + r"_(\d+)")
        tok_pat = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(new) + r"(?![A-Za-z0-9_])"
        )
        def _apply(s: str, np=net_pat, tp=tok_pat, o=old) -> str:
            s = np.sub(lambda m, o=o: f"{o}_{m.group(1)}", s)
            s = tp.sub(o, s)
            return s
        _rewriters.append(_apply)

    def normalise(f: Finding) -> tuple[str, str, str]:
        msg, comp = f.message, f.component
        for rw in _rewriters:
            msg = rw(msg)
            comp = rw(comp)
        return (f.rule, comp, msg)

    orig_set = {(f.rule, f.component, f.message) for f in original_findings}

    warnings: list[str] = []
    for f in new_findings:
        if normalise(f) in orig_set:
            continue
        warnings.append(
            f"new finding after --fix: [{f.rule}] {f.component}: {f.message}"
        )
    return (len(warnings) == 0), warnings


# ---------- helpers used by the planner ----------

def component_by_designator(doc: Document, designator: str) -> Component | None:
    for c in doc.components:
        if c.designator == designator:
            return c
    return None
