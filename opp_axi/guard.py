"""opp_axi/guard.py — the guard around every Salesforce write.

Every write in opp-axi goes through guarded_patch(): a per-opp flock held across
re-read -> compare -> PATCH -> read-back, an audit line written BEFORE the PATCH
and confirmed after, and a compare-and-swap so two sessions racing the same field
lose LOUDLY (E_REFUSED) instead of quietly dropping one write. That is the gap this
module closes: two sessions prepending SE Activity within seconds of each other
used to lose one entry with no trace it ever existed.

This module imports `opp_axi.cli` at the top (module object, not names) and only
touches its attributes inside function bodies — never at import time — because
cli.py imports guard.py too (for the `field`, `undo` and `writes` verbs). Binding
the module object up front and resolving attributes lazily is what keeps that a
working import cycle instead of a broken one.

Env: OPP_AXI_STATE overrides the state dir for tests; otherwise
XDG_STATE_HOME/opp-axi, falling back to ~/.local/state/opp-axi.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timezone

from opp_axi import cli


# ── state dir ────────────────────────────────────────────────────────────────
def state_dir():
    override = os.environ.get("OPP_AXI_STATE")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "opp-axi")


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _locks_dir():
    return _ensure_dir(os.path.join(state_dir(), "locks"))


def _writes_path():
    _ensure_dir(state_dir())
    return os.path.join(state_dir(), "writes.jsonl")


@contextlib.contextmanager
def _opp_lock(opp_id):
    """Per-opp flock, held by the caller across re-read -> compare -> PATCH ->
    read-back. Two sessions writing the SAME opp serialize; different opps don't
    wait on each other."""
    path = os.path.join(_locks_dir(), f"{opp_id}.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── the one PATCH path ─────────────────────────────────────────────────────
def sf_patch(opp_id, body):
    """REST PATCH only. `sf data update -v` silently writes null for emoji picklists
    and mangles newlines in long textareas — it reports success either way.

    The only caller is guarded_patch() below: every write in opp-axi is locked,
    compare-and-swapped and audited before it reaches here."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, ensure_ascii=False)
    try:
        p = cli._run(["sf", "api", "request", "rest",
                      f"/services/data/{cli.API}/sobjects/Opportunity/{opp_id}",
                      "--method", "PATCH", "--body", f"@{path}", "-o", cli.ORG])
        if p.returncode != 0:
            cli.die(f"PATCH failed: {(p.stderr or p.stdout).strip()[:300]}")
    finally:
        os.unlink(path)


def _read_field(opp_id, field):
    recs = cli.sf_query(f"SELECT {field} FROM Opportunity WHERE Id = '{opp_id}'")
    if not recs:
        cli.die(f"opp {opp_id} not found", cli.E_NOTFOUND)
    return recs[0].get(field)


def _is_multipicklist(field):
    """True if `field` (an API name) is declared `multipicklist` in `opp-axi fields
    write` — `cli.FIELDS["write"]` is the one place the code already knows field
    types (see `_resolve_field()`), so this reads that table rather than keeping a
    second list. A field with no entry there (or any other declared kind) is not
    a multipicklist, full stop — a plain text field that happens to contain ';' is
    never guessed into one."""
    return any(api == field and kind == "multipicklist"
               for _label, api, kind, _values in cli.FIELDS["write"])


def _same(a, b, field=None):
    """Value equality tolerant of Salesforce's own normalization: None and "" are
    the same fact (empty), and text differs only if it differs after CRLF and
    trailing-whitespace normalization.

    Without this, a successful write can read back as a false `mismatch` — a
    textarea round-trips \\n as \\r\\n, or Salesforce trims trailing whitespace —
    and `undo` then refuses FOREVER, because `current != rec["new"]` never
    matches again even though nothing is actually wrong.

    `field`, when given, is the API name of the field being compared. Salesforce
    stores a multipicklist in picklist-DEFINITION order, not the order it was
    sent in, so a correct write's read-back can differ from what was written by
    member order alone. When `field` is declared `multipicklist` (per
    `_is_multipicklist`, the code's existing field-type table), the two sides are
    compared as sets of ';'-separated, whitespace-stripped members instead of as
    a literal string. Without a declared type there is no way to tell a
    multipicklist from a textarea that happens to contain ';', so anything not in
    that table — including a plain text field — still compares as an exact
    (normalized) string."""
    if a is None:
        a = ""
    if b is None:
        b = ""
    if isinstance(a, str) and isinstance(b, str):
        a = a.replace("\r\n", "\n").rstrip()
        b = b.replace("\r\n", "\n").rstrip()
        if field and _is_multipicklist(field):
            to_set = lambda s: {part.strip() for part in s.split(";") if part.strip()}
            return to_set(a) == to_set(b)
        return a == b
    return a == b


