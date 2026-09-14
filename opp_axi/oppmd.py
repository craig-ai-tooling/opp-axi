"""oppmd — a budgeted slice of an OPP.md, and a way to append to one without reading it.

9/14/26 (Opp Ops Redesign, problem 4, option A): sessions stop reading whole OPP.md files.
70 notes average ~250k tokens total, the biggest single files ~20k tokens each — that is
context spent before a task has asked a single question. `brief()` reads the frontmatter,
a handful of sections and only the recent Log, and caps the result at a token budget.
`insert_log_entry()` appends a dated entry with a targeted text edit, never a full read.

Pure text/markdown processing, deliberately independent of cli.py's Salesforce/Google
plumbing — it knows nothing about REPO, opp_index() or resolve(). cli.py's cmd_brief and
cmd_log resolve a ref to a note path and hand off; everything below operates on that path
(or on parsed text) alone, which is what keeps this fixture-testable without a live repo.

Log shape, verified across the real notes: entries are top-level (unindented) bullets that
open with a date, either bare (`- 2026-08-26: **Headline.** ...`) or bold-wrapped, optionally
with a parenthetical (`- **2026-09-14 (lab validation)** — ...`), or M/D/YY-led. Everything
indented underneath — sub-bullets, tables, wrapped continuation lines — belongs to that entry
until the next dated top-level bullet. Some Logs open with an HTML comment before the first
entry; that comment is never itself an entry.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import date, timedelta

FRONTMATTER_KEYS = ("opp_name", "stage", "amount", "close_date", "sf_opp_id")
DEFAULT_SECTIONS = ("Snapshot", "Decisions", "Risks")
DEFAULT_WINDOW_DAYS = 30
DEFAULT_BUDGET_TOKENS = 1500


class OppMdError(Exception):
    """Base error for oppmd operations."""


class NoLogSection(OppMdError):
    """The note has no '## Log' heading to insert below."""


class BadDate(OppMdError):
    """An unrecognized --since/--date value."""


class BadRegex(OppMdError):
    """An invalid --grep pattern."""


# ── date parsing ─────────────────────────────────────────────────────────────
_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_MDY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _parse_date_str(s):
    """'YYYY-MM-DD' or 'M/D/YY[YY]' -> date, or None if unrecognized. Anchored: `s` must
    itself start with the date (use _find_date_token first to pull one out of a bigger
    string)."""
    s = (s or "").strip()
    m = _ISO_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = _MDY_RE.match(s)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
        if y < 100:
            y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _find_date_token(s):
    m = _ISO_RE.search(s or "") or _MDY_RE.search(s or "")
    return m.group(0) if m else None


# ── frontmatter ──────────────────────────────────────────────────────────────
def _parse_frontmatter(text):
    """The `---`-delimited header: simple `key: value` lines. `#`-led lines and a line
    whose value is only a trailing comment (the never-filled-in template placeholder) are
    not real values."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = {}
    for ln in lines[1:end]:
        s = ln.strip()
        if not s or s.startswith("#") or ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        key = key.strip()
        val = val.split(" #", 1)[0].strip()
        if val.startswith("#"):
            val = ""
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1]
        fm[key] = val
    return fm, "\n".join(lines[end + 1:])


# ── sections ─────────────────────────────────────────────────────────────────
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)


def _parse_sections(text):
    """Every `## ` heading -> the text up to the next one. Order is insertion order,
    which is the file's own order — Python dicts keep it, no separate list needed."""
    sections = {}
    heads = list(_HEADING.finditer(text))
    for i, m in enumerate(heads):
        name = m.group(1).strip()
        start = m.end()
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections[name] = text[start:stop].strip("\n")
    return sections


def find_section(sections, name):
    """Case-insensitive prefix match: 'Risks' matches 'Risks / blockers' or
    'Risks / watch-items'; 'Decisions' matches 'Decisions & commitments'. First hit in
    the file's own order wins."""
    want = name.strip().lower()
    for actual in sections:
        if actual.strip().lower().startswith(want):
            return actual
    return None


