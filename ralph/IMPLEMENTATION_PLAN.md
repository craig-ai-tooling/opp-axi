# Implementation plan

Scope declaration for CI. A PR touching paths outside the allowlist is denied.

```allowlist
opp_axi/**
tests/**
docs/**
scripts/**
README.md
AGENTS.md
CLAUDE.md
Makefile
pyproject.toml
ralph/**
```

`.github/workflows/**` is deliberately NOT in the allowlist. An agent that can edit
the workflow judging it can approve itself; changes there go in their own PR and
need Craig.

## Tasks

- [x] Extract from `customer-opportunities/automation/` into a standalone package
- [x] Connector table with `required` / `optional`, and a `doctor` that probes it
- [x] `E_PARTIAL` — an unread source can no longer exit 0
- [x] zipapp build, tag-triggered release, private-repo installer
- [ ] Remove the original script from `customer-opportunities` once the symlink has
      been proven in daily use for a week
- [ ] Decide whether `triage`'s dependency on ai-lawnmower's `patterns.yaml` should
      become a vendored default rather than a cross-repo path

## Out of scope

- The AXI service boundary (lm-33's second half). This repo is the precondition
  for it, not the thing itself.