# ── audit log ────────────────────────────────────────────────────────────────
def _write_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"w-{stamp}-{secrets.token_hex(2)}"


def _append_audit(record):
    """One JSON line, appended under its own flock so a pending+verified pair from
    one write never interleaves with another opp's line mid-write. Append-only:
    nothing here ever rewrites an earlier line."""
    path = _writes_path()
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_writes():
    path = _writes_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def _latest_by_id(records):
    """Collapse pending+verified/mismatch pairs to one row per id — the LAST line
    written for that id wins, since pending is always written before the outcome."""
    by_id = {}
    for rec in records:
        by_id[rec.get("id")] = rec
    return by_id


# ── the guard itself ─────────────────────────────────────────────────────────
def guarded_patch(opp_id, slug, field, new_value, expected_old, *, reason, allow=(),
                   dry_run=False):
    """Lock the opp, re-read `field`, and refuse (E_REFUSED) if it no longer equals
    `expected_old` — a change since the caller last read it means a compare-and-swap
    would silently clobber whatever landed in between.

    On a real write: append a `pending` audit line, PATCH, read back, append the
    `verified`/`mismatch` line for the SAME id, and return that record. A mismatch
    is NOT raised here — it is a fact about the write, and the caller (which knows
    whether this was `activity`, `field` or `undo`) decides how loudly to report it.

    On --dry-run: run the same lock + CAS re-read, write NOTHING (no PATCH, no audit
    line), and return a preview record carrying `dry_run: True`.
    """
    with _opp_lock(opp_id):
        current = _read_field(opp_id, field)
        if not _same(current, expected_old):
            cli.die(f"{field} on {slug} changed since read — re-run", cli.E_REFUSED)

        if dry_run:
            return {"opp": opp_id, "slug": slug, "field": field, "old": current,
                     "new": new_value, "reason": reason, "allow": list(allow),
                     "dry_run": True}

        base = {
            "id": _write_id(),
            "at": datetime.now(timezone.utc).isoformat(),
            "opp": opp_id,
            "slug": slug,
            "field": field,
            "old": current,
            "new": new_value,
            "reason": reason,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            "allow": list(allow),
        }
        _append_audit({**base, "status": "pending"})
        try:
            sf_patch(opp_id, {field: new_value})
        except SystemExit:
            # cli.die() inside sf_patch already printed why. The pending line is
            # otherwise the last word on this write, and it would say a PATCH
            # that never happened is still in flight -- so close it out as
            # `failed` before letting the exit propagate.
            _append_audit({**base, "status": "failed"})
            raise
        readback = _read_field(opp_id, field)
        status = "verified" if _same(readback, new_value, field) else "mismatch"
        record = {**base, "status": status}
        _append_audit(record)
        return record


# ── activity: same-day dedupe + lint ─────────────────────────────────────────
def dedupe_top_entry(existing, stamp, initials):
    """True if `existing`'s newest entry already carries today's `M/D/YY INITIALS:`
    stamp — the exact prefix cmd_activity itself writes."""
    return cli.first_line(existing).startswith(f"{stamp} {initials}:")


_LEADING_DATE = re.compile(r"^[-\s]*(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})")


def _top_entry_end(lines):
    """Index of the first line (from 1) that starts a NEW entry, or len(lines) when
    the whole field is one entry with no second dated line."""
    for i in range(1, len(lines)):
        if _LEADING_DATE.match(lines[i]):
            return i
    return len(lines)


def top_entry(existing):
    """Just the newest entry's text -- everything from line 0 up to (not including)
    the next line that starts with a date. Shares the boundary rule with
    amend_top_entry so "the newest entry" means the same span whether it is being
    read (followups' Next: extraction) or replaced (activity --amend)."""
    lines = existing.split("\n")
    return "\n".join(lines[:_top_entry_end(lines)])


