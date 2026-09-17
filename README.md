# opp-axi

Agent-ergonomic CLI over Salesforce + Google Workspace for SE pipeline work.

Built on AXI principles: token-efficient TOON output, minimal default schemas,
truncation with `--full` escape hatches, pre-computed aggregates, definitive empty
states, structured exit codes, and next-step disclosure. The point is not that less
data moves — it is that less data reaches the model's context. The subprocess may
pull 165KB; the agent sees 4KB.

## Install

The release artifact is a single-file zipapp. It needs `python3 >= 3.10` on the
target; it is not a static binary.

```sh
curl -fsSL https://raw.githubusercontent.com/craig-ai-tooling/opp-axi/main/scripts/install.sh | bash
```

or by hand:

```sh
curl -fsSL https://github.com/craig-ai-tooling/opp-axi/releases/latest/download/opp-axi.pyz \
  -o ~/.local/bin/opp-axi
chmod +x ~/.local/bin/opp-axi
```

Then, always:

```sh
opp-axi doctor
```

## Configure

**Start with `opp-axi doctor`.** It probes every connector, prints what is missing,
and prints the exact command that fixes each one. Nothing below needs memorising —
the doctor says it.

### Connectors

| connector | need | what it gives you | how to satisfy it |
|---|---|---|---|
| `salesforce` | **required** | opportunities, SE Activity, account data | `sf org login web --alias spectrocloud` |
| `google` | **required** | calendar, gmail and Drive evidence | `gws` (or `gog`) on PATH; `source ~/.profile` |
| `opp-repo` | **required** | local opp folders and `.salesforce.json` | clone the opp repo, set `OPP_REPO` |
| `wispr` | optional | meeting transcripts for `evidence` / `triage` | `/wispr-sync` in a Claude session, or `OPP_WISPR=off` |
| `patterns` | optional | `triage` rules | `pip install pyyaml`, set `OPP_PATTERNS` |
| `dispatch` | optional | `triage --push` queueing | install the `dispatch` CLI |

**Required** means the tool cannot do its job: a command that needs the connector
refuses, with a non-zero exit. **Optional** means the tool works without it — but a
command that *would* have used it says `UNAVAILABLE` and exits `5`. It never renders
as an empty result.

That distinction is the whole point. `wispr[0]: (none)` is a claim that there were
no meetings. `wispr: UNAVAILABLE` is an admission that nobody looked. Collapsing the
second into the first is how a stale cache reads as a quiet week.

### Running without Wispr

You do not need Wispr to use opp-axi. Everything except the `wispr` subcommand
works without it; `evidence` and `triage` simply return one fewer source.

```sh
opp-axi evidence acme            # works. wispr renders UNAVAILABLE, exit 5
OPP_WISPR=off opp-axi evidence acme   # works. wispr renders "disabled", exit 0
```

Set `OPP_WISPR=off` once (in your shell profile) if you will never have Wispr.
That converts the gap from *something is broken* into *this source does not apply
here*, and the exit code goes back to 0. The output still states, on every run,
that meetings were not searched — because a conclusion drawn from four sources when
there are five should say so.

### Environment

| var | default | meaning |
|---|---|---|
| `OPP_REPO` | `~/code/customer-opportunities` | the opp folders |
| `SF_ORG` | `spectrocloud` | `sf` org alias |
| `OPP_SE` | `CraigSmith` | SE assignment filter |
| `OPP_INITIALS` | `CS` | stamped on SE Activity entries |
| `OPP_WISPR` | `on` | `off` declares Wispr out of scope |
| `OPP_WISPR_DIR` | `~/.cache/opp-axi/wispr` | meeting cache (outside the repo: verbatim customer speech) |
| `OPP_PATTERNS` | `~/code/ai-lawnmower/patterns/patterns.yaml` | `triage` rules |
| `OPP_CS_FOLDER` | resolved by name at runtime | `Customers` folder id, to skip the name lookup |
| `OPP_CS_DRIVE` | `Customer Success` | shared drive `folder` searches |
| `OPP_CS_CUSTOMERS` | `Customers` | folder within that drive holding one dir per customer |
| `OPP_AXI_STATE` | `$XDG_STATE_HOME/opp-axi` or `~/.local/state/opp-axi` | write locks + audit log (`writes.jsonl`) |
| `LAWNMOWER_GATEWAY` | unset | base URL of the lm-42 tool gateway. **Unset is the local `sf` CLI, exactly as before.** Set, `sf data query` goes over HTTP and a gateway failure is a refusal, not a quiet retry against `sf` |
| `LAWNMOWER_GATEWAY_TOKEN` | unset | bearer token; falls back to `LAWNMOWER_GATEWAY_TOKEN_FILE` |
| `LAWNMOWER_GATEWAY_TOKEN_FILE` | `~/.config/lawnmower/tool-gateway-token` | where the token is read from when the env var is unset |

### The tool gateway (lm-42)

`LAWNMOWER_GATEWAY` moves `sf data query` off the local CLI and onto a service that
holds the credential, so a pod does not need Craig's Salesforce login on disk. Unset
changes nothing, which is what lets verbs move one at a time.

```sh
opp-axi opps                                          # local sf, as always
LAWNMOWER_GATEWAY=http://tool-gateway.lawnmower.svc opp-axi opps
```

