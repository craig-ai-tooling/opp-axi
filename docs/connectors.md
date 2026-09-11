# The connector contract

## The failure this prevents

A tool that reaches five sources and can only reach four has two honest options:
refuse, or say which one it missed. It has one dishonest option, which is the one
everybody ships by accident: render the missing source as empty and exit 0.

That is how `quota unknown` read as healthy for days on this platform. It is how a
stale meeting cache reads as a quiet week, and then somebody writes "No new activity
this week" into Salesforce about an account that had three meetings.

## The rule

Every external source is declared in `CONNECTORS` with one of two needs:

- **required** — the tool cannot do its job without it. A command that needs it
  refuses before doing any work, exits `1`, and names the command that fixes it.
- **optional** — the tool works without it, less well. A command that would have
  used it renders `UNAVAILABLE`, exits `5` (`E_PARTIAL`), and names the fix.

There is no third kind. "Best effort" is not a need; it is how the empty-result bug
gets in.

## Three states, not two

The mistake is modelling a source as present/absent. It has three states:

| state | rendering | exit | meaning |
|---|---|---|---|
| configured | the data | 0 | it worked |
| not configured | `UNAVAILABLE — <reason> \| fix: <command>` | 5 | nobody looked |
| declared off | `disabled (OPP_WISPR=off)` | 0 | it does not apply here |

The third state is what makes the tool usable by someone who will never have Wispr.
Without it, an honest configuration is indistinguishable from a permanent fault, and
the user learns to ignore the warning — which costs more than the warning saved.

## Why the exit code and not just the message

Because nothing parses stdout for a warning string. The warning text in this tool
was already loud and correct before this change; it was also invisible to every
caller, every script, and every agent that checked `$?`. A contract nobody can
check mechanically is documentation, not a contract.

## Adding a connector

1. Write `_probe_x()` returning `(status, detail, fix)`. It must not raise — but if
   it does, `doctor` renders it as `down` rather than dying.
2. Add a row to `CONNECTORS` with `required` or `optional` and one line of *why*.
3. If it is optional, call `gap()` at the point of use so the ledger records it.
4. Add a test. `tests/test_connectors.py` pins the shape, not the network.