def amend_top_entry(existing, new_entry):
    """Replace today's top entry — everything from line 0 up to (not including) the
    next line that starts with a date — with `new_entry`. No second dated line means
    the whole field was one entry, and it is replaced entirely."""
    lines = existing.split("\n")
    rest = lines[_top_entry_end(lines):]
    return new_entry + ("\n" + "\n".join(rest) if rest else "")


LINT_RULES = {
    # "correcting my earlier assessment" / "one thing that genuinely landed" — an SE
    # Activity entry states the current fact, never that it is fixing a previous one.
    "correction-framing": [
        re.compile(r"\bcorrect(ing|ion)\b.*\b(earlier|previous|prior|my)\b", re.I),
        re.compile(r"\bone thing .*\b(landed|missed)\b", re.I),
    ],
    # Process critique belongs in OPP.md, not a field the customer's own AE reads.
    # First-person/plan phrasing only — "Customer has not sent the RVTools export
    # yet" is a fact about the CUSTOMER'S progress, not process critique, and must
    # not fire just because it contains "not sent".
    "process-critique": [
        re.compile(r"\bwe (never|didn't|did not|haven't|have not) (send|sent|follow|followed)\b",
                   re.I),
        re.compile(r"\b(not|never) following (the |our )?process\b", re.I),
        re.compile(r"\bthe (plan|process) (was|is) (never|not)\b", re.I),
    ],
    "internal-pricing": [
        re.compile(r"\$\s?\d"),
        re.compile(r"\b(discount|pricing|price|quote|margin)\b", re.I),
    ],
    "internal-roadmap": [
        re.compile(r"\broadmap\b", re.I),
        re.compile(r"\bETA\b"),
        re.compile(r"\b(PE|PLT|PCP)-\d+\b"),
    ],
    # Competitive-POSITIONING language, not the name of a competitor: a customer's
    # current platform (OpenShift, Nutanix, VMware) is a fact that belongs in
    # Salesforce -- Secondary_Environment_s__c even lists "Nutanix AHV" as a valid
    # value. What does NOT belong is internal battle-plan language.
    "internal-competitive": [
        re.compile(r"\b(battle ?cards?|displac(e|ed|ing)|"
                   r"competitive (play|positioning|takeout)|win against)\b", re.I),
    ],
}


def lint(text, allow=()):
    """Run every named rule not in `allow`; return the rule names that fired.

    A rule in `allow` is skipped outright — the override itself is recorded in the
    audit line by the caller, never applied silently."""
    return [name for name, patterns in LINT_RULES.items()
            if name not in allow and any(p.search(text) for p in patterns)]


# ── field: label/API resolution + type validation ────────────────────────────
PICKLISTS = {
    "SE_Forecast__c": ["Favorable", "Needs Attention", "At Risk"],
    "Tech_Risk_Status__c": ["\U0001f534 High", "\U0001f7e2 Low"],
    "Hands_on_Eval_By__c": ["Customer", "Partner", "Spectro Cloud"],
    # Plain High|Low. The describe also lists no Medium on this field, unlike its
    # emoji twin Tech_Risk_Status__c whose value set carries an inactive Medium.
    "Technical_Risk__c": ["High", "Low"],
}
MULTIPICKLISTS = {
    "Secondary_Environment_s__c": ["AWS", "Azure", "GCP", "Nutanix AHV", "OpenStack",
                                    "vCloud Director", "VMware", "Bare Metal", "Other"],
}
BOOL_TRUE, BOOL_FALSE = ("true", "1", "yes"), ("false", "0", "no")


def _resolve_field(ref):
    """label or API name -> (api, kind). Refuses (E_REFUSED) a never/dead field by
    name, and (E_USAGE) anything not in `opp-axi fields write` at all."""
    ref_l = ref.strip().lower()
    for label, api, kind, _values in cli.FIELDS["write"]:
        if ref_l in (label.lower(), api.lower()):
            return api, kind
    for label, api, why in cli.FIELDS["never"]:
        if ref_l in (label.lower(), api.lower()):
            cli.die(f"refusing '{ref}' ({api}): {why} — SE never writes this field",
                     cli.E_REFUSED)
    for label, api, why in cli.FIELDS["dead"]:
        if ref_l in (label.lower(), api.lower()):
            cli.die(f"refusing '{ref}' ({api}): {why}", cli.E_REFUSED)
    for label, api, _section in cli.FIELDS["rep"]:
        if ref_l in (label.lower(), api.lower()):
            cli.die(f"refusing '{ref}' ({api}): rep-owned — read it with "
                     f"`opp-axi rep`, never write it", cli.E_REFUSED)
    cli.die(f"unknown field '{ref}' — see opp-axi fields write", cli.E_USAGE)


