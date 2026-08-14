"""End-to-end tests for the --fix pipeline.

These load real JSON fixtures rather than constructing Documents by hand,
because the fix path *has* to round-trip through the raw file — the whole
point of it is that we don't lose any field we don't parse.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from easyeda_lint.checks import run_all, run_cross
from easyeda_lint.fix import (
    KNOWN_CATEGORIES,
    apply_plan,
    plan_fixes,
    verify_round_trip,
)
from easyeda_lint.models import DocKind
from easyeda_lint.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def _load_pair():
    """Parse the rename-fixture schematic + PCB and return them."""
    sch = parse_file(FIXTURES / "rename_schematic.json")[0]
    pcb = parse_file(FIXTURES / "rename_pcb.json")[0]
    assert sch.kind is DocKind.SCHEMATIC
    assert pcb.kind is DocKind.PCB
    return sch, pcb


# ---------- planning ----------

def test_orphan_via_is_planned_for_delete():
    _, pcb = _load_pair()
    plan = plan_fixes(None, pcb, {"orphan-vias"})
    deletes = [f for f in plan.fixes if f.kind == "delete"]
    assert len(deletes) == 1
    assert deletes[0].category == "orphan-vias"


def test_named_via_with_track_is_not_planned():
    """The VIA on R2_2 has a TRACK covering it — must NOT be flagged."""
    _, pcb = _load_pair()
    plan = plan_fixes(None, pcb, {"orphan-vias"})
    for f in plan.fixes:
        # Sanity: the R2_2 via is at index 3; the orphan is index 4.
        assert f.source_index != 3, "R2_2 via was incorrectly targeted for delete"


def test_sync_rename_pair_planned():
    sch, pcb = _load_pair()
    plan = plan_fixes(sch, pcb, {"sync-rename-pairs"})
    categories = [f.category for f in plan.fixes]
    assert "sync-rename-pairs" in categories
    modifies = [f for f in plan.fixes if f.kind == "modify"]
    # Expect at least the LIB rewrite; TRACK/VIA follow-ups add more.
    assert len(modifies) >= 1
    # And each modifier should be callable.
    for f in modifies:
        assert callable(f.modifier)


def test_unknown_category_raises():
    _, pcb = _load_pair()
    with pytest.raises(ValueError, match="unknown fix categories"):
        plan_fixes(None, pcb, {"delete-everything"})


# ---------- end-to-end apply ----------

def test_apply_plan_writes_output_with_renamed_designator(tmp_path):
    sch, pcb = _load_pair()
    plan = plan_fixes(sch, pcb, KNOWN_CATEGORIES)

    output = tmp_path / "rename_pcb.fixed.json"
    apply_plan(pcb.source_path, plan, output)

    # Re-parse and confirm the LIB carries the schematic name now.
    fixed = parse_file(output)[0]
    designators = {c.designator for c in fixed.components}
    assert designators == {"R1"}, f"expected R1 after rename; got {designators}"

    # The auto-generated pad nets R2_1/R2_2 should have followed too.
    for c in fixed.components:
        for p in c.pins:
            if p.net:
                assert not p.net.startswith("R2_"), \
                    f"pad net {p.net!r} still references old designator"

    # Track carrying the same auto-net was rewritten.
    assert any(t.net == "R1_2" for t in fixed.tracks)
    assert not any(t.net == "R2_2" for t in fixed.tracks)

    # The named via on that net follows too (VIA at 200,100).
    live_vias = [v for v in fixed.vias if v.net == "R1_2"]
    assert len(live_vias) == 1

    # The orphan via at (250,150) was deleted.
    assert not any(abs(v.x - 250) < 0.01 and abs(v.y - 150) < 0.01
                   for v in fixed.vias)


def test_apply_preserves_unknown_fields(tmp_path):
    """The fixer must NOT round-trip through our parsed model — fields we
    don't know about (like the editorVersion at the top) must survive."""
    sch, pcb = _load_pair()
    plan = plan_fixes(sch, pcb, KNOWN_CATEGORIES)
    output = tmp_path / "out.json"
    apply_plan(pcb.source_path, plan, output)

    raw = json.loads(output.read_text())
    assert raw.get("editorVersion") == "6.5.99"
    assert raw.get("head", {}).get("docType") == "3"


