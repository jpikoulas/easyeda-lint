"""Low-level string surgery on EasyEDA shape[] records.

A shape entry is a tilde-delimited string that may carry `#@$`-joined
sub-records (for LIB entries). We deliberately work at the string layer
rather than re-serialising from our parsed model — that way any field we
don't understand is preserved byte-for-byte, and EasyEDA can still reopen
the file after --fix runs.

Two primitives:

    replace_designator_in_lib(shape_str, new)
        Rewrites the T~P/TEXT~P sub-record's designator field.

    rename_net_auto_suffix(shape_str, old_prefix, new_prefix)
        Rewrites any PAD/TRACK/VIA net matching `<old>_<digits>` to
        `<new>_<digits>`. This is EasyEDA's auto-generated naming
        pattern; user-chosen names like `SDA` or `3V3` are never touched.
"""
from __future__ import annotations

# Sub-record kinds that carry a net name in a known field position.
# The value is the field index the net string lives at.
_NET_FIELD_INDEX = {
    "PAD":   7,   # PAD~SHAPE~x~y~w~h~layer~NET~pin~hole~...
    "TRACK": 3,   # TRACK~width~layer~NET~coords~...
    "VIA":   4,   # VIA~x~y~outerD~NET~innerR~ggeId~vis
}

# Designator text lives at a fixed field index inside T~P (schematic) or
# TEXT~P (PCB) sub-records — matches parser._text_value's `fixed_idx`.
_DESIGNATOR_TEXT_INDEX = {
    "T":    12,   # schematic
    "TEXT": 10,   # PCB
}


def replace_designator_in_lib(shape_str: str, new_designator: str) -> str:
    """Return a new shape string with the LIB's designator text replaced.

    Raises ValueError if the record isn't a LIB or the T~P/TEXT~P
    sub-record can't be located — the caller should treat that as
    "refuse to fix" rather than guess.
    """
    if not shape_str.startswith("LIB"):
        raise ValueError("shape record is not a LIB entry")

    parts = shape_str.split("#@$")
    header = parts[0]
    subs = parts[1:]
    if not subs:
        raise ValueError("LIB has no sub-records — nothing to rewrite")

    replaced = False
    for i, sub in enumerate(subs):
        sub_fields = sub.split("~")
        if len(sub_fields) < 2:
            continue
        head = sub_fields[0]
        if sub_fields[1] != "P":
            continue
        idx = _DESIGNATOR_TEXT_INDEX.get(head)
        if idx is None or idx >= len(sub_fields):
            continue
        sub_fields[idx] = new_designator
        subs[i] = "~".join(sub_fields)
        replaced = True
        break

    if not replaced:
        raise ValueError("no T~P/TEXT~P sub-record with a designator text field found")

    return "#@$".join([header, *subs])


def rename_net_auto_suffix(
    shape_str: str, old_prefix: str, new_prefix: str
) -> str:
    """Rewrite auto-generated net names of the form `<old>_<digits>`.

    Applied conservatively — the old prefix must be followed by an
    underscore and one-or-more digits, and nothing else. That's the
    exact shape EasyEDA generates for unlabelled component nets; a
    user-named net like `SDA` or `3V3` will never match.

    Walks every sub-record of the shape (PAD, TRACK, VIA), so the same
    helper works for LIB records (which carry PADs) and free-standing
    TRACK/VIA records.
    """
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return shape_str

    prefix_with_underscore = old_prefix + "_"
    replacement_prefix = new_prefix + "_"

    parts = shape_str.split("#@$")
    for i, part in enumerate(parts):
        fields = part.split("~")
        if not fields:
            continue
        head = fields[0]
        net_idx = _NET_FIELD_INDEX.get(head)
        if net_idx is None or net_idx >= len(fields):
            continue
        net = fields[net_idx]
        if not net.startswith(prefix_with_underscore):
            continue
        suffix = net[len(prefix_with_underscore):]
        if not suffix.isdigit():
            continue
        fields[net_idx] = replacement_prefix + suffix
        parts[i] = "~".join(fields)

    return "#@$".join(parts)