def field_label(ref):
    """API name (or label) -> the friendly label a human reads. Unknown -> itself.

    `_resolve_field()` already walks these rows label-first to answer "what do I
    PATCH"; this walks the same rows to answer "what do I call it on screen".
    Every table, not just `write`: the audit log is history, and a field that has
    since moved to `never`/`dead`/`rep` still has a write recorded under it.

    Callers that RENDER a write -- the console's Salesforce writes card is the
    one today -- must ask for this rather than keep their own table. A second
    copy drifts silently the first time a field is added here, and the drift
    shows up as a raw `Hands_on_Eval_POV_URL__c` on Craig's board.
    """
    ref_l = (ref or "").strip().lower()
    for table in ("write", "never", "dead", "rep"):
        for row in cli.FIELDS.get(table, ()):
            label, api = row[0], row[1]
            if ref_l in (label.lower(), api.lower()):
                return label
    return ref or ""


def _validate(api, kind, raw):
    """Coerce + validate a CLI string for `api` per its declared type. Anything that
    fails is a usage error (E_USAGE) — the guard exists to stop a bad VALUE, not to
    second-guess a well-formed one."""
    if kind == "boolean":
        v = raw.strip().lower()
        if v in BOOL_TRUE:
            return True
        if v in BOOL_FALSE:
            return False
        cli.die(f"{api}: '{raw}' is not a boolean (true|false)", cli.E_USAGE)
    if kind == "date":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            cli.die(f"{api}: '{raw}' is not YYYY-MM-DD", cli.E_USAGE)
        return raw
    if kind == "picklist":
        allowed = PICKLISTS.get(api)
        if allowed and raw not in allowed:
            cli.die(f"{api}: '{raw}' is not one of {allowed}", cli.E_USAGE)
        return raw
    if kind == "multipicklist":
        allowed = MULTIPICKLISTS.get(api)
        if allowed:
            bad = [v for v in (s.strip() for s in raw.split(";")) if v and v not in allowed]
            if bad:
                cli.die(f"{api}: {bad} not in {allowed}", cli.E_USAGE)
        return raw
    return raw   # textarea / freeform


def cmd_field(a):
    """opp-axi field <opp> <Field>=<value> [...] — write one or more SE-owned
    fields. Renamed from the spec's `set` because a global shell guard blocks the
    bare word `set` as a command."""
    idx = cli.opp_index()
    slug, oid = cli.resolve(a.ref, idx)

    pairs = []
    for kv in a.fields:
        if "=" not in kv:
            cli.die(f"'{kv}' is not Field=value", cli.E_USAGE)
        ref, _, raw = kv.partition("=")
        api, kind = _resolve_field(ref)
        pairs.append((api, kind, _validate(api, kind, raw)))

    cols = ",".join(sorted({api for api, _, _ in pairs} | {"SA_Assignment_Oppty__c"}))
    recs = cli.sf_query(f"SELECT Id,{cols} FROM Opportunity WHERE Id = '{oid}'")
    if not recs:
        cli.die(f"opp {oid} not found", cli.E_NOTFOUND)
    r = recs[0]
    owner = r.get("SA_Assignment_Oppty__c") or ""
    if owner != cli.ME:
        cli.die(f"refusing write: SE on {slug} is '{owner}', not {cli.ME}. "
                 f"Secondary-SE assignment is not write access — keep context in OPP.md.",
                 cli.E_REFUSED)

    rows = []
    for api, _kind, value in pairs:
        current = r.get(api)
        if a.if_empty and not _same(current, ""):
            rows.append({"field": api, "old": current, "new": value,
                         "status": "skipped-not-empty", "id": ""})
            continue
        rec = guarded_patch(oid, slug, api, value, current,
                             reason=a.reason or "field write", dry_run=a.dry_run)
        rows.append({"field": api, "old": rec["old"], "new": rec["new"],
                     "status": "dry-run" if a.dry_run else rec["status"],
                     "id": "" if a.dry_run else rec["id"]})

    cli.emit(f"field {slug} {oid}",
             cli.toon("write", ["field", "old", "new", "status", "id"], rows),
             cli.nxt(f"opp-axi opp {slug}", "opp-axi writes"))
    if any(row["status"] == "mismatch" for row in rows):
        sys.exit(cli.E_ERR)


