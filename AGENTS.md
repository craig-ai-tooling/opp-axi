# AGENTS.md

## Why this repo exists

`opp-axi` was a 1,328-line script inside `customer-opportunities/automation/`,
reached by a `~/bin` symlink into one checkout. That worked on exactly one machine.
It is now its own tool: installable, versioned, released, and — the part that
mattered enough to justify the move — **honest about what it could not reach**.

## What it is

One Python package, stdlib only. `opp_axi/cli.py` holds every verb.
`opp_axi/connectors` is not a module — the connector table is a literal list in
`cli.py` named `CONNECTORS`, and that is deliberate: one table, one place to look.

- `opp_axi/__init__.py` — `__version__`. The only place a release bumps.
- `opp_axi/cli.py` — verbs, TOON output, connector probes, `doctor`.
- `opp_axi/templates/` — the OPP.md seed, shipped with the tool so `scaffold`
  does not need a file in the data repo.
- `tests/` — the connector contract. Not "does Salesforce work".

## How — the only commands that matter

```sh
make build     # dist/opp-axi.pyz (zipapp, python3 >= 3.10)
make test
make lint
make doctor    # build, then probe this machine
```

A release is a `v*` tag. Nothing else publishes.

## Hard rules

**A source that could not be read is never rendered as an empty result.** This is
the rule the repo exists to hold. `wispr[0]: (none)` claims there were no meetings.
`wispr: UNAVAILABLE` admits nobody looked. If you find yourself emitting an empty
table for a source you failed to reach, you have reintroduced the bug.

**Every connector is declared in `CONNECTORS` with `required` or `optional`.**
There is no third kind and no undeclared dependency. Adding a new external call
means adding a row and a probe, or you have made a promise the doctor cannot check.

**`required` down → refuse (exit 1). `optional` absent → report and exit 5.**
Never exit 0 with an unread source. `_GAPS` is the ledger; `main()` converts a
non-empty ledger into `E_PARTIAL`. Do not bypass it by printing a warning and
returning.

**A probe must never crash the doctor.** Probing is the doctor's whole job; an
exception inside one is rendered as `down`, not propagated.

**`OPP_WISPR=off` is not the same as Wispr being broken.** A declared opt-out exits
clean. Losing that distinction turns an honest configuration into a permanent error.

**Never print a secret.** No API keys, no tokens, no `op` item contents in any
output, including `doctor --json`.

**This tool writes to live Salesforce.** `activity` does a verified REST PATCH.
Never use `sf data update record -v` — it writes null for emoji picklists and
mangles newlines while reporting success.

## Conventions

- TOON output, not JSON, unless `--json` is asked for.
- Empty states are definitive: say which source produced them.
- `next:` lines suggest the next command, never a paragraph.
- Dates are M/D/YY in anything a human reads.
