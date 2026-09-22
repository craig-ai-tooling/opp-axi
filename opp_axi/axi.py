# VENDORED from craig-ai-tooling/axi-py v0.1.0 sha256=05b1a966d9de629b9a28e251f149c055b63836e58d4d5ffaffd3057cc1c55bf5
# Do not edit here: change craig-ai-tooling/axi-py, tag it, then run `make vendor-axi AXI_PY_REF=<tag>`.
"""axi — the output and exit-code contract every Python AXI tool shares.

One file, stdlib only, Python 3.10+. Each tool carries a VENDORED copy inside its
own package (so its single-file zipapp keeps having no dependencies) and a
generated test that fails if that copy is edited in place. Change it here, tag a
release, then run `make vendor-axi` in each tool.

Why it exists: opp-axi and palette-axi each carried this code under a comment
saying "copied verbatim, both tools must agree", and they had already started to
disagree. monday-axi and launchpad-axi wrote their own encoders, and both could
emit a row that does not parse.

The exit codes are the table in ai-lawnmower docs/axi-contract.md.
"""
import sys

__version__ = "0.1.0"

# Exit codes. A caller checks $?, never stdout, so every tool uses the same table.
#   0 E_OK        success
#   1 E_ERR       a required source was unreachable, or any failure not below
#   2 E_USAGE     bad args, or an ambiguous name that needs disambiguating
#   3 E_NOTFOUND  no such thing
#   4 E_REFUSED   refused on policy grounds
#   5 E_PARTIAL   finished, but an optional source could not be consulted
E_OK, E_ERR, E_USAGE, E_NOTFOUND, E_REFUSED, E_PARTIAL = 0, 1, 2, 3, 4, 5


def _tv(v):
    """TOON scalar: quote only when it would break the row.

    None and False are DIFFERENT facts and must not share a cell. Collapsing
    False into "" made "POV not required" read identically to "nobody knows",
    which is exactly the kind of silent wrong answer these tools exist to stop.
    Empty means unknown; false means known-false."""
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    s = str(v).replace("\r", "")
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""').replace("\n", "\\n") + '"'
    return s


def toon(name, fields, rows, indent="  "):
    """TOON block. rows==[] yields a definitive `name[0]{...}: (none)` — never ambiguous."""
    head = f"{name}[{len(rows)}]{{{','.join(fields)}}}:"
    if not rows:
        return head + " (none)"
    return "\n".join([head] + [indent + ",".join(_tv(r.get(f)) for f in fields) for r in rows])


def emit(*parts):
    print("\n".join(p for p in parts if p))


def nxt(*suggestions):
    """Contextual disclosure: what to run next, not a wall of help text."""
    return "\nnext: " + " | ".join(suggestions) if suggestions else ""


def die(msg, code=E_ERR):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def size_hint(full, shown):
    extra = len(full) - len(shown)
    return f"  [+{extra}B truncated, --full]" if extra > 0 else ""