# ── undo ───────────────────────────────────────────────────────────────────
def cmd_undo(a):
    by_id = _latest_by_id(_load_writes())
    rec = by_id.get(a.write_id)
    if not rec:
        cli.die(f"no write '{a.write_id}' in the audit log", cli.E_NOTFOUND)
    if rec.get("status") != "verified":
        cli.die(f"write {a.write_id} is '{rec.get('status')}', not verified — refusing to undo",
                 cli.E_REFUSED)
    current = _read_field(rec["opp"], rec["field"])
    if not _same(current, rec["new"], rec["field"]):
        cli.die(f"{rec['field']} on {rec['slug']} no longer matches write {a.write_id} — "
                 f"refusing undo.\n  recorded: {cli.first_line(rec['new'])}\n"
                 f"  current:  {cli.first_line(current)}", cli.E_REFUSED)

    new_rec = guarded_patch(rec["opp"], rec["slug"], rec["field"], rec["old"], current,
                            reason=f"undo {a.write_id}", dry_run=a.dry_run)
    status = "dry-run" if a.dry_run else new_rec["status"]
    cli.emit(cli.toon("undo", ["of", "opp", "slug", "field", "status", "id"],
                      [{"of": a.write_id, "opp": rec["opp"][:15], "slug": rec["slug"],
                        "field": rec["field"], "status": status,
                        "id": "" if a.dry_run else new_rec["id"]}]),
             cli.nxt(f"opp-axi opp {rec['slug']}"))
    if status == "mismatch":
        sys.exit(cli.E_ERR)


# ── writes: the audit-log listing ────────────────────────────────────────────
def _parse_mdy(s):
    mo, d, y = (int(x) for x in s.split("/"))
    return datetime(y + 2000 if y < 100 else y, mo, d)


def cmd_writes(a):
    # --full carries whole field values -- an SE Activity entry measured 2,335
    # characters, newlines and all. TOON is a table; a cell like that does not
    # survive it. Refusing beats quietly first_line()-ing the value, which is
    # the exact truncation --full exists to undo.
    if getattr(a, "full", False) and not getattr(a, "json", False):
        cli.die("writes --full returns whole multi-line field values, which TOON "
                "cannot hold — use: opp-axi writes --full --json", cli.E_USAGE)
    latest = list(_latest_by_id(_load_writes()).values())
    if a.since:
        cutoff = _parse_mdy(a.since)   # naive, local calendar date at 00:00
        latest = [r for r in latest
                  if datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
                  .astimezone().replace(tzinfo=None) >= cutoff]
    if a.opp:
        latest = [r for r in latest if r.get("slug") == a.opp]
    latest.sort(key=lambda r: r.get("id", ""))
    if getattr(a, "full", False):
        # Everything a caller needs to SHOW the write rather than list it: the
        # friendly label, the whole new value, and the `old` the ledger has been
        # recording since the guard's first line (guarded_patch stamps both).
        # "The full update" is a diff question and the answer was already on disk.
        rows = [{"id": r.get("id"), "at": r.get("at"), "slug": r.get("slug"),
                 "field": r.get("field"), "label": field_label(r.get("field")),
                 "old": r.get("old"), "new": r.get("new"),
                 "status": r.get("status")} for r in latest]
    else:
        rows = [{"id": r.get("id"), "at": r.get("at"), "slug": r.get("slug"),
                 "field": r.get("field"), "new": cli.first_line(r.get("new")),
                 "status": r.get("status")} for r in latest]

    if getattr(a, "json", False):
        print(json.dumps(rows, indent=2))
        return
    cli.emit(cli.toon("writes", ["id", "at", "slug", "field", "new", "status"], rows),
             cli.nxt("opp-axi undo <write-id>"))
