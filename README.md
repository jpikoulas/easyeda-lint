# easyeda-lint

Sanity-check EasyEDA schematic and PCB JSON exports before fabrication.

The goal is a fast "did I forget anything, are the values right, is anything weird"
pass that runs on the raw EasyEDA JSON. It doesn't replace a proper DRC — it catches
the class of mistakes that DRC misses: naming inconsistencies, orphaned nets,
suspicious values, and human-eye-fatigue-slips.

## What it checks (MVP)

**Tier 1 — topology, always run:**
- Every component listed with designator, value, package, part number
- Nets with only one pad (dangling)
- Duplicate reference designators
- Components with missing value/package/part fields
- Schematic components without a corresponding PCB footprint (when both files given)

**Tier 2 — naming/consistency:**
- Value field parses as a real electrical value (`100nF`, `10K`, `4.7uF`)
- Designator's implied type matches value type (e.g. designator hints at a cap → value looks like capacitance)
- Value-vs-designator label mismatch (e.g. designator `1NF` but value `100nF`)

## What it does NOT do

- Physical DRC (trace-width, clearance) — that's EasyEDA's job
- Simulation
- Counterfeit-part detection (a real one caught us on the M5Stamp soil sensor
  board — no tool would have flagged it; buy from Mouser/Farnell/Digi-Key)

## Usage

```bash
python -m easyeda_lint path/to/schematic.json
python -m easyeda_lint path/to/pcb.json
python -m easyeda_lint path/to/schematic.json path/to/pcb.json  # cross-check
```

## Status

Early. Adding rules as they catch real issues on my own boards.
