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
| `google` | **required** | calendar and gmail evidence | `gws` (or `gog`) on PATH; `source ~/.profile` |
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
opp-axi sweep                # weekly-sweep coverage
opp-axi activity <ref> --add "..."   # prepend an SE Activity entry (verified PATCH)
opp-axi cal --date today
opp-axi mail 'subject:X newer_than:2d'
opp-axi evidence <ref>       # cal + zoom + mail + repo + wispr
opp-axi wispr                # cached meetings, matched to opps
opp-axi triage               # signal -> pattern -> proposed work
opp-axi fields               # SE field reference
opp-axi doctor               # what is configured, what is not
opp-axi brief <ref>          # a budgeted slice of OPP.md, not the whole file
opp-axi log <ref> "text"     # append a dated Log entry without reading the file
```

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