# ── Log entries ────────────────────────────────────────────────────────────
_DATE = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})"
ENTRY_START = re.compile(rf"^-\s+\**\s*{_DATE}\b")
_PREFIX_BOLD = re.compile(rf"^-\s+\*\*\s*{_DATE}(?:\s*\([^)]*\))?\s*\*\*", re.DOTALL)
_PREFIX_BARE = re.compile(rf"^-\s+{_DATE}")
_SEP_AFTER_DATE = re.compile(r"^\s*[:\-–—]*\s*")
_HTML_COMMENT_LEAD = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)
_BOLD_LEAD = re.compile(r"\A\s*\*\*(.+?)\*\*", re.DOTALL)


def _split_date_prefix(entry_raw):
    """Strip the leading '- [**]DATE[ (paren)][**][ :-—]' off one entry, bold or bare.
    Returns (matched_prefix, remainder) — remainder is what headline extraction runs on."""
    m = _PREFIX_BOLD.match(entry_raw) or _PREFIX_BARE.match(entry_raw)
    if not m:
        return None, entry_raw
    prefix = m.group(0)
    rest = entry_raw[m.end():]
    sep = _SEP_AFTER_DATE.match(rest)
    if sep:
        rest = rest[sep.end():]
    return prefix, rest


def _headline(body_text, cap=200):
    """The bold LEAD (immediately after the date, not just any bold text later in a long
    entry) if present, else the first sentence. Wrapped lines inside a bold span (long
    headlines wrap at ~100 chars in the source) are whitespace-normalized into one line."""
    m = _BOLD_LEAD.match(body_text)
    if m:
        text = m.group(1)
    else:
        norm = re.sub(r"\s+", " ", body_text).strip()
        m2 = re.match(r"(.*?[.!?])(\s|$)", norm)
        text = m2.group(1) if m2 else norm
    return re.sub(r"\s+", " ", text).strip()[:cap]


def parse_log_entries(text):
    """A '## Log' section body -> [{date, headline, body, raw}], in the file's own order
    (newest-first, by convention — this does not re-sort). `date` is a `datetime.date` or
    None when a top-level bullet could not be dated. A leading HTML comment is skipped,
    never treated as an entry."""
    body = _HTML_COMMENT_LEAD.sub("", text, count=1)
    lines = body.split("\n")
    entries = []
    cur = []

    def flush():
        if not cur:
            return
        raw = "\n".join(cur).strip("\n")
        if not raw.strip():
            return
        prefix, rest = _split_date_prefix(raw)
        d = _parse_date_str(_find_date_token(prefix)) if prefix else None
        body_text = rest.strip() if prefix else raw.strip()
        entries.append({"date": d, "headline": _headline(body_text),
                        "body": body_text, "raw": raw})

    for ln in lines:
        if ENTRY_START.match(ln):
            flush()
            cur = [ln]
        elif cur:
            cur.append(ln)
    flush()
    return entries


def parse(text):
    """Parse a whole OPP.md: (frontmatter dict, ordered {section name: body} dict,
    Log entries [{date, headline, body, raw}]). The Log section, if present, is parsed
    into entries automatically — callers do not need to find it themselves."""
    frontmatter, rest = _parse_frontmatter(text)
    sections = _parse_sections(rest)
    log_key = find_section(sections, "Log")
    entries = parse_log_entries(sections[log_key]) if log_key else []
    return frontmatter, sections, entries


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── brief ──────────────────────────────────────────────────────────────────
# Budget priority, most protected first: frontmatter (never cut) -> Log headlines
# (floor: the 10 newest in-window, or all if fewer — the most valuable thing in a
# brief) -> Snapshot (floor: its first paragraph/bullet) -> Decisions -> Risks ->
# any extra --section. Trimming spends in the opposite order: extra sections first,
# then Risks, then Decisions, then Snapshot down to its floor, and only THEN log
# headlines, oldest first, down to their floor. Sections are cut at whole
# paragraph/bullet boundaries — never mid-word — and a dated bullet list (Decisions)
# drops its oldest bullets first, keeping the newest. Floors are a preference, not a
# license to blow the budget: if a budget is small enough that every floor is spent
# and it still does not fit, the last resort is Snapshot's protected paragraph, then
# the headline floor itself — the hard byte cap always wins in the end.
def _cut_note(cut_logs, cut_bytes, budget):
    bits = []
    if cut_logs:
        bits.append(f"{cut_logs} older log line(s)")
    if cut_bytes:
        bits.append(f"{cut_bytes}B of section text")
    if not bits:
        return ""
    return f"[budget {budget}tok: cut {' + '.join(bits)} — see it all with --full]"


