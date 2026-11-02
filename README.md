# easyeda-lint

[![CI](https://github.com/jpikoulas/easyeda-lint/actions/workflows/tests.yml/badge.svg)](https://github.com/jpikoulas/easyeda-lint/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Sanity-check EasyEDA schematic and PCB JSON exports before you order the board.**

A fast offline linter for [EasyEDA Standard](https://easyeda.com/) designs. Parses
the JSON export, lists every component with values and part numbers, and runs a
growing catalogue of rules that catch the class of mistake that DRC misses:
naming inconsistencies, orphan nets, missing decoupling caps, drifted designators
between schematic and PCB, and so on.

> **Status: alpha.** Tested against a handful of real boards. False positives are
> possible; issues and PRs welcome. The rule catalogue grows one entry per bug
> found on a real design.

## Why

EasyEDA has built-in DRC (physical clearance) and ERC (electrical rules), and
they're useful. But they miss whole classes of design mistake:

- The reference-designator `1NF` on a capacitor whose value field says `100nF`.
- A power rail named after its source component (`U3_1`) rather than what it
  actually is (`3V3`) — so no automated tool notices when the decoupling cap
  is missing.
- Designator drift between schematic and PCB (`BATT_CONN` on the schematic,
  `BATT` on the PCB) that silently breaks BOM/netlist tooling downstream.
- The via that was orphaned when the trace above it got rerouted, and nothing
  ever cleaned it up.

`easyeda-lint` is a git-friendly, CI-friendly text tool you can run before
pressing **Order**. Runs in under a second on any real board.

## Install

```bash
pip install easyeda-lint
```

Or from source:

```bash
git clone https://github.com/jpikoulas/easyeda-lint
cd easyeda-lint
pip install -e .
```

Python 3.10 or newer. No runtime dependencies outside the standard library.

## Quick start

```bash
# Lint a single PCB export
easyeda-lint pcb.json

# Lint schematic + PCB together (runs cross-checks between them)
easyeda-lint schematic.json pcb.json

# CI usage — exit non-zero on WARN findings too
easyeda-lint *.json --fail-on warn
```

Also runnable as a module:

```bash
python -m easyeda_lint schematic.json pcb.json
```

### Getting the JSON

In EasyEDA Standard: **File → EasyEDA File Source** or **Document → Save
Source File** exports the JSON representation of the current schematic or PCB.
Point `easyeda-lint` at those files.

## Example output

```
PCB: (untitled)  —  ./my_board.json  [EasyEDA 6.5.57]

Components:
Designator                Value       Package       Device            Supplier Part   Pins
------------------------  ----------  ------------  ----------------  --------------  -----
C1                        100nF       C0402         CAP_0402          C1525           2
R1                        4.7K        R0603         Res_0603          C99782          2
U1                        LM358       SOIC-8        LM358DR2G         C7950           8

Nets:  4 named net(s), 12 pad connections

Findings:
WARN (1)
  [value-designator-mismatch] 1NF: designator '1NF' implies 1 nF, value '100nF' parses as 100 nF

SCHEMATIC ↔ PCB cross-check
WARN (1)
  [find_schematic_pcb_mismatch] BATT_CONN ↔ BATT: designator differs between schematic and PCB — same part & package suggests one side got renamed
```

## What it checks

Rules are organised in tiers by ambition; every tier is enabled by default.

| Tier | Focus | Example rules |
|------|-------|---------------|
| 1 | Component listing & basic hygiene | `duplicate-designator`, `missing-value`, `no-part-number` |
| 2 | Value & naming consistency | `unparseable-value`, `value-designator-mismatch` |
| 3 | Geometry & topology | `find_orphan_net_labels`, `find_via_antennas`, `find_pads_center_not_connected` |
| 4 | Electrical intent | `find_unconnected_ic_pins`, `find_missing_decoupling` |
| 5 | PCB physical hygiene | `find_components_outside_board_outline`, `find_pads_near_board_edge`, `find_acute_angles` |
| 6 | Cross-file & topology-aware | `find_missing_decoupling_by_label` (cross-check), `find_missing_i2c_pullups`, `find_missing_reset_pullup` |

The rule *names* follow the convention established by
[salitronic/eda-agent](https://github.com/salitronic/eda-agent) so folks
coming from that tool find them familiar. Implementations are independent
(see [NOTICE](NOTICE)).

Some rules ship implemented-but-not-registered when EasyEDA's JSON doesn't
give us enough signal to avoid noise (e.g. `find_off_grid_components` needs
the active placement grid which the format doesn't expose). Enable those
manually in your own script if you know your grid.

## What it is NOT

- Not a replacement for EasyEDA's built-in DRC (physical clearance /
  manufacturing rules) — run that too.
- Not a simulator.
- Not a counterfeit-part detector — buy your chips from Mouser, Farnell, or
  Digi-Key, not AliExpress.
- Not tied to a live CAD app (unlike eda-agent, which needs Altium, KiCad,
  or EasyEDA Pro running).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

Short version: adding a new rule is 5–20 lines. Write a function, register it
in `ALL_CHECKS` (or `CROSS_CHECKS`), add a test case with a fixture that
triggers it. When you find a new class of mistake on your own board that this
tool didn't catch, that's a rule waiting to be written.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