Verified 9/17/26: byte-identical output both ways (45 opps, 5,476 bytes), 3.0s local
against 3.7s through the gateway.

**A gateway failure is a failure.** It does not fall back to `sf` per call. A silent
fallback means the gateway can be broken for a week while every call quietly uses the
credential that was supposed to have moved, and nothing says so. The error names the
gateway, so a dead service does not read as "sf query failed" and send you to the wrong
machine.

## Exit codes

| code | meaning |
|---|---|
| 0 | complete |
| 1 | error — a required connector is down, or the command failed |
| 2 | usage |
| 3 | not found |
| 4 | refused (a guard said no) |
| 5 | **partial** — the command ran, but a declared source went unread and said so |

`5` exists so a caller can tell "there was nothing" from "I could not look" without
parsing stdout for a warning string. Nobody parses stdout for a warning string.

## Use

```sh
opp-axi                      # overview (no args = live data)
opp-axi opps                 # open opps
opp-axi opp <ref>            # one opp
opp-axi sweep                # weekly-sweep coverage: who is missing an SE Activity entry
opp-axi coverage             # what every opp owes: SE fields AND repo collateral, by stage
opp-axi coverage --artifacts # the repo-collateral half only (--fields for the Salesforce half)
opp-axi activity <ref> --add "..."   # prepend an SE Activity entry (guarded, verified)
opp-axi field <ref> Field=value [Field=value ...]   # write SE-owned fields (guarded)
opp-axi undo <write-id>       # restore a guarded write's prior value
opp-axi writes                # audit log of guarded writes
opp-axi writes --full --json  # + friendly label and the whole old/new values
opp-axi cal --date today
opp-axi mail 'subject:X newer_than:2d'
opp-axi evidence <ref>       # cal + zoom + mail + repo + wispr
opp-axi wispr                # cached meetings, matched to opps
opp-axi triage               # signal -> pattern -> proposed work
opp-axi folder <ref>         # CS customer-folder state: is it there, what is missing from it
opp-axi folder --all         # the same sweep across every open opp
opp-axi folder <ref> --populate      # copy in only the artifacts that are not there yet
opp-axi rep                  # what reps wrote across your open opps (read-only)
opp-axi rep <ref>            # rep/AE-entered data on one opp: next steps, MEDDPICC, calls, notes, history
opp-axi fields               # SE field reference
opp-axi fields rep           # rep/AE-owned field reference (read-only)
opp-axi doctor               # what is configured, what is not
opp-axi brief <ref>          # a budgeted slice of OPP.md, not the whole file
opp-axi log <ref> "text"     # append a dated Log entry without reading the file
```

## Customer folders

`folder` answers one question: does this opp have a folder in the Customer Success
shared drive yet, and if it does, which of the required artifacts are missing from
it. SEs are readers on that drive, so the folder is created by a Content Manager or
a drive Manager, often well after the opp needs one. A missing folder is therefore
an ordinary reported state rather than an error, and the check is safe to re-run
until it appears.

Three artifact states, and they mean different things:

| state | meaning |
| --- | --- |
| `present` | already in the customer folder, nothing to do |
| `copyable` | exists elsewhere in Drive; `--populate` copies it in |
| `absent` | does not exist anywhere — somebody has to write it |

A folder that could be one of two accounts reports `AMBIGUOUS`, never `MISSING`.
Those are different facts, and guessing files one customer's plan into another
customer's folder. `--populate` refuses on a folder this account cannot write to
instead of failing mid-copy.

## Writing

`activity`, `field` and `undo` are the only write paths, and every one of them goes
through the same guard (`opp_axi/guard.py`): a per-opp lock held across a re-read,
compare and PATCH; a compare-and-swap that refuses (exit 4) if the field changed
since you last read it; and an audit line in `~/.local/state/opp-axi/writes.jsonl`
written BEFORE the PATCH and confirmed after, so a crash mid-write still leaves a
`pending` record rather than nothing. `opp-axi writes` lists that log; `opp-axi undo
<id>` reverts one entry, but only if the field still holds exactly what that write
put there.

`writes --full --json` returns the same rows with the field's friendly label (the
one in `cli.FIELDS`, e.g. `Sales_Engineer_Overview__c` -> "SE Activity"), the WHOLE
`new` value rather than its first line, and the `old` the ledger recorded. That is
what a caller needs to render a write instead of list one, and it is emitted from
here so nothing downstream keeps a second copy of the label table. `--full` without
`--json` is refused: TOON is a table and these values are multi-line.

`field` is deliberately not called `set` — a global shell guard on this box blocks
the bare word `set` as a command.

`activity` additionally refuses a second same-day entry unless you pass `--amend`,
and lints the text before it ever reaches Salesforce (correction-framing, process
critique, internal pricing/roadmap/competitive mentions). `--allow <rule>` overrides
a specific lint rule; the override is recorded in the audit line, never applied
silently. `--dry-run` runs the same lint/dedupe/compare-and-swap checks and prints
what would change, without writing anything — not even an audit line.

## Build from source

```sh
make build     # dist/opp-axi.pyz
make install   # -> ~/.local/bin/opp-axi
make test
make lint
make doctor
```

## Develop

```sh
python -m pip install -e ".[dev]"
```

A release is cut by pushing a `v*` tag; `.github/workflows/release.yml` builds the
zipapp and attaches it. The version lives in `opp_axi/__init__.py` and nowhere else.
