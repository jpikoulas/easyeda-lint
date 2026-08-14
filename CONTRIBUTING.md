# Contributing to easyeda-lint

Thanks for the interest. This tool grows one rule per bug found on a real
design — real-world friction is the best fuel for a linter.

## Ways to help, in order of value

1. **Run it on your own designs, and tell us what it got wrong.** False
   positives are the highest-priority bug class — a linter that cries wolf
   gets ignored. If you can share the shape of the design (redacted is
   fine), we can add a test fixture that pins the fix.
2. **Add a rule for a mistake you've made.** The pattern is small, see
   below.
3. **Test coverage on the parser.** EasyEDA's JSON format has a lot of
   version-dependent shape; every real file you can point at is welcome.
4. **Docs and examples.** Especially: annotated example output showing
   what each rule catches, on synthetic fixtures.

## Development setup

```bash
git clone https://github.com/jpikoulas/easyeda-lint
cd easyeda-lint
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

## Adding a rule

The pattern is deliberately small so adding one is quick.

1. **Write a function** in `easyeda_lint/checks.py`. Shape:

   ```python
   def rule_find_my_new_check(doc: Document) -> Iterator[Finding]:
       """One-line summary of what this catches."""
       if doc.kind is not DocKind.PCB:
           return  # or SCHEMATIC, depending on where the rule applies
       for c in doc.components:
           if <something-is-wrong>:
               yield Finding(
                   severity=Severity.WARN,   # ERROR / WARN / INFO
                   rule="find_my_new_check",
                   message="specific, evidenced description of the problem",
                   component=c.designator,
                   where=doc.kind.value,
               )
   ```

   For a rule that compares schematic against PCB, use the
   `CrossCheckFn` signature (takes `sch, pcb` and lives in `CROSS_CHECKS`).

2. **Register it** by appending to `ALL_CHECKS` (single-doc) or
   `CROSS_CHECKS` (schematic+PCB) at the bottom of `checks.py`.

3. **Add a test.** Put a tiny synthetic JSON fixture in
   `tests/fixtures/` that intentionally triggers your rule, and a test
   case in `tests/test_checks_*.py` that asserts the finding fires with
   the right severity + rule name.

4. **Update the README** rule table if the rule spans a new category.

## Adding a fix category

`--fix` is deliberately narrow. A category is only in scope if the fix is
mechanical, unambiguous, and can be verified after writing — a fixer that
guesses wrong is worse than no fixer.

To add one:

1. **Write the low-level edit** in `easyeda_lint/shape_edit.py`. Keep it
   string-in / string-out and preserve every field you don't need to
   touch — the whole point of working at the shape[] string layer is
   that EasyEDA can still open the file after we edit it.
2. **Add a planner** in `easyeda_lint/fix.py` that yields `Fix` objects
   with `kind="modify"` (with a `modifier` callable) or `kind="delete"`.
3. **Register the category name** in `KNOWN_CATEGORIES` at the top of
   `fix.py`. The CLI reads this set to validate `--fix <name>`.
4. **Add an end-to-end test** in `tests/test_fix.py` that runs the full
   pipeline (plan → apply → re-parse) and confirms the fix landed and
   the round-trip verifier stays green.

The round-trip verifier is the safety net — if your fix introduces any
new finding, the CLI deletes the output and exits non-zero. If your
category legitimately changes messages on pre-existing findings (e.g. a
rename that renames a net referenced in other rules' messages), add the
mapping to `FixPlan.renames` so the verifier can normalise.

## Rule design principles

Stolen from
[eda-agent's REVIEW_QUALITY_RULES](https://github.com/salitronic/eda-agent/blob/main/src/eda_agent/tools/review.py):

1. **Run and triage, never relay.** A raw dump of warnings is useless.
   Every finding should be investigated to real-or-false before it's
   surfaced.
2. **No "homework" findings.** Never emit "verify X" / "check X". Trace
   it and state the conclusion.
3. **Specific, evidenced, actionable.** Every finding names the exact
   component, pin, net; cites the fact; states the concrete fix.
4. **Topology vs function.** A single-pin net is a *netlist* fact — state
   it. A pin's *function* is a *datasheet* fact — never infer from the
   symbol label.
5. **Not everything is a finding.** Normal design practice is noise;
   don't inflate the count.
6. **Calibrate severity.** ERROR / WARN for real evidenced problems.
   Unconfirmed → INFO.

If your rule can only speak with 50% confidence, use INFO, not WARN.

## Code style

- Ruff-clean (`ruff check .`).
- Python 3.10+ syntax is fine.
- No dependencies outside the standard library for the core package.
  `dev` extras (pytest, ruff) are the only allowed non-stdlib.
- Comments should explain the *why*, not restate the code. Especially:
  when you know a rule has a limitation, comment that limitation next
  to the code so the next person doesn't waste time rediscovering it.

## Reporting bugs

Include:

- The command you ran.
- The output (or a redacted version).
- If the parser crashed or produced wrong output on a real file, either
  attach a minimal reproducer JSON or share a link to a public EasyEDA
  design that reproduces.

## License

By contributing, you agree that your contributions will be licensed
under this project's MIT license.