def test_round_trip_verification_passes_on_good_fix(tmp_path):
    sch, pcb = _load_pair()
    baseline = list(run_all(pcb)) + list(run_cross(sch, pcb))
    plan = plan_fixes(sch, pcb, KNOWN_CATEGORIES)
    output = tmp_path / "out.json"
    apply_plan(pcb.source_path, plan, output)

    ok, warnings = verify_round_trip(
        output, baseline,
        schematic_path=sch.source_path,
        renames=plan.renames,
    )
    assert ok, f"verify_round_trip failed unexpectedly: {warnings}"


def test_round_trip_verification_flags_regression(tmp_path):
    """Simulate a broken fix by manually damaging the output file — the
    verifier should return ok=False with a warning naming the new finding.
    """
    sch, pcb = _load_pair()
    baseline = list(run_all(pcb)) + list(run_cross(sch, pcb))

    plan = plan_fixes(sch, pcb, KNOWN_CATEGORIES)
    output = tmp_path / "out.json"
    apply_plan(pcb.source_path, plan, output)

    # Damage the file: give the LIB a duplicate designator. That's a
    # brand-new ERROR finding that wasn't there before.
    raw = json.loads(output.read_text())
    raw["shape"].append(raw["shape"][0])  # dupe the LIB record
    output.write_text(json.dumps(raw))

    ok, warnings = verify_round_trip(
        output, baseline,
        schematic_path=sch.source_path,
        renames=plan.renames,
    )
    assert not ok
    assert any("duplicate-designator" in w for w in warnings)


# ---------- rename propagation (v0.2.1 bug fix) ----------

def test_rename_propagates_to_shared_net_on_other_components(tmp_path):
    """Regression test for the M5Stamp V2 discovery: when component R2
    connects to C5 through shared net `R2_2`, renaming R2→R1 must also
    rewrite C5's pad from `R2_2` to `R1_2`. Otherwise the net fragments
    and round-trip verification (correctly) rejects the fix."""
    sch = parse_file(FIXTURES / "rename_schematic.json")[0]
    pcb = parse_file(FIXTURES / "rename_shared_net_pcb.json")[0]

    baseline = list(run_all(pcb)) + list(run_cross(sch, pcb))
    plan = plan_fixes(sch, pcb, {"sync-rename-pairs"})

    output = tmp_path / "out.json"
    apply_plan(pcb.source_path, plan, output)

    fixed = parse_file(output)[0]

    # The renamed R2 → R1 landed.
    assert {c.designator for c in fixed.components} == {"R1", "C5"}

    # C5's pad that was on R2_2 must now be on R1_2.
    c5 = next(c for c in fixed.components if c.designator == "C5")
    c5_nets = {p.net for p in c5.pins}
    assert "R1_2" in c5_nets, f"C5 pad still on stale net; got {c5_nets}"
    assert "R2_2" not in c5_nets

    # And the shared net has both pads (proper 2-pad connection, not
    # fragmented into two dangling single-pad nets).
    r1_2_pads = fixed.nets.get("R1_2", [])
    assert len(r1_2_pads) == 2, (
        f"shared net fragmented — got {len(r1_2_pads)} pads, expected 2"
    )

    # Round-trip verifier stays green.
    ok, warnings = verify_round_trip(
        output, baseline,
        schematic_path=sch.source_path,
        renames=plan.renames,
    )
    assert ok, f"verify failed: {warnings}"


# ---------- --skip-rename escape hatch ----------

def test_skip_renames_excludes_named_pairs():
    sch, pcb = _load_pair()
    # Skip R2 — the only rename pair in this fixture. Result: empty plan.
    plan = plan_fixes(
        sch, pcb, {"sync-rename-pairs"},
        skip_renames={"R2"},
    )
    assert not any(f.category == "sync-rename-pairs" for f in plan.fixes)
    assert plan.renames == {}


def test_skip_renames_does_not_affect_other_categories():
    sch, pcb = _load_pair()
    # Skip renames but keep orphan-vias.
    plan = plan_fixes(
        sch, pcb, KNOWN_CATEGORIES,
        skip_renames={"R2"},
    )
    categories = {f.category for f in plan.fixes}
    assert "orphan-vias" in categories
    assert "sync-rename-pairs" not in categories
