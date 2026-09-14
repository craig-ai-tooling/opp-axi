"""opp_axi/rep.py — rep/AE-entered Salesforce Opportunity data. READ-ONLY.

opp-axi's other verbs (`opp`, `field`, `activity`) read and write the handful of
Opportunity fields the SE owns. Everything else on the Opportunity — Stage, Amount,
Close Date, Next Steps, MEDDPICC, qualification, narrative fields, rep call logs,
Lightning Notes, field history, contact roles — belongs to the account rep/AE. Craig
needs to READ that data to build an accurate status update; he must never write it.

Reads go through `cli.sf_query()` (SOQL) plus one plain GET per Lightning Note body
(`cli._run()` against .../ContentVersion/<id>/VersionData). This module never touches
opp_axi's write guard and issues no Salesforce write of any kind. `tests/test_rep.py`
statically forbids the write helpers in this file, and separately checks that every
field in `cli.FIELDS["rep"]` is refused by `field`.

Same module-import pattern as guard.py: `from opp_axi import cli` binds the module
object, and only function bodies touch its attributes, so cli.py -> rep.py ->
opp_axi (re-exporting cli) forms a working cycle instead of a broken one.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date, datetime, timedelta

from opp_axi import cli

# ── Next Steps parsing ───────────────────────────────────────────────────────
# Next_Steps__c is a free-text, newest-first dated log the AE maintains by hand.
# Real formats seen live: "9/14/2026 MB ...", "9/14/26 DF - ...", "2026-09-14: ...",
# "9-12-2026 - MN - ...", "8/17 MNDA signed- ..." (no year), "7.27.26 - Mesh - ..."
# (dotted, year REQUIRED -- a year-less dotted number like "1.5 million" must never be
# read as a date), CRLF line endings, blank lines, and inline run-ons where a second
# dated entry starts mid-line after 2+ spaces.
_LEAD_DATE = re.compile(
    r"^(?:"
    r"(?P<iso_y>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_d>\d{1,2})"
    r"|(?P<dot_m>\d{1,2})\.(?P<dot_d>\d{1,2})\.(?P<dot_y>\d{2,4})"
    r"|(?P<us_m>\d{1,2})[/-](?P<us_d>\d{1,2})(?:[/-](?P<us_y>\d{2,4}))?"
    r")"
)

# After the date: an optional leading separator (space/dash/colon), then an optional
# 2-3 CAPITAL-letter token (a candidate initials, bounded so "MNDA" never matches as
# "MN"), then an optional trailing separator, then the rest of the line as text.
_REST = re.compile(
    r"^(?P<lead_ws>\s*(?:[-:–]\s*)?)"
    r"(?:(?P<initials>[A-Z]{2,3})\b(?P<mid_sep>\s*(?:[-:–]\s*)?))?"
    r"(?P<text>.*)$"
)

# A second dated entry starting mid-line, after 2+ spaces, with a YEAR and a 2-3
# capital-letter token — the Tesla-style run-on. Split it into two lines before any
# other parsing; each side is then handled by the normal per-line logic above.
_INLINE_SPLIT = re.compile(
    r"(?<=\S)[ \t]{2,}(?=\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+[A-Z]{2,3}\b)"
)


def _lead_date_iso(m, today):
    """The regex match for a leading date -> 'YYYY-MM-DD', or None if not a real date."""
    if m.group("iso_y"):
        y, mo, d = int(m.group("iso_y")), int(m.group("iso_m")), int(m.group("iso_d"))
    elif m.group("dot_y"):
        mo, d, y = int(m.group("dot_m")), int(m.group("dot_d")), int(m.group("dot_y"))
        if y < 100:
            y += 2000
    else:
        mo, d = int(m.group("us_m")), int(m.group("us_d"))
        y_raw = m.group("us_y")
        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            # Year-less date (e.g. "8/17"): assume today's year, unless that lands
            # more than 30 days in the future -- then it was almost certainly last year.
            y = today.year
            try:
                if (date(y, mo, d) - today).days > 30:
                    y -= 1
            except ValueError:
                return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _split_dated_line(line, m, initials_set):
    """The remainder of a dated line -> (by, text). `by` is the captured initials
    token ONLY if it is a known initials (owner's or cli.INITIALS) -- otherwise the
    token was never an author (e.g. "SHI", a partner name, or "MNDA", which the regex
    itself never captures because it is 4 letters) and stays in the text untouched."""
    rest = line[m.end():]
    rm = _REST.match(rest)
    initials = rm.group("initials")
    if initials and initials.upper() in initials_set:
        return initials.upper(), rm.group("text").strip()
    lead_ws = rm.group("lead_ws") or ""
    return "", rest[len(lead_ws):].strip()


def parse_next_steps(text, owner_initials=(), today=None):
    """Next_Steps__c -> [{date, by, text}, ...], newest-first as written (the field's
    own order is never re-sorted). `date` is 'YYYY-MM-DD' or None. `by` is an initials
    token only when it is in `owner_initials` or `cli.INITIALS` -- any other 2-3
    capital-letter token (a partner name like "SHI") is never treated as an author.
    Undated leading text becomes an entry with date=None rather than being dropped;
    later non-dated lines are continuations of whatever entry precedes them."""
    if not text:
        return []
    today = today or datetime.now().date()
    initials_set = {i.upper() for i in owner_initials if i} | {cli.INITIALS.upper()}

    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    norm = _INLINE_SPLIT.sub("\n", norm)

    entries = []
    for raw_line in norm.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = _LEAD_DATE.match(line)
        if m:
            d = _lead_date_iso(m, today)
            by, txt = _split_dated_line(line, m, initials_set)
            entries.append({"date": d, "by": by, "text": txt})
        elif entries:
            entries[-1]["text"] = (entries[-1]["text"] + " " + line).strip()
        else:
            entries.append({"date": None, "by": "", "text": line})
    return entries


# ── Clari call summaries ─────────────────────────────────────────────────────
_CLARI_HEADER = re.compile(r"Smart Summary(?:\s*\([^)]*\))?\s*:\s*", re.I)
_TAG = re.compile(r"<[^>]+>")


def clari_summary(description):
    """A rep Task's Description: text after the Clari Copilot 'Smart Summary (...)  :'
    header if present, else the whole description. Whitespace always collapsed."""
    text = description or ""
    m = _CLARI_HEADER.search(text)
    if m:
        text = text[m.end():]
    return " ".join(text.split())


def _strip_html(text):
    """A Lightning Note's VersionData body is often HTML. Strip tags, unescape
    entities, collapse whitespace -- never render markup as if it were the note."""
    return " ".join(html_lib.unescape(_TAG.sub(" ", text or "")).split())


# ── small shared helpers ─────────────────────────────────────────────────────
def _rel_name(r, key, field="Name"):
    return (r.get(key) or {}).get(field) or ""


def _iso_date(s):
    """A SF date/datetime string -> its date portion, or None."""
    return s[:10] if s else None


def _mdy(iso):
    """'YYYY-MM-DD' (or a longer SF datetime string) -> 'M/D/YY'. Empty in, empty out --
    an unknown date must never render as today's date."""
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return iso
    return d.strftime("%-m/%-d/%y")


def _owner_initials_of(owner_name):
    """'Matthew Byram' -> 'MB'. First letter of each word in the owner's name."""
    words = re.findall(r"[A-Za-z']+", owner_name or "")
    return "".join(w[0].upper() for w in words if w)


def _parse_since(s):
    """YYYY-MM-DD or M/D/YY -> a date. Dies E_USAGE on anything else."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    cli.die(f"--since: '{s}' is not YYYY-MM-DD or M/D/YY", cli.E_USAGE)


def _days_stale(iso_date, today):
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (today - d).days


def _windowed(rows, since_date, full, default_n):
    """Shared show/limit rule for nextSteps, activity, notes and changes:
    default -> latest `default_n`; --since -> every row on/after it, no count cap
    (undated rows dropped, since they cannot be judged against a cutoff); --full ->
    every row, uncapped. Returns (shown, total) where total is every row, regardless
    of the window, so a caller always sees how much more there is."""
    total = len(rows)
    if since_date:
        since_iso = since_date.isoformat()
        pool = [r for r in rows if r.get("date") and r["date"] >= since_iso]
    else:
        pool = rows
    shown = pool if (since_date or full) else pool[:default_n]
    return shown, total


NUDGE_RE = re.compile(r"^(AE Nudge\b|Update Next Steps:)")


# ── field tables (label, api) per section, mirroring cli.FIELDS["rep"] ───────
MEDDPICC_ROWS = [
    ("Metrics", "Metrics__c", None),
    ("Economic Buyer", "Economic_Buyer__c", "Economic_Buyer_Status__c"),
    ("Decision Criteria", "Decision_Criteria__c", None),
    ("Decision Process", "Decision_Process__c", None),
    ("Paper Process", "Paper_Process__c", None),
    ("Identified Pain", "Identified_Pain__c", None),
    ("Champion", "Champion__c", "Champion_Status__c"),
    ("Last Time Testing Champion", "Last_Time_Testing_Champion__c", None),
    ("Competition", "Competition__c", None),
    ("Coach", "Coach__c", "Coach_Status__c"),
    ("Compelling Event", "Compelling_Event__c", None),
]

QUALIFICATION_ROWS = [
    ("Why Anything", "Why_Anything__c"),
    ("Why Now", "Why_Now__c"),
    ("Why Us", "Why_Us__c"),
    ("Budget Description", "Budget_Description__c"),
    ("Authority Description", "Authority_Description__c"),
    ("Need Description", "Need_Description__c"),
    ("Timeline Description", "Timeline_Description__c"),
]

NARRATIVE_ROWS = [
    ("Description", "Description"),
    ("Use Case Notes", "Use_Case_Notes__c"),
    ("Executive Summary Narrative", "Executive_Summary_Narrative__c"),
    ("Account Plan Narrative", "Account_Plan_Narrative__c"),
    ("Mutual Action Plan Narrative", "Mutual_Action_Plan_Narrative__c"),
    ("Value Hypothesis Narrative", "Value_Hypothesis_Narrative__c"),
    ("Channel Notes", "Channel_Notes__c"),
    ("Current Technical State", "Current_Technical_State__c"),
    ("Desired Technical State", "Desired_Technical_State__c"),
]

LINKS_ROWS = [
    ("Executive Summary", "Executive_Summary__c"),
    ("Account Plan", "Account_Plan__c"),
    ("Mutual Action Plan", "EW_Mutual_Action_Plan__c"),
    ("Value Hypothesis", "Value_Hypothesis__c"),
]

# Days_Since_Next_Steps_Update__c is deliberately NOT selected here -- it is a
# Salesforce formula field that can disagree with a same-day local calculation by a
# day depending on time-of-day. `daysStale` below is computed locally from
# Next_Steps_Last_Updated__c, the same way the rollup computes it, so the two views
# never disagree. (It stays in cli.FIELDS["rep"] as a documented, readable field.)
DEAL_FIELDS = [
    "StageName", "Amount", "CloseDate", "ForecastCategoryName", "Forecast_Status__c",
    "Probability__c", "Confidence__c", "Deal_Qualification_Health__c",
    "Next_Steps_Last_Updated__c",
    "Account_Executive__c", "Use_Case_Primary__c", "LeadSource",
    "Partner__r.Name", "SDR_of_Record__r.Name",
]

DEFAULT_SECTIONS = ("deal", "nextSteps", "meddpicc", "qualification", "narrative",
                    "activity", "notes", "files", "changes", "contacts")


# ── detail: opp-axi rep <ref> ─────────────────────────────────────────────────
def cmd_rep(a):
    if not a.ref:
        return _rollup(a)

    idx = cli.opp_index()
    slug, oid = cli.resolve(a.ref, idx)
    wanted = set(a.section) if a.section else set(DEFAULT_SECTIONS)
    since = _parse_since(a.since)
    today = datetime.now().date()

    fields = ["Id", "Name", "Owner.Name", "SA_Assignment_Oppty__c"]
    if "deal" in wanted:
        fields += DEAL_FIELDS
    if "nextSteps" in wanted:
        fields.append("Next_Steps__c")
    if "meddpicc" in wanted:
        for _l, api, status_api in MEDDPICC_ROWS:
            fields.append(api)
            if status_api:
                fields.append(status_api)
    if "qualification" in wanted:
        fields += [api for _l, api in QUALIFICATION_ROWS]
    if "narrative" in wanted:
        fields += [api for _l, api in NARRATIVE_ROWS] + [api for _l, api in LINKS_ROWS]
    fields = list(dict.fromkeys(fields))   # dedupe, keep order (SOQL rejects a dup column)

    recs = cli.sf_query(f"SELECT {','.join(fields)} FROM Opportunity WHERE Id = '{oid}'")
    if not recs:
        cli.die(f"opp {oid} not found in {cli.ORG}", cli.E_NOTFOUND)
    r = recs[0]
    owner = _rel_name(r, "Owner")
    se = r.get("SA_Assignment_Oppty__c") or ""
    owner_initials = (_owner_initials_of(owner),) if owner else ()
    id15 = r["Id"][:15]

    # Other OPEN opps under the same repo dir -- several accounts (tesla, toyota,
    # aunalytics) carry more than one, so a bare slug alone is ambiguous. Sourced from
    # idx only (already loaded above); no extra query.
    siblings = []
    if slug in idx:
        for o in idx[slug].get("opportunities", []) or []:
            sib_id = (o.get("id") or "")[:15]
            if not sib_id or sib_id == id15:
                continue
            if (o.get("status") or "open") != "open":
                continue
            siblings.append({"id": sib_id, "name": o.get("name") or ""})

    blocks = [f"rep id={id15} slug={slug} owner={owner} se={se} "
              "source=salesforce (read-only)",
              f"name: {r.get('Name')}"]
    for sib in siblings:
        blocks.append(f"also open under {slug}: {sib['id']} {sib['name']} — "
                      f"opp-axi rep {sib['id']}")
    j = {"opp": {"id": id15, "slug": slug, "name": r.get("Name"),
                "owner": owner, "se": se, "siblings": siblings}}

    if "deal" in wanted:
        deal = {
            "owner": owner, "ae": r.get("Account_Executive__c") or "",
            "stage": r.get("StageName"), "amt": cli.money(r.get("Amount")),
            "close": r.get("CloseDate"), "forecastCat": r.get("ForecastCategoryName"),
            "forecast": r.get("Forecast_Status__c"), "prob": r.get("Probability__c"),
            "confidence": r.get("Confidence__c"),
            "health": r.get("Deal_Qualification_Health__c"),
            "partner": _rel_name(r, "Partner__r"), "isr": _rel_name(r, "SDR_of_Record__r"),
            "useCase": r.get("Use_Case_Primary__c"),
            "nextStepsUpdated": r.get("Next_Steps_Last_Updated__c"),
            "daysStale": _days_stale(_iso_date(r.get("Next_Steps_Last_Updated__c")), today),
        }
        blocks.append(cli.toon("deal", list(deal.keys()), [{
            **deal, "close": _mdy(deal["close"]), "nextStepsUpdated": _mdy(deal["nextStepsUpdated"]),
        }]))
        j["deal"] = deal

    if "nextSteps" in wanted:
        entries = parse_next_steps(r.get("Next_Steps__c") or "", owner_initials, today)
        shown, total = _windowed(entries, since, a.full, 3)
        rows = [{"date": _mdy(e["date"]), "by": e["by"],
                 "text": e["text"] if a.full else e["text"][:240]} for e in shown]
        blocks.append(cli.toon("nextSteps", ["date", "by", "text"], rows))
        blocks.append(f"nextSteps: showing {len(shown)} of {total}")
        j["nextSteps"] = {"shown": len(shown), "total": total, "entries": shown}

    if "meddpicc" in wanted:
        rows, empty = [], []
        for label, api, status_api in MEDDPICC_ROWS:
            val = r.get(api)
            if not val:
                empty.append(label)
                continue
            rows.append({"field": label, "status": r.get(status_api) or "" if status_api else "",
                        "value": val if a.full else val[:200]})
        blocks.append(cli.toon("meddpicc", ["field", "status", "value"], rows))
        blocks.append(f"meddpicc.empty: {', '.join(empty) if empty else '(all populated)'}")
        j["meddpicc"] = {"rows": [{"field": lbl, "status": (r.get(sapi) or "") if sapi else "",
                                   "value": r.get(api) or ""}
                                  for lbl, api, sapi in MEDDPICC_ROWS if r.get(api)],
                         "empty": empty}

    if "qualification" in wanted:
        rows, empty = [], []
        for label, api in QUALIFICATION_ROWS:
            val = r.get(api)
            if not val:
                empty.append(label)
                continue
            rows.append({"field": label, "value": val if a.full else val[:200]})
        blocks.append(cli.toon("qualification", ["field", "value"], rows))
        blocks.append(f"qualification.empty: {', '.join(empty) if empty else '(all populated)'}")
        j["qualification"] = {"rows": [{"field": lbl, "value": r.get(api) or ""}
                                       for lbl, api in QUALIFICATION_ROWS if r.get(api)],
                              "empty": empty}

    if "narrative" in wanted:
        rows, empty = [], []
        for label, api in NARRATIVE_ROWS:
            full_val = r.get(api) or ""
            if not full_val:
                empty.append(label)
                continue
            shown_val = full_val if a.full else full_val[:300]
            rows.append({"field": label, "value": shown_val + cli.size_hint(full_val, shown_val)})
        blocks.append(cli.toon("narrative", ["field", "value"], rows))
        blocks.append(f"narrative.empty: {', '.join(empty) if empty else '(all populated)'}")

        link_rows, link_empty = [], []
        for label, api in LINKS_ROWS:
            url = r.get(api) or ""
            if not url:
                link_empty.append(label)
                continue
            link_rows.append({"field": label, "url": url})
        blocks.append(cli.toon("links", ["field", "url"], link_rows))
        blocks.append(f"links.empty: {', '.join(link_empty) if link_empty else '(all populated)'}")

        j["narrative"] = {"rows": [{"field": lbl, "value": r.get(api) or ""}
                                   for lbl, api in NARRATIVE_ROWS if r.get(api)],
                          "empty": empty}
        j["links"] = {"rows": [{"field": lbl, "url": r.get(api) or ""}
                               for lbl, api in LINKS_ROWS if r.get(api)],
                     "empty": link_empty}

    if "activity" in wanted:
        tasks = cli.sf_query(
            "SELECT Id, Subject, Description, TaskSubtype, ActivityDate, CreatedDate, "
            f"Owner.Name FROM Task WHERE WhatId = '{oid}' "
            "ORDER BY ActivityDate DESC NULLS LAST, CreatedDate DESC")
        events = cli.sf_query(
            "SELECT Id, Subject, Description, ActivityDateTime, ActivityDate, CreatedDate, "
            f"Owner.Name FROM Event WHERE WhatId = '{oid}' "
            "ORDER BY ActivityDateTime DESC NULLS LAST, CreatedDate DESC")
        acts, nudges = [], []
        for t in tasks:
            subj = t.get("Subject") or ""
            d = _iso_date(t.get("ActivityDate") or t.get("CreatedDate"))
            if NUDGE_RE.match(subj):
                nudges.append(d)
                continue
            acts.append({"date": d, "kind": (t.get("TaskSubtype") or "Task").lower(),
                        "by": _rel_name(t, "Owner"), "subject": subj,
                        "summary": clari_summary(t.get("Description"))})
        for e in events:
            d = _iso_date(e.get("ActivityDateTime") or e.get("ActivityDate") or e.get("CreatedDate"))
            acts.append({"date": d, "kind": "event", "by": _rel_name(e, "Owner"),
                        "subject": e.get("Subject") or "",
                        "summary": clari_summary(e.get("Description"))})
        acts.sort(key=lambda x: x["date"] or "", reverse=True)
        shown, total = _windowed(acts, since, a.full, 5)
        rows = [{"date": _mdy(x["date"]), "kind": x["kind"], "by": x["by"],
                "subject": x["subject"],
                "summary": x["summary"] if a.full else x["summary"][:300]} for x in shown]
        blocks.append(cli.toon("activity", ["date", "kind", "by", "subject", "summary"], rows))
        blocks.append(f"activity: showing {len(shown)} of {total}")
        latest_nudge = max((d for d in nudges if d), default=None)
        blocks.append(f"activity.nudges: {len(nudges)} stale-next-steps reminders"
                      + (f" (latest {_mdy(latest_nudge)})" if latest_nudge else ""))
        j["activity"] = {"shown": len(shown), "total": total, "nudges": len(nudges), "rows": shown}

    if "notes" in wanted or "files" in wanted:
        cvs = _content_versions(oid)
        note_gaps = []
        if "notes" in wanted:
            note_src = [_cv_row(cv) for cv in cvs if (cv.get("FileType") or "") == "SNOTE"]
            shown, total = _windowed(note_src, since, a.full, 5)
            if a.full:
                for row in shown:
                    body, err = _note_body(row["_id"])
                    if err:
                        note_gaps.append(cli.gap("sf-note-body",
                                                 f"could not fetch note body for "
                                                 f"'{row['title'] or row['_id']}': {err}",
                                                 f"sf org login web --alias {cli.ORG}"))
                        row["body"] = f"[body UNAVAILABLE] {row['body']}"
                    else:
                        row["body"] = body
            rows = [{"date": _mdy(x["date"]), "by": x["by"], "title": x["title"],
                    "body": x["body"]} for x in shown]
            blocks.append(cli.toon("notes", ["date", "by", "title", "body"], rows))
            blocks.append(f"notes: showing {len(shown)} of {total}")
            blocks += note_gaps
            j["notes"] = {"shown": len(shown), "total": total,
                          "rows": [{k: v for k, v in x.items() if k != "_id"} for x in shown]}
        if "files" in wanted:
            file_src = [_cv_row(cv, is_note=False) for cv in cvs
                       if (cv.get("FileType") or "") != "SNOTE"]
            rows = [{"date": _mdy(x["date"]), "by": x["by"], "title": x["title"],
                    "type": x["type"]} for x in file_src]
            blocks.append(cli.toon("files", ["date", "by", "title", "type"], rows))
            j["files"] = [{k: v for k, v in x.items() if k != "_id"} for x in file_src]

    if "changes" in wanted:
        hist = cli.sf_query(
            "SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name "
            f"FROM OpportunityFieldHistory WHERE OpportunityId = '{oid}' "
            "AND Field != 'Sales_Engineer_Overview__c' ORDER BY CreatedDate DESC")
        ch_src = [{"date": _iso_date(x.get("CreatedDate")), "by": _rel_name(x, "CreatedBy"),
                  "field": x.get("Field"), "old": x.get("OldValue"), "new": x.get("NewValue")}
                 for x in hist]
        shown, total = _windowed(ch_src, since, a.full, 8)
        rows = [{"date": _mdy(x["date"]), "by": x["by"], "field": x["field"],
                "old": x["old"], "new": x["new"]} for x in shown]
        blocks.append(cli.toon("changes", ["date", "by", "field", "old", "new"], rows))
        blocks.append(f"changes: showing {len(shown)} of {total}")
        j["changes"] = shown

    if "contacts" in wanted:
        ocr = cli.sf_query(
            "SELECT Contact.Name, Contact.Title, Role, IsPrimary FROM OpportunityContactRole "
            f"WHERE OpportunityId = '{oid}'")
        rows = [{"name": _rel_name(x, "Contact"), "title": _rel_name(x, "Contact", "Title"),
                "role": x.get("Role") or "", "primary": x.get("IsPrimary")} for x in ocr]
        blocks.append(cli.toon("contacts", ["name", "title", "role", "primary"], rows))
        j["contacts"] = rows

    if a.json:
        j["gaps"] = list(cli._GAPS)
        print(json.dumps(j, indent=2, default=str))
        return

    since_hint = (datetime.now().date() - timedelta(days=30)).strftime("%-m/%-d/%y")
    # rep hints use the id, not the slug: `rep <slug>` resolves to the account's primary
    # opp, which is not this one when the caller came in by id on a sibling.
    blocks.append(cli.nxt(f"opp-axi rep {id15} --full", f"opp-axi rep {id15} --since {since_hint}",
                          f"opp-axi opp {slug}", f"opp-axi evidence {slug}"))
    cli.emit(*blocks)


def _content_versions(oid):
    """Lightning Notes + files linked to the opp, newest LastModifiedDate first.
    Skips the ContentVersion query entirely when there are no links -- an empty
    CDL result IS the answer, not a reason to guess."""
    cdl = cli.sf_query(f"SELECT ContentDocumentId FROM ContentDocumentLink "
                       f"WHERE LinkedEntityId = '{oid}'")
    doc_ids = [d["ContentDocumentId"] for d in cdl if d.get("ContentDocumentId")]
    if not doc_ids:
        return []
    ids_sql = ",".join(f"'{i}'" for i in doc_ids)
    return cli.sf_query(
        "SELECT Id, ContentDocumentId, Title, FileType, TextPreview, CreatedBy.Name, "
        "CreatedDate, LastModifiedDate, ContentSize FROM ContentVersion "
        f"WHERE ContentDocumentId IN ({ids_sql}) AND IsLatest = true "
        "ORDER BY LastModifiedDate DESC")


def _cv_row(cv, is_note=True):
    row = {"_id": cv.get("Id"), "date": _iso_date(cv.get("LastModifiedDate")),
          "by": _rel_name(cv, "CreatedBy"), "title": cv.get("Title") or ""}
    if is_note:
        row["body"] = cv.get("TextPreview") or ""
    else:
        row["type"] = cv.get("FileType") or ""
    return row


def _note_body(version_id):
    """The full body of one Lightning Note: a plain, read-only GET of its VersionData.
    Returns (body, error) -- error is '' on success."""
    p = cli._run(["sf", "api", "request", "rest",
                 f"/services/data/{cli.API}/sobjects/ContentVersion/{version_id}/VersionData",
                 "-o", cli.ORG])
    if p.returncode != 0:
        return "", (p.stderr or p.stdout or "unknown error").strip()[:200]
    return _strip_html(p.stdout), ""


# ── rollup: opp-axi rep (no ref) ──────────────────────────────────────────────
def _rollup(a):
    since = _parse_since(a.since)
    today = datetime.now().date()
    recs = cli.sf_query(
        "SELECT Id, Name, Owner.Name, StageName, Forecast_Status__c, "
        "Next_Steps_Last_Updated__c, Next_Steps__c FROM Opportunity "
        f"WHERE {cli.OPEN_WHERE} ORDER BY Next_Steps_Last_Updated__c DESC NULLS LAST")
    i2s = cli.id_to_slug(cli.opp_index())

    raw, updated7, stale14, nosteps = [], 0, 0, 0
    for r in recs:
        owner = _rel_name(r, "Owner")
        ns_text = r.get("Next_Steps__c") or ""
        updated_iso = _iso_date(r.get("Next_Steps_Last_Updated__c"))
        stale = _days_stale(updated_iso, today)
        if not ns_text.strip():
            nosteps += 1
        if stale is not None and stale > 14:
            stale14 += 1
        if stale is not None and stale <= 7:
            updated7 += 1

        if since and (not updated_iso or updated_iso < since.isoformat()):
            continue
        entries = parse_next_steps(ns_text, (_owner_initials_of(owner),) if owner else (), today)
        latest = entries[0] if entries else None
        # Several accounts (tesla, toyota, aunalytics) carry more than one open opp, so
        # the slug alone is ambiguous -- the id is the thing `opp-axi rep <id>` resolves
        # unambiguously.
        raw.append({"id": r["Id"][:15], "slug": i2s.get(r["Id"][:15], "-"), "owner": owner,
                   "stage": r.get("StageName"), "forecast": r.get("Forecast_Status__c"),
                   "updated": updated_iso, "daysStale": stale, "latest": latest})

    if a.json:
        print(json.dumps({
            "version": cli.__version__, "se": cli.ME,
            "since": since.isoformat() if since else None,
            "reps": raw,
            "counts": {"open": len(recs), "updated_le_7d": updated7,
                      "stale_gt_14d": stale14, "no_next_steps": nosteps},
        }, indent=2, default=str))
        return

    rows = []
    for x in raw:
        latest = x["latest"]
        if latest:
            d = _mdy(latest["date"]) if latest["date"] else ""
            txt = latest["text"] if a.full else latest["text"][:100]
            latest_str = f"{d} {txt}".strip()
        else:
            latest_str = ""
        rows.append({"id": x["id"], "slug": x["slug"], "owner": x["owner"], "stage": x["stage"],
                    "forecast": x["forecast"], "updated": _mdy(x["updated"]),
                    "daysStale": x["daysStale"] if x["daysStale"] is not None else "",
                    "latest": latest_str})

    cli.emit(f"rep rollup se={cli.ME}",
             cli.toon("reps", ["id", "slug", "owner", "stage", "forecast", "updated",
                               "daysStale", "latest"], rows),
             f"\nopen:{len(recs)} updated<=7d:{updated7} stale>14d:{stale14} "
             f"noNextSteps:{nosteps}",
             cli.nxt("opp-axi rep <id>", "opp-axi rep <id> --full"))