def _nxt(slug):
    return ("next: opp-axi brief " + slug + " --since <date> | "
            "opp-axi brief " + slug + " --grep <regex> | "
            "opp-axi brief " + slug + " --full")


def _section_priority(requested_name):
    """0 = Snapshot-tier (most protected), 1 = Decisions-tier, 2 = Risks-tier, 3 =
    anything else asked for via --section (least protected, cut first). Matches on
    the name the caller asked for, not the file's own (possibly longer) heading."""
    low = requested_name.strip().lower()
    if low.startswith("snapshot"):
        return 0
    if low.startswith("decision"):
        return 1
    if low.startswith("risk"):
        return 2
    return 3


def _split_blocks(body):
    """A section body -> ('bullets'|'paragraphs', [block, ...]) in file order. A block
    is one top-level '- ' bullet (plus any indented continuation) if bullets make up
    at least half the section's non-blank lines, else one blank-line-separated
    paragraph. Trimming only ever drops a whole block, so a cut never lands mid-word."""
    lines = body.split("\n")
    nonblank = [ln for ln in lines if ln.strip()]
    dash_top = sum(1 for ln in lines if ln.startswith("- "))
    if dash_top and dash_top >= max(1, len(nonblank) // 2):
        blocks, cur = [], []
        for ln in lines:
            if ln.startswith("- "):
                if cur:
                    blocks.append("\n".join(cur).rstrip())
                cur = [ln]
            else:
                cur.append(ln)
        if cur:
            blocks.append("\n".join(cur).rstrip())
        return "bullets", [b for b in blocks if b.strip()]
    paras = [p.strip("\n") for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "paragraphs", paras


def _block_date(block):
    token = _find_date_token(block[:60])
    return _parse_date_str(token) if token else None


def _drop_order(blocks):
    """Indices in the order a section's blocks should be dropped: dated ones
    oldest-first (so the newest survive — this is Decisions' "keep the newest
    bullets" rule), undated ones only after every dated one is gone. A section with
    no dates at all (plain bullets like Risks) falls back to dropping from the end,
    the general rule for prose."""
    dates = [_block_date(b) for b in blocks]
    if any(d is not None for d in dates):
        dated = sorted((i for i in range(len(blocks)) if dates[i] is not None),
                       key=lambda i: dates[i])
        undated = list(reversed([i for i in range(len(blocks)) if dates[i] is None]))
        return dated + undated
    return list(reversed(range(len(blocks))))


def _section_marker(name, kind, cut, total, budget):
    unit = "bullets" if kind == "bullets" else "paragraphs"
    return (f'[{name}: {cut} of {total} {unit} cut — --section "{name}" '
            f"--budget {budget * 3} or --full]")


def _section_state(requested_name, actual_name, body):
    kind, blocks = _split_blocks(body)
    prio = _section_priority(requested_name)
    # Snapshot-tier keeps a floor of one block (its lead paragraph/bullet): the drop
    # order simply never includes index 0. Every other tier can be cut to nothing.
    order = list(reversed(range(1, len(blocks)))) if prio == 0 else _drop_order(blocks)
    return {"name": actual_name, "kind": kind, "blocks": blocks, "priority": prio,
            "drop_order": order, "ptr": 0, "removed": set(), "cut_count": 0}


def _next_cuttable(states, tier_order):
    """The next section (by cut priority) that still has a block it is willing to
    give up, or None once every section has hit its floor."""
    for i in tier_order:
        if states[i]["ptr"] < len(states[i]["drop_order"]):
            return i
    return None


def brief(path, slug, *, since=None, grep=None, sections=None, budget=DEFAULT_BUDGET_TOKENS,
          full=False, as_json=False):
    """A budgeted slice of `path`'s content. `slug` is display-only (used in nxt hints and
    --json's `opp` field) — this function never touches the repo index.

    --full short-circuits everything else and returns the file unchanged, per spec.
    """
    raw = _read(path)
    if full:
        return raw.rstrip("\n")

    frontmatter, sects, entries = parse(raw)

    if since:
        since_date = _parse_date_str(since)
        if since_date is None:
            raise BadDate(f"unrecognized --since date: {since}")
    else:
        since_date = date.today() - timedelta(days=DEFAULT_WINDOW_DAYS)

    requested = sections or list(DEFAULT_SECTIONS)
    picked = []
    for name in requested:
        actual = find_section(sects, name)
        # The Log section has its own dedicated, budgeted rendering below — never double
        # it up as a plain section even if someone passes --section Log.
        if actual and not actual.strip().lower().startswith("log"):
            picked.append((name, actual, sects[actual].strip()))

    windowed = sorted((e for e in entries if e["date"] and e["date"] >= since_date),
                      key=lambda e: e["date"], reverse=True)

    grep_re = None
    if grep:
        try:
            grep_re = re.compile(grep, re.I)
        except re.error as e:
            raise BadRegex(f"bad --grep pattern: {e}") from e

    has_log = find_section(sects, "Log") is not None
    if grep_re:
        matches = [e for e in windowed if grep_re.search(e["raw"])]
        log_items = [e["raw"].strip() for e in matches]
        log_label = f"## Log matches /{grep}/ (since {since_date}): {len(matches)} of {len(windowed)} in-window"
    else:
        matches = windowed
        log_items = [f"{e['date'].isoformat()} {e['headline']}" for e in windowed]
        log_label = f"## Log (since {since_date}): {len(windowed)} of {len(entries)} total"

    if as_json:
        return json.dumps({
            "opp": slug,
            "frontmatter": {k: frontmatter[k] for k in FRONTMATTER_KEYS if frontmatter.get(k)},
            "sections": {actual: body for _req, actual, body in picked},
            "since": since_date.isoformat(),
            "grep": grep,
            "log_entries": [{"date": e["date"].isoformat() if e["date"] else None,
                             "headline": e["headline"], "body": e["body"]} for e in matches],
        }, indent=2)

    header_block = "\n".join(f"{k}: {frontmatter[k]}" for k in FRONTMATTER_KEYS if frontmatter.get(k))
    states = [_section_state(req, actual, body) for req, actual, body in picked]
    # Cut order: highest priority number first (3=extra, 2=Risks, 1=Decisions,
    # 0=Snapshot last) — the exact reverse of how protected each tier is.
    tier_order = sorted(range(len(states)), key=lambda i: -states[i]["priority"])
    # The headline floor only protects plain headline rendering — a --grep hit list
    # is not "the most valuable thing in a brief" in the same sense, and a full-body
    # match can be large enough that a hard floor on count would blow the budget.
    headline_floor = 0 if grep_re else min(10, len(windowed))

    def compose(states, items, cut_line):
        parts = [header_block] if header_block else []
        for st in states:
            kept = [st["blocks"][j] for j in range(len(st["blocks"])) if j not in st["removed"]]
            if not kept and not st["cut_count"]:
                continue
            joiner = "\n" if st["kind"] == "bullets" else "\n\n"
            body_text = joiner.join(kept)
            if st["cut_count"]:
                marker = _section_marker(st["name"], st["kind"], st["cut_count"],
                                         len(st["blocks"]), budget)
                body_text = f"{body_text}\n{marker}" if body_text else marker
            if body_text:
                parts.append(f"## {st['name']}\n{body_text}")
        if has_log:
            joiner = "\n\n" if grep_re else "\n"
            block = log_label + (("\n" + joiner.join(items)) if items else "")
            parts.append(block)
        if cut_line:
            parts.append(cut_line)
        parts.append(_nxt(slug))
        return "\n\n".join(p for p in parts if p)

    budget_bytes = max(budget, 0) * 4
    items = list(log_items)
    cut_logs = cut_bytes = 0
    text = compose(states, items, "")
    guard = 0
    while len(text.encode("utf-8")) > budget_bytes and guard < 2000:
        guard += 1
        sec_i = _next_cuttable(states, tier_order)
        if sec_i is not None:
            st = states[sec_i]
            idx = st["drop_order"][st["ptr"]]
            st["removed"].add(idx)
            st["ptr"] += 1
            st["cut_count"] += 1
            cut_bytes += len(st["blocks"][idx].encode("utf-8"))
        elif len(items) > headline_floor:
            items.pop()               # list is newest-first: pop() drops the oldest
            cut_logs += 1
        else:
            # Every floor is spent and it still does not fit — break them too, in the
            # same protected-last order, so the hard budget cap always wins.
            snap = next((s for s in states if s["priority"] == 0 and s["blocks"]
                        and 0 not in s["removed"]), None)
            if snap:
                snap["removed"].add(0)
                snap["cut_count"] += 1
                cut_bytes += len(snap["blocks"][0].encode("utf-8"))
            elif items:
                items.pop()
                cut_logs += 1
            else:
                break                  # nothing left anywhere to cut
        text = compose(states, items, _cut_note(cut_logs, cut_bytes, budget))
    return text


# ── log insert ───────────────────────────────────────────────────────────────
_LOG_HEADING = re.compile(r"^## Log[ \t]*\r?\n", re.M)
# No leading \A: this is matched with .match(raw, pos) at an arbitrary offset, and \A
# anchors to the start of the whole string regardless of `pos` — that silently broke
# comment detection for every note where the Log heading isn't at position 0.
_COMMENT_AFTER_HEADING = re.compile(r"(?:[ \t]*\r?\n)*[ \t]*<!--.*?-->[ \t]*\r?\n?", re.DOTALL)


def _existing_date_style(tail):
    """ISO or M/D/Y, matching whatever the note's newest existing entry uses. ISO when
    there is no existing entry to match, per spec."""
    body = _HTML_COMMENT_LEAD.sub("", tail, count=1)
    for ln in body.split("\n"):
        if ENTRY_START.match(ln):
            prefix, _rest = _split_date_prefix(ln)
            token = _find_date_token(prefix) if prefix else None
            return "mdy" if token and "/" in token else "iso"
    return "iso"


def _format_date(d, style):
    return f"{d.month}/{d.day}/{d.year % 100:02d}" if style == "mdy" else d.isoformat()


def _state_dir():
    base = (os.environ.get("OPP_AXI_STATE") or os.environ.get("XDG_STATE_HOME")
            or os.path.expanduser("~/.local/state"))
    d = os.path.join(base, "opp-axi", "locks")
    os.makedirs(d, exist_ok=True)
    return d


@contextlib.contextmanager
def _locked(slug):
    """Per-note flock, held outside the repo — 'beside nothing' in the spec's words, so a
    lock file is never mistaken for note content or picked up by git status."""
    path = os.path.join(_state_dir(), f"oppmd-{slug}.lock")
    fd = open(path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def _atomic_write(path, content):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".oppmd-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def insert_log_entry(path, slug, text, date_str=None):
    """Prepend one dated entry directly under the '## Log' heading (and below a leading
    HTML comment, if present), above the newest existing entry — a targeted text edit,
    never a full read. Locked per-note and written atomically (temp file + os.replace),
    so a concurrent insert or a crash mid-write can't corrupt the note.

    Raises NoLogSection if `path` has no '## Log' heading; raises BadDate for an
    unparsable --date. Returns the inserted line (no trailing newline).
    """
    d = _parse_date_str(date_str) if date_str else date.today()
    if d is None:
        raise BadDate(f"unrecognized --date value: {date_str}")

    with _locked(slug):
        raw = _read(path)
        m = _LOG_HEADING.search(raw)
        if not m:
            raise NoLogSection(path)
        pos = m.end()
        style = _existing_date_style(raw[pos:])
        cm = _COMMENT_AFTER_HEADING.match(raw, pos)
        if cm:
            pos = cm.end()
        line = f"- {_format_date(d, style)}: {text.strip()}"
        new_raw = raw[:pos] + line + "\n" + raw[pos:]
        _atomic_write(path, new_raw)
    return line
