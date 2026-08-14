"""Tests for the low-level shape[] string surgery primitives.

These operate on the exact tilde-delimited encoding EasyEDA uses, so the
inputs here are handcrafted to match what the parser sees in real files.
"""
from __future__ import annotations

import pytest

from easyeda_lint.shape_edit import (
    rename_net_auto_suffix,
    replace_designator_in_lib,
)

# A minimal PCB LIB entry with the two pads + designator sub-record we care
# about. This matches the fixture structure in tests/fixtures/tiny_pcb.json.
PCB_LIB_R1 = (
    "LIB~100~100~package`R0603`Device`Res`Supplier`LCSC`Supplier Part`C99782`"
    "~0~0~ggeR1~1~~yes~yes~~~"
    "#@$TEXT~N~100~95~0.6~0~0~3~~1.9685~4.7K~M "
    "#@$TEXT~P~100~90~0.6~0~0~3~~1.9685~R1~M "
    "#@$PAD~RECT~98~100~1.5~1.5~1~R1_1~1~0~97 99 99 99 99 101 97 101"
    "~0~ggePAD1~0~~Y~0~0~0.2~98,100"
    "#@$PAD~RECT~102~100~1.5~1.5~1~R1_2~2~0~101 99 103 99 103 101 101 101"
    "~0~ggePAD2~0~~Y~0~0~0.2~102,100"
)

# Schematic LIB uses T~P (single char), not TEXT~P, and the designator
# lives at a different field index. Real-shape derived.
SCH_LIB_R1 = (
    "LIB~200~-200~package`R0603`Device`Res`Supplier`LCSC`Supplier Part`C99782`"
    "~0~0~ggeR1~1~~yes~yes~~~"
    "#@$T~N~200~-190~0~#000080~Arial~6.5pt~5~~text-after-edge~comment"
    "~4.7K~1~start~ggeN1~0~pinpart"
    "#@$T~P~200~-210~0~#000080~Arial~6.5pt~5~~text-after-edge~comment"
    "~R1~1~start~ggeP1~0~pinpart"
)


# ---------- replace_designator_in_lib ----------

def test_replace_designator_pcb_style_updates_TEXT_P():
    out = replace_designator_in_lib(PCB_LIB_R1, "R99")
    # Old designator field is gone, new one in its place.
    assert "~R1~M " not in out
    assert "~R99~M " in out
    # Value text (TEXT~N ~4.7K~) is untouched.
    assert "~4.7K~M " in out


def test_replace_designator_schematic_style_updates_T_P():
    out = replace_designator_in_lib(SCH_LIB_R1, "R99")
    assert "~R1~1~start" not in out
    assert "~R99~1~start" in out
    # Value text unchanged.
    assert "~4.7K~1~start" in out


def test_replace_designator_rejects_non_LIB_records():
    with pytest.raises(ValueError, match="not a LIB"):
        replace_designator_in_lib("TRACK~1~1~SDA~10 10 20 20~gge1~0", "R99")


def test_replace_designator_rejects_lib_without_designator_subrecord():
    # LIB with only a TEXT~N (value) sub-record — no designator to replace.
    bare = (
        "LIB~100~100~package`R0603`~0~0~ggeR1~1~~yes~yes~~~"
        "#@$TEXT~N~100~95~0.6~0~0~3~~1.9685~4.7K~M "
    )
    with pytest.raises(ValueError, match="T~P/TEXT~P"):
        replace_designator_in_lib(bare, "R99")


# ---------- rename_net_auto_suffix ----------

def test_rename_net_auto_suffix_rewrites_matching_pad_nets():
    out = rename_net_auto_suffix(PCB_LIB_R1, "R1", "R99")
    assert "~R1_1~" not in out
    assert "~R1_2~" not in out
    assert "~R99_1~" in out
    assert "~R99_2~" in out


def test_rename_net_auto_suffix_leaves_named_nets_alone():
    named_pad = (
        "PAD~RECT~98~100~1.5~1.5~1~SDA~1~0~97 99 99 99 99 101 97 101"
        "~0~ggePAD1~0~~Y~0~0~0.2~98,100"
    )
    assert rename_net_auto_suffix(named_pad, "R1", "R99") == named_pad


def test_rename_net_auto_suffix_leaves_partial_prefix_matches_alone():
    """`R1_A` matches the underscore-prefix but the suffix isn't all digits,
    so it's a user-named net (or unrelated to R1) and stays."""
    weird_pad = (
        "PAD~RECT~98~100~1.5~1.5~1~R1_A~1~0~97 99 99 99 99 101 97 101"
        "~0~ggePAD1~0~~Y~0~0~0.2~98,100"
    )
    assert rename_net_auto_suffix(weird_pad, "R1", "R99") == weird_pad


def test_rename_net_auto_suffix_ignores_similar_prefixes():
    """Renaming R1 must not touch R10_1 — the shared 'R1' at the start
    is a substring, but the prefix-plus-underscore check catches it."""
    pad_r10 = (
        "PAD~RECT~98~100~1.5~1.5~1~R10_1~1~0~97 99 99 99 99 101 97 101"
        "~0~ggePAD1~0~~Y~0~0~0.2~98,100"
    )
    assert rename_net_auto_suffix(pad_r10, "R1", "R99") == pad_r10


def test_rename_net_auto_suffix_rewrites_track_net():
    track = "TRACK~1~1~R1_1~102 100 198 100~ggeTRACK1~0"
    out = rename_net_auto_suffix(track, "R1", "R99")
    assert "~R99_1~" in out
    assert "~R1_1~" not in out


def test_rename_net_auto_suffix_rewrites_via_net():
    via = "VIA~102~100~2~R1_1~1~ggeV1~0"
    out = rename_net_auto_suffix(via, "R1", "R99")
    assert "~R99_1~" in out


def test_rename_net_auto_suffix_noop_on_same_prefix():
    assert rename_net_auto_suffix(PCB_LIB_R1, "R1", "R1") == PCB_LIB_R1


def test_rename_net_auto_suffix_noop_on_empty_prefix():
    assert rename_net_auto_suffix(PCB_LIB_R1, "", "R99") == PCB_LIB_R1
    assert rename_net_auto_suffix(PCB_LIB_R1, "R1", "") == PCB_LIB_R1
