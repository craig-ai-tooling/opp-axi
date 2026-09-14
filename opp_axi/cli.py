#!/usr/bin/env python3
"""opp-axi — agent-ergonomic CLI over Salesforce + Google Workspace for SE pipeline work.

Built on AXI (Agent eXperience Interface) principles: token-efficient TOON output,
minimal default schemas, truncation with --full escape hatches, pre-computed
aggregates (no round trips), definitive empty states, structured exit codes,
content-first (no args = live data), and next-step disclosure.

The point is not that less data moves — it's that less data reaches the model's
context. The subprocess may pull 165KB; the agent sees 4KB.

Connectors are declared, not assumed. `opp-axi doctor` reports each one; a REQUIRED
connector that is down refuses the command, and an OPTIONAL one that is absent is
reported as UNAVAILABLE and exits E_PARTIAL — never as an empty result. "Nobody met
with them" and "I could not look" are different facts and must not share a rendering.

Env overrides: OPP_REPO, SF_ORG (spectrocloud), OPP_SE (CraigSmith), OPP_INITIALS (CS),
OPP_WISPR_DIR, OPP_WISPR (on|off), OPP_PATTERNS
"""
from __future__ import annotations


import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

from opp_axi import __version__

# Packaged as a zipapp, __file__ is inside the archive, so the old
# dirname(dirname(realpath(__file__))) trick cannot find the opp repo any more.
# The data repo is now named explicitly and `doctor` checks that it exists.
REPO = os.environ.get("OPP_REPO") or os.path.expanduser("~/code/customer-opportunities")
ORG = os.environ.get("SF_ORG", "spectrocloud")
ME = os.environ.get("OPP_SE", "CraigSmith")
INITIALS = os.environ.get("OPP_INITIALS", "CS")
API = "v67.0"
WISPR_DIR = os.environ.get("OPP_WISPR_DIR") or os.path.expanduser("~/.cache/opp-axi/wispr")
# Wispr is OPTIONAL. Three states, deliberately distinct:
#   off      — declared absent by the operator. Not a gap; exits clean.
#   on       — expected present. A missing cache is a GAP, never an empty table.
WISPR_ENABLED = (os.environ.get("OPP_WISPR", "on").strip().lower()
                 not in ("off", "0", "false", "no"))
WISPR_STALE_DAYS = 2
WISPR_GAP_HOURS = 6   # a same-day sync can still predate that day's meetings

E_OK, E_ERR, E_USAGE, E_NOTFOUND, E_REFUSED, E_PARTIAL = 0, 1, 2, 3, 4, 5

# Sources that could not be consulted during this run. A command that finishes with a
# non-empty ledger exits E_PARTIAL: it did work, but something it was supposed to look
# at was not looked at, and the caller must be able to tell that from the exit code
# alone. Checking stdout for a warning string is not a contract anyone honours.
_GAPS: list[dict] = []


def gap(source, reason, fix=""):
    """Record that a declared source could not be consulted. Returns a render-ready line."""
    _GAPS.append({"source": source, "reason": reason, "fix": fix})
    return f"{source}: UNAVAILABLE — {reason}" + (f" | fix: {fix}" if fix else "")

OPEN_WHERE = f"IsClosed = false AND SA_Assignment_Oppty__c = '{ME}'"


# ── output ─────────────────────────────────────────────────────────────────
def _tv(v):
    """TOON scalar: quote only when it would break the row.

    None and False are DIFFERENT facts and must not share a cell. Collapsing
    False into "" made "POV not required" read identically to "nobody knows",
    which is exactly the kind of silent wrong answer this tool exists to stop.
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
    """Principle 9 — contextual disclosure."""
    return "\nnext: " + " | ".join(suggestions) if suggestions else ""


def die(msg, code=E_ERR):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def size_hint(full, shown):
    extra = len(full) - len(shown)
    return f"  [+{extra}B truncated, --full]" if extra > 0 else ""


# ── subprocess plumbing ────────────────────────────────────────────────────
def _run(cmd, timeout=180):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"timeout after {timeout}s: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}")
    except FileNotFoundError:
        die(f"not found: {cmd[0]}")


def _parse_json(s, what="output"):
    """gws prints 'Using keyring backend: keyring' before its JSON. Skip any preamble."""
    i, j = s.find("{"), s.find("[")
    if i < 0 and j < 0:
        die(f"no JSON in {what}: {s.strip()[:200]}")
    start = j if (i < 0 or (0 <= j < i)) else i
    try:
        return json.loads(s[start:])
    except json.JSONDecodeError as e:
        die(f"bad JSON in {what}: {e}")


def sf_query(soql, timeout=180):
    p = _run(["sf", "data", "query", "-q", soql, "-o", ORG, "--json"], timeout)
    d = _parse_json(p.stdout or p.stderr, "sf query")
    if p.returncode != 0 or d.get("status") not in (0, None):
        die(f"sf query failed: {d.get('message', (p.stderr or '').strip())[:300]}")
    return d.get("result", {}).get("records", [])


def sf_patch(opp_id, body):
    """REST PATCH only. `sf data update -v` silently writes null for emoji picklists
    and mangles newlines in long textareas — it reports success either way."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, ensure_ascii=False)
    try:
        p = _run(["sf", "api", "request", "rest",
                  f"/services/data/{API}/sobjects/Opportunity/{opp_id}",
                  "--method", "PATCH", "--body", f"@{path}", "-o", ORG])
        if p.returncode != 0:
            die(f"PATCH failed: {(p.stderr or p.stdout).strip()[:300]}")
    finally:
        os.unlink(path)


def gcli():
    for c in ("gws", "gog"):
        if shutil.which(c):
            return c
    die("no Google CLI on PATH (need gws or gog)")


# ── repo index ─────────────────────────────────────────────────────────────
def _acct(d):
    """Tolerate both key styles gen-pipeline.py handles."""
    a = d.get("account") or {}
    return d.get("account_name") or a.get("name") or ""


def opp_index():
    idx = {}
    for p in sorted(glob.glob(os.path.join(REPO, "*", ".salesforce.json"))):
        slug = os.path.basename(os.path.dirname(p))
        try:
            idx[slug] = json.load(open(p))
        except Exception:
            continue
    return idx


def id_to_slug(idx):
    m = {}
    for slug, d in idx.items():
        for o in d.get("opportunities", []) or []:
            if o.get("id"):
                m[o["id"][:15]] = slug
    return m


def resolve(ref, idx):
    """slug | 15/18-char SF id | unique substring -> (slug, opp_id). Primary opp wins."""
    if re.fullmatch(r"006[A-Za-z0-9]{12,15}", ref or ""):
        return id_to_slug(idx).get(ref[:15], "?"), ref
    cands = [s for s in idx if s == ref] or [s for s in idx if ref.lower() in s.lower()]
    if not cands:
        die(f"no opp dir matching '{ref}'", E_NOTFOUND)
    if len(cands) > 1:
        die(f"ambiguous '{ref}': {', '.join(sorted(cands)[:8])}", E_USAGE)
    slug = cands[0]
    opps = idx[slug].get("opportunities", []) or []
    live = [o for o in opps if (o.get("status") or "open") == "open"]
    prim = next((o for o in opps if o.get("primary")), None) \
        or (max(live, key=lambda o: o.get("amount") or 0) if live else None)
    if not prim or not prim.get("id"):
        die(f"{slug} has no open/primary opp in .salesforce.json", E_NOTFOUND)
    return slug, prim["id"]


STOP = {"corp", "inc", "llc", "ltd", "the", "company", "corporation", "group",
        "technologies", "technology", "systems", "solutions", "north", "america",
        # generic across many accounts — matching on these produces wrong-deal evidence
        "international", "enterprise", "enterprises", "services", "service", "holdings",
        "industries", "global", "partners", "communications", "companies", "worldwide",
        "consulting", "software", "hardware", "digital", "platform", "spectro", "cloud"}

# Not stop words — these are real evidence when the domain agrees. But as a bare Gmail
# `subject:` term they drag in every other account in the same industry, which is how
# MedImpact Healthcare's evidence filled up with GE HealthCare threads. Used only to rank
# subject search terms, never to block a match. Deliberately tight: "realty" and "travel"
# are NOT here, because they are the only distinguishing token some accounts have.
INDUSTRY = {"healthcare", "health", "medical", "insurance", "financial", "bank", "energy",
            "retail", "foods", "brands", "pharma", "labs", "media", "telecom", "wireless",
            "networks", "security", "analytics", "research", "logistics", "stores", "market"}


def match_tokens(slug, account, aliases=()):
    toks = set()
    for src in (slug.replace("-", " "), account or "", " ".join(aliases)):
        for w in re.split(r"[^A-Za-z0-9]+", src.lower()):
            if len(w) >= 4 and w not in STOP:
                toks.add(w)
    # aliases are explicit, so honour short ones (AB, CD, 3xk) the length filter would drop
    for al in aliases:
        al = al.strip().lower()
        if al:
            toks.add(al)
    return toks


# Public suffixes seen on customer domains — used only to find the registrable label, so
# "gehealthcare.com" -> "gehealthcare" and a token can never match on "com".
_TLD1 = {"com", "net", "org", "io", "ai", "co", "us", "gov", "edu", "mil", "info", "biz"}
_TLD2 = {"co.uk", "com.au", "co.jp", "co.nz", "com.br", "co.in", "com.mx"}
_INFRA_LABELS = {"www", "mail", "smtp", "mx"}


def domain_labels(domains):
    """['connection.com;tom-is.com'] -> {'connection', 'tom-is', 'tomis'}.

    Public suffix dropped. Hyphenated labels are returned both ways, because account names
    lose the hyphen ("tom-is" vs "tomis")."""
    out = set()
    for chunk in re.split(r"[;,\s]+", " ".join(domains or []).lower()):
        chunk = chunk.strip().strip(".")
        if not chunk:
            continue
        parts = [p for p in chunk.split(".") if p]
        if len(parts) >= 3 and ".".join(parts[-2:]) in _TLD2:
            parts = parts[:-2]
        elif len(parts) >= 2 and (parts[-1] in _TLD1 or len(parts[-1]) <= 3):
            parts = parts[:-1]
        for lab in parts:
            if lab and lab not in _INFRA_LABELS:
                out.add(lab)
                out.add(lab.replace("-", ""))
    return out


def cover_vocab(slug, account, aliases=()):
    """Every word an account owns, including STOP words and 2-3 char fragments.

    Deliberately wider than match_tokens(): "ge" and "digital" are useless as standalone
    evidence but are exactly the pieces needed to account for a whole domain label."""
    out = set()
    for src in (slug.replace("-", " "), account or "", " ".join(aliases)):
        for w in re.split(r"[^A-Za-z0-9]+", (src or "").lower()):
            if len(w) >= 2:
                out.add(w)
    return out


def explicit_tokens(slug, aliases=()):
    """Tokens the repo declared on purpose. These carry a domain match at any length,
    so a short-but-real slug or alias (3xk, TI, GEHC) still matches its own domain."""
    out = {(slug or "").lower().replace("-", "")}
    out |= {w for w in re.split(r"[^A-Za-z0-9]+", (slug or "").lower()) if w}
    out |= {a.strip().lower() for a in aliases if a and a.strip()}
    return {t for t in out if t}


def segmentable(label, vocab):
    """Can `label` be built end-to-end out of this account's own words?

    This is what separates GE HealthCare from MedImpact Healthcare on gehealthcare.com:
    both own the word "healthcare", but only GE can also account for the leading "ge".
    A substring test cannot tell them apart, and the old one did not try."""
    n = len(label)
    reach = [False] * (n + 1)
    reach[0] = True
    for i in range(n):
        if not reach[i]:
            continue
        for j in range(i + 2, n + 1):          # no 1-char segments
            if label[i:j] in vocab:
                reach[j] = True
    return reach[n]


def norm_hay(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower())


def text_hit(tok, hay):
    """Short tokens/aliases must match as WHOLE words — a prefix rule makes 'ti' match
    'time' and 'by' match every sentence. Longer ones allow suffixes. `hay` must already
    be lowercased and punctuation-stripped by norm_hay()."""
    pat = r"\b" + re.escape(tok) + (r"\b" if len(tok) <= 4 else r"")
    return re.search(pat, hay) is not None


def domain_hits(toks, vocab, explicit, domains):
    """Tokens that legitimately match one of these domains.

    A hit requires the account to explain the WHOLE registrable label and to contribute a
    real token to it. The rule this replaces was `tok in " ".join(domains)` — an unanchored
    substring test — so the token "health" matched gehealthcare.com and GE HealthCare
    threads were served as evidence for MedImpact Healthcare, Elevance Health and Centauri
    Health. Because a domain hit outranks every text hit, the wrong account won outright."""
    hits = []
    for lab in domain_labels(domains):
        if not segmentable(lab, vocab):
            continue
        hits.extend(t for t in toks
                    if (len(t) >= 4 or t in explicit)
                    and (t == lab or lab.startswith(t) or lab.endswith(t)))
    return sorted(set(hits))


def build_matcher(idx):
    table = []
    for slug, d in idx.items():
        acct = _acct(d)
        aliases = d.get("aliases") or ()
        table.append((slug, match_tokens(slug, acct, aliases),
                      cover_vocab(slug, acct, aliases), explicit_tokens(slug, aliases)))

    def match(text, domains):
        """Best candidate, not first-in-dict-order. Domain evidence outranks text; among
        text matches, more distinct tokens wins, then the longest token."""
        hay = norm_hay(text)
        best, best_score = "", ()
        for slug, toks, vocab, expl in table:
            if not toks:
                continue
            dhits = domain_hits(toks, vocab, expl, domains)
            hhits = [t for t in toks if text_hit(t, hay)]
            if not (dhits or hhits):
                continue
            score = (1 if dhits else 0, len(dhits or hhits),
                     max(len(t) for t in (dhits or hhits)))
            if score > best_score:
                best, best_score = slug, score
        return best
    return match


# ── SE-activity dates ──────────────────────────────────────────────────────
def first_line(text):
    return (text or "").strip().split("\n")[0].strip()


def entry_date(text):
    """Leading date of the newest SE Activity entry. Handles 8/25/26, 08/17/2026, 2026-03-20."""
    ln = first_line(text)
    m = re.match(r"^[-\s]*(\d{4})-(\d{1,2})-(\d{1,2})", ln)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^[-\s]*(\d{1,2})/(\d{1,2})/(\d{2,4})", ln)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
    try:
        return datetime(y, mo, d).date()
    except ValueError:
        return None


PLACEHOLDER = re.compile(
    r"no\s+(new\s+)?(activity|update|updates|change|changes|progress)|nothing\s+to\s+report",
    re.I)


def entry_author(text):
    """Initials on the newest entry, or None. Style 2 ('2026-03-20 — ...') carries no
    initials and is a valid Craig entry, so absence must NOT be read as someone else."""
    ln = first_line(text)
    m = re.match(r"^[-\s]*(?:\d[\d/\-]*)\s*[-\u2013]?\s*([A-Z]{2,3})\s*:", ln)
    return m.group(1) if m else None


def is_placeholder(text):
    return bool(PLACEHOLDER.search(first_line(text)))


def money(n):
    return f"{int(n):,}" if n else "0"


# ── commands ───────────────────────────────────────────────────────────────
def cmd_overview(a):
    """No args: live pipeline + aggregates. Principle 8 — content first, not help text."""
    recs = sf_query(
        "SELECT Id,Name,StageName,Amount,CloseDate,SE_Forecast__c,Tech_Risk_Status__c "
        f"FROM Opportunity WHERE {OPEN_WHERE} ORDER BY CloseDate")
    i2s = id_to_slug(opp_index())
    rows, total, risk = [], 0, 0
    for r in recs:
        amt = r.get("Amount") or 0
        total += amt
        fc = r.get("SE_Forecast__c") or ""
        if fc == "At Risk" or "High" in (r.get("Tech_Risk_Status__c") or ""):
            risk += 1
        rows.append({"id": r["Id"][:15], "slug": i2s.get(r["Id"][:15], "-"),
                     "stage": r.get("StageName"), "amt": money(amt),
                     "close": r.get("CloseDate"), "fc": fc})
    emit(f"pipeline se={ME} org={ORG}",
         toon("opps", ["id", "slug", "stage", "amt", "close", "fc"], rows),
         f"\nopen:{len(rows)} value:${money(total)} atRisk:{risk} "
         f"noDir:{sum(1 for r in rows if r['slug'] == '-')}",
         nxt("opp-axi sweep", "opp-axi opp <slug>", "opp-axi cal"))


def cmd_opps(a):
    where = OPEN_WHERE
    if a.at_risk:
        where += " AND (SE_Forecast__c = 'At Risk' OR Tech_Risk_Status__c LIKE '%High%')"
    recs = sf_query(f"SELECT Id,Name,StageName,Amount,CloseDate,SE_Forecast__c,Tech_Risk_Status__c "
                    f"FROM Opportunity WHERE {where} ORDER BY Amount DESC")
    i2s = id_to_slug(opp_index())
    rows = [{"id": r["Id"][:15], "slug": i2s.get(r["Id"][:15], "-"),
             "name": r["Name"] if a.full else r["Name"][:44],
             "stage": r.get("StageName"), "amt": money(r.get("Amount")),
             "close": r.get("CloseDate"), "fc": r.get("SE_Forecast__c"),
             "risk": r.get("Tech_Risk_Status__c")} for r in recs]
    emit(toon("opps", ["id", "slug", "name", "stage", "amt", "close", "fc", "risk"], rows),
         f"\ncount:{len(rows)} value:${money(sum(r.get('Amount') or 0 for r in recs))}",
         nxt("opp-axi opp <slug>", "opp-axi opps --full"))


def cmd_opp(a):
    idx = opp_index()
    slug, oid = resolve(a.ref, idx)
    recs = sf_query(
        "SELECT Id,Name,StageName,Amount,CloseDate,SE_Forecast__c,Tech_Risk_Status__c,"
        "POV_Pass__c,Solution_Validated__c,Hands_on_Eval_Required__c,"
        "Hands_on_Eval_EstStartDate__c,Hands_on_Eval_POV_URL__c,SA_Assignment_Oppty__c,"
        f"Sales_Engineer_Overview__c,Technical_Notes__c FROM Opportunity WHERE Id = '{oid}'")
    if not recs:
        die(f"opp {oid} not found in {ORG}", E_NOTFOUND)
    r = recs[0]
    mine = (r.get("SA_Assignment_Oppty__c") or "") == ME
    act = r.get("Sales_Engineer_Overview__c") or ""
    notes = r.get("Technical_Notes__c") or ""
    shown_act = act if a.full else first_line(act)
    shown_notes = notes if a.full else notes[:300]
    emit(
        toon("opp", ["id", "slug", "stage", "amt", "close", "fc", "risk", "se", "writable"],
             [{"id": r["Id"][:15], "slug": slug, "stage": r.get("StageName"),
               "amt": money(r.get("Amount")), "close": r.get("CloseDate"),
               "fc": r.get("SE_Forecast__c"), "risk": r.get("Tech_Risk_Status__c"),
               "se": r.get("SA_Assignment_Oppty__c"), "writable": mine}]),
        f"name: {r['Name']}",
        toon("pov", ["required", "techWin", "validated", "estStart", "planUrl"],
             [{"required": r.get("Hands_on_Eval_Required__c"), "techWin": r.get("POV_Pass__c"),
               "validated": r.get("Solution_Validated__c"),
               "estStart": r.get("Hands_on_Eval_EstStartDate__c"),
               "planUrl": r.get("Hands_on_Eval_POV_URL__c") or ""}]),
        f"\nactivity.latest: {shown_act or '(empty)'}{size_hint(act, shown_act)}",
        f"notes: {shown_notes or '(empty)'}{size_hint(notes, shown_notes)}",
        "" if mine else f"\nWARNING: SE is '{r.get('SA_Assignment_Oppty__c')}', not {ME} — read-only, do not write.",
        nxt(f"opp-axi opp {slug} --full", f"opp-axi activity {slug} --add \"...\"") if mine
        else nxt(f"opp-axi opp {slug} --full"))


def cmd_sweep(a):
    """Weekly-sweep completeness. Pulls 126KB of SE Activity; shows you ~2KB of first lines.

    Reports four distinct states rather than a single swept/missing flag, because a field
    that is merely non-empty is not the same as a week that was actually worked:
      stale      — newest entry predates the cutoff
      empty      — no parseable entry at all
      other-se   — newest entry carries another SE's initials, so it is not your report
      thin       — swept, but the entry is a placeholder with no substance
    """
    since = (datetime.strptime(a.since, "%Y-%m-%d").date() if a.since
             else (datetime.now().date() - timedelta(days=7)))
    recs = sf_query("SELECT Id,Name,Amount,CloseDate,Sales_Engineer_Overview__c "
                    f"FROM Opportunity WHERE {OPEN_WHERE} ORDER BY CloseDate")
    i2s = id_to_slug(opp_index())
    missing, thin, swept = [], [], 0
    for r in recs:
        act = r.get("Sales_Engineer_Overview__c")
        d, who = entry_date(act), entry_author(act)
        row = {"id": r["Id"][:15], "slug": i2s.get(r["Id"][:15], "-"),
               "amt": money(r.get("Amount")), "close": r.get("CloseDate"),
               "last": d.strftime("%-m/%-d/%y") if d else "none"}
        if not d:
            missing.append({**row, "why": "empty"})
        elif d < since:
            missing.append({**row, "why": "stale"})
        elif who and who != INITIALS:
            missing.append({**row, "why": f"other-se:{who}"})
        else:
            swept += 1
            if is_placeholder(act):
                thin.append({**row, "entry": first_line(act)[:64]})
    total = len(recs)
    cov = round(100 * swept / total) if total else 0
    stamp = datetime.now().strftime("%-m/%-d/%y")

    blocks = [f"sweep since={since} se={ME}",
              toon("missing", ["id", "slug", "amt", "close", "last", "why"], missing),
              toon("thin", ["slug", "last", "entry"], thin)]

    if a.secondary:
        sec = sf_query("SELECT Id,Name,Amount,SA_Assignment_Oppty__c FROM Opportunity "
                       f"WHERE IsClosed=false AND Secondary_Solution_Architect_Assignment__c='{ME}' "
                       f"AND SA_Assignment_Oppty__c!='{ME}'")
        blocks.append(toon("secondary", ["slug", "se", "amt", "name"],
                           [{"slug": i2s.get(x["Id"][:15], "-"),
                             "se": x.get("SA_Assignment_Oppty__c"),
                             "amt": money(x.get("Amount")), "name": x["Name"][:40]}
                            for x in sec]))
        blocks.append("  ^ read-only: not your record. Keep context in OPP.md, never write.")

    if a.closed:
        cl = sf_query("SELECT Id,Name,StageName,CloseDate FROM Opportunity "
                      f"WHERE IsClosed=true AND SA_Assignment_Oppty__c='{ME}' "
                      f"AND CloseDate=LAST_N_DAYS:{a.closed_days} ORDER BY CloseDate DESC")
        blocks.append(toon("closed", ["slug", "stage", "close", "name"],
                           [{"slug": i2s.get(x["Id"][:15], "-"), "stage": x.get("StageName"),
                             "close": x.get("CloseDate"), "name": x["Name"][:40]} for x in cl]))
        blocks.append(f"  ^ closed in {a.closed_days}d — wins may need a tech-win entry, "
                      "losses a post-mortem line.")

    blocks += [f"\nopen:{total} swept:{swept} thin:{len(thin)} missing:{len(missing)} "
               f"coverage:{cov}%",
               nxt("opp-axi evidence <slug>",
                   f'opp-axi activity <slug> --add "..."  # auto-stamps {stamp} {INITIALS}:')
               if missing or thin else nxt("sweep complete")]
    emit(*blocks)


def cmd_activity(a):
    idx = opp_index()
    slug, oid = resolve(a.ref, idx)
    recs = sf_query("SELECT Id,Name,SA_Assignment_Oppty__c,Sales_Engineer_Overview__c "
                    f"FROM Opportunity WHERE Id = '{oid}'")
    if not recs:
        die(f"opp {oid} not found", E_NOTFOUND)
    r = recs[0]
    owner = r.get("SA_Assignment_Oppty__c") or ""
    if owner != ME:
        die(f"refusing write: SE on {slug} is '{owner}', not {ME}. "
            f"Secondary-SE assignment is not write access — keep context in OPP.md.", E_REFUSED)
    text = a.add.strip()
    if not re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}|^\d{4}-\d{2}-\d{2}", text):
        text = f"{datetime.now().strftime('%-m/%-d/%y')} {INITIALS}: {text}"
    existing = r.get("Sales_Engineer_Overview__c") or ""
    new = text + ("\n" + existing if existing else "")
    if len(new) > 100000:
        die("SE Activity would exceed the 100k field limit", E_ERR)
    if a.dry_run:
        emit(f"dry-run {slug} {oid}", f"would prepend: {text}",
             f"len {len(existing)} -> {len(new)}", nxt("re-run without --dry-run"))
        return
    sf_patch(oid, {"Sales_Engineer_Overview__c": new})
    check = sf_query(f"SELECT Sales_Engineer_Overview__c FROM Opportunity WHERE Id = '{oid}'")
    got = first_line(check[0].get("Sales_Engineer_Overview__c") if check else "")
    ok = got == text.split("\n")[0]
    emit(toon("write", ["opp", "slug", "field", "verified"],
              [{"opp": oid[:15], "slug": slug, "field": "Sales_Engineer_Overview__c",
                "verified": ok}]),
         f"prepended: {got}",
         "" if ok else "WARNING: readback mismatch — inspect the record.",
         nxt(f"opp-axi opp {slug}", "opp-axi sweep"))
    if not ok:
        sys.exit(E_ERR)


def cmd_cal(a):
    """Calendar normalized to what an SE needs. Raw Google API is ~1.5k tok/event."""
    g = gcli()
    day = (datetime.strptime(a.date, "%Y-%m-%d") if a.date and a.date not in ("today", "tomorrow")
           else datetime.now() + timedelta(days=1 if a.date == "tomorrow" else 0))
    lo, hi = day.strftime("%Y-%m-%dT00:00:00Z"), (day + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    if g == "gws":
        params = json.dumps({"calendarId": "primary", "timeMin": lo, "timeMax": hi,
                             "singleEvents": True, "orderBy": "startTime"})
        p = _run([g, "calendar", "events", "list", "--params", params])
        items = _parse_json(p.stdout, "calendar").get("items", [])
    else:
        p = _run([g, "calendar", "events", "--from", day.strftime("%Y-%m-%d"),
                  "--to", (day + timedelta(days=1)).strftime("%Y-%m-%d"), "-j"])
        d = _parse_json(p.stdout, "calendar")
        items = d.get("items", d) if isinstance(d, (dict, list)) else []
    match = build_matcher(opp_index())
    rows = []
    for ev in items:
        if (ev.get("status") or "") == "cancelled":
            continue
        st = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
        atts = [x.get("email", "") for x in (ev.get("attendees") or [])]
        ext = sorted({e.split("@")[-1] for e in atts
                      if "@" in e and not e.endswith("spectrocloud.com")})
        rows.append({"time": st[11:16] if "T" in st else "all-day",
                     "summary": (ev.get("summary") or "")[:52],
                     "n": len(atts), "ext": ";".join(ext[:3]),
                     "opp": match(ev.get("summary", ""), ext)})
    ext_rows = [r for r in rows if r["ext"]]
    emit(f"calendar {day.strftime('%Y-%m-%d')}",
         toon("events", ["time", "summary", "n", "ext", "opp"], rows),
         f"\nevents:{len(rows)} customerFacing:{len(ext_rows)} "
         f"matched:{sum(1 for r in rows if r['opp'])}",
         nxt("opp-axi opp <slug>", 'opp-axi mail "subject:X newer_than:2d"'))


def cmd_mail(a):
    """Search + headers in ONE shot. format=full is ~20k tok/message; metadata is ~235."""
    g = gcli()
    if g != "gws":
        p = _run([g, "gmail", "search", a.query, "-j"])
        emit(p.stdout.strip()[:4000], nxt("gog path: raw passthrough"))
        return
    p = _run([g, "gmail", "users", "messages", "list",
              "--params", json.dumps({"userId": "me", "q": a.query, "maxResults": a.limit})])
    msgs = _parse_json(p.stdout, "gmail list").get("messages", []) or []
    if not msgs:
        emit(toon("mail", ["date", "from", "subject"], []), f'\nquery: {a.query}',
             nxt("widen the query, e.g. newer_than:7d"))
        return
    fmt = "full" if a.full else "metadata"
    rows = []
    for m in msgs:
        params = {"userId": "me", "id": m["id"], "format": fmt}
        if not a.full:
            params["metadataHeaders"] = ["From", "Subject", "Date"]
        q = _run([g, "gmail", "users", "messages", "get", "--params", json.dumps(params)])
        d = _parse_json(q.stdout, "gmail get")
        h = {x["name"].lower(): x["value"] for x in (d.get("payload") or {}).get("headers", [])}
        # NOT truncated. Every other field on this row is a display string and shortening
        # it costs nothing; an id is an ADDRESS. A 12-character Gmail message id fetches
        # nothing, so a caller holding one has to go around opp-axi entirely -- which is
        # exactly what a session did on 8/28/26: "opp-axi truncates the message IDs. Let
        # me query Gmail directly for the thread." A tool that makes the caller bypass it
        # has failed at the one thing it is for.
        row = {"id": m["id"], "date": (h.get("date") or "")[:16],
               "from": (h.get("from") or "")[:38], "subject": (h.get("subject") or "")[:52],
               "snippet": (d.get("snippet") or "")[:90]}
        if a.full:
            row["snippet"] = (d.get("snippet") or "")[:400]
        rows.append(row)
    emit(toon("mail", ["id", "date", "from", "subject", "snippet"], rows),
         f"\ncount:{len(rows)} query:{a.query} format:{fmt}",
         nxt("opp-axi mail <query> --full", "opp-axi opp <slug>"))


SLUG_DROP = {"inc", "inc.", "llc", "ltd", "corp", "corporation", "co", "company", "companies",
             "holdings", "group", "the", "wholesale", "communications", "enterprise",
             "enterprises", "international", "technologies", "incorporated", "plc", "gmbh"}


def slugify(account):
    """Directory slug from an SF account name, matching the existing dirs' style."""
    words = [w for w in re.split(r"[^A-Za-z0-9']+", (account or "").lower()) if w]
    words = [w.replace("'", "") for w in words if w not in SLUG_DROP]
    return "-".join(words)[:40].strip("-") or "unknown"


def cmd_scaffold(a):
    """Create repo dirs for open opps that have none.

    An opp with no directory is invisible to OPP.md, PIPELINE.md, the domain harvesting in
    `evidence`, and every agent workflow — it exists only in Salesforce.
    """
    recs = sf_query(
        "SELECT Id,Name,AccountId,Account.Name,StageName,Amount,CloseDate,SE_Forecast__c,"
        f"Use_Case_Primary__c FROM Opportunity WHERE {OPEN_WHERE} ORDER BY Amount DESC")
    idx = opp_index()
    have = {o.get("id", "")[:15] for d in idx.values() for o in (d.get("opportunities") or [])}

    by_acct = {}
    for r in recs:
        if r["Id"][:15] in have:
            continue
        acct = (r.get("Account") or {}).get("Name") or re.split(r"\s+-\s+", r["Name"])[0]
        by_acct.setdefault((r.get("AccountId"), acct), []).append(r)

    # An account may already have a dir under a different slug (acme vs acme-health),
    # possibly with an empty opportunities list. Adopt it rather than fragmenting the account.
    by_id, by_name = {}, {}
    for sl, d in idx.items():
        if d.get("account_id"):
            by_id[d["account_id"][:15]] = sl
        an = re.sub(r"[^a-z0-9]", "", (_acct(d) or "").lower())
        if an:
            by_name[an] = sl

    made, updated, skipped = [], [], []
    template = note_template()
    today = datetime.now().strftime("%Y-%m-%d")

    for (acct_id, acct), opps in sorted(by_acct.items(), key=lambda kv: -sum(
            o.get("Amount") or 0 for o in kv[1])):
        prim = max(opps, key=lambda o: o.get("Amount") or 0)
        existing = (by_id.get((acct_id or "")[:15])
                    or by_name.get(re.sub(r"[^a-z0-9]", "", acct.lower())))
        if existing:
            # adopt: append these opps to the dir that already represents this account
            fp = os.path.join(REPO, existing, ".salesforce.json")
            try:
                cur = json.load(open(fp))
            except Exception:
                skipped.append({"slug": existing, "acct": acct[:34], "why": "unreadable json"})
                continue
            cur.setdefault("opportunities", [])
            cur.setdefault("account_id", acct_id)
            known = {o.get("id", "")[:15] for o in cur["opportunities"]}
            add = [o for o in opps if o["Id"][:15] not in known]
            if not add:
                skipped.append({"slug": existing, "acct": acct[:34], "why": "already tracked"})
                continue
            has_primary = any(o.get("primary") for o in cur["opportunities"])
            for o in sorted(add, key=lambda o: -(o.get("Amount") or 0)):
                cur["opportunities"].append({
                    "id": o["Id"], "name": o["Name"], "stage": o.get("StageName"),
                    "close_date": o.get("CloseDate"), "amount": o.get("Amount"),
                    "se_status_assessment": o.get("SE_Forecast__c"),
                    "use_case_primary": o.get("Use_Case_Primary__c"), "status": "open",
                    **({"primary": True} if (o is prim and not has_primary) else {})})
            updated.append({"slug": existing, "acct": acct[:30], "added": len(add),
                            "amt": money(sum(o.get("Amount") or 0 for o in add))})
            if not a.dry_run:
                with open(fp, "w") as f:
                    json.dump(cur, f, indent=2); f.write("\n")
            continue
        slug = slugify(acct)
        d = os.path.join(REPO, slug)
        if os.path.exists(d):
            skipped.append({"slug": slug, "acct": acct[:34], "why": "dir exists, account differs"})
            continue
        sf_json = {
            "account_name": acct, "account_id": acct_id,
            "opportunities": [{
                "id": o["Id"], "name": o["Name"], "stage": o.get("StageName"),
                "close_date": o.get("CloseDate"), "amount": o.get("Amount"),
                "se_status_assessment": o.get("SE_Forecast__c"),
                "use_case_primary": o.get("Use_Case_Primary__c"),
                "status": "open", **({"primary": True} if o is prim else {})}
                for o in sorted(opps, key=lambda o: -(o.get("Amount") or 0))],
        }
        rows = "\n".join(
            f"| {'⭐' if o is prim else ''} | {o['Name']} | {o['Id']} | {o.get('StageName') or ''} "
            f"| Active | {money(o.get('Amount'))} | {o.get('CloseDate') or ''} |"
            for o in sorted(opps, key=lambda o: -(o.get("Amount") or 0)))
        note = (template
                .replace("account:", f"account: {acct}", 1)
                .replace("opp_name:", f"opp_name: \"{prim['Name']}\"", 1)
                .replace("sf_opp_id:", f"sf_opp_id: {prim['Id']}", 1)
                .replace("stage:", f"stage: {prim.get('StageName') or ''}", 1)
                .replace("amount:", f"amount: {prim.get('Amount') or ''}", 1)
                .replace("close_date:", f"close_date: {prim.get('CloseDate') or ''}", 1)
                .replace("use_case:", f"use_case: {prim.get('Use_Case_Primary__c') or ''}", 1)
                .replace("se_status:", f"se_status: {prim.get('SE_Forecast__c') or ''}", 1)
                .replace("updated:", f"updated: {today}", 1)
                .replace("{{ACCOUNT}}", acct)
                .replace("| ⭐ |  |  |  | Active |  |  |", rows)
                .replace("_Current state in 2–3 sentences + the single next action._",
                         f"{acct}: {prim.get('Use_Case_Primary__c') or 'opportunity'}, "
                         f"{prim.get('StageName')}, {money(prim.get('Amount'))}, target close "
                         f"{prim.get('CloseDate')}. Scaffolded from Salesforce {today}; "
                         f"no local engagement docs yet."))
        made.append({"slug": slug, "acct": acct[:30], "opps": len(opps),
                     "amt": money(sum(o.get("Amount") or 0 for o in opps))})
        if a.dry_run:
            continue
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".salesforce.json"), "w") as f:
            json.dump(sf_json, f, indent=2); f.write("\n")
        open(os.path.join(d, "CLAUDE.md"), "w").write(
            "Read [OPP.md](OPP.md) before any work on this account.")
        open(os.path.join(d, "OPP.md"), "w").write(note)

    emit(f"scaffold{' (dry-run)' if a.dry_run else ''} se={ME}",
         toon("created", ["slug", "acct", "opps", "amt"], made),
         toon("adopted", ["slug", "acct", "added", "amt"], updated),
         toon("skipped", ["slug", "acct", "why"], skipped),
         f"\ncreated:{len(made)} adopted:{len(updated)} skipped:{len(skipped)}",
         nxt("python3 automation/gen-pipeline.py", "opp-axi")
         if (made or updated) and not a.dry_run
         else nxt("re-run without --dry-run"))


FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}

# Resellers, SIs and alliance vendors. They sit on many unrelated opportunities at once, so
# their domain identifies a route to market, not an account — treating shi.com as Ivanti's
# own domain made every SHI meeting, including Tesla's, look like Ivanti evidence. These are
# never harvested from notes and never learned from a meeting.
#
# This only stops a partner domain standing in for an account's identity. An internal meeting
# about a customer still matches on the customer's name in the title, which is where it
# belongs. And if a partner is itself an opportunity, its own dir matches its own domain
# through the normal token path — this list does not block that.
#
# Override with OPP_PARTNER_DOMAINS (comma-separated) to add or replace.
PARTNER_DOMAINS = {
    # resellers / distributors
    "shi.com", "cdw.com", "softwareone.com", "wwt.com", "gdt.com", "insight.com",
    "connection.com", "presidio.com", "ahead.com", "sirius.com", "computacenter.com",
    "eplus.com", "trace3.com", "optiv.com", "carahsoft.com",
    # SIs / consultancies
    "deloitte.com", "accenture.com", "infosys.com", "wipro.com", "capgemini.com",
    "kyndryl.com", "hcltech.com", "tcs.com",
    # alliance / hardware / software vendors we co-sell with
    "purestorage.com", "portworx.com", "amd.com", "nvidia.com", "intel.com",
    "supermicro.com", "lenovo.com", "dell.com", "hpe.com", "netapp.com", "arista.com",
    "snuc.com", "everpuredata.com",
}
if os.environ.get("OPP_PARTNER_DOMAINS"):
    _p = {d.strip().lower() for d in os.environ["OPP_PARTNER_DOMAINS"].split(",") if d.strip()}
    PARTNER_DOMAINS = _p if os.environ.get("OPP_PARTNER_DOMAINS_REPLACE") else PARTNER_DOMAINS | _p
SKIP_DIRS = {".git", "node_modules", "site", ".terraform", "__pycache__", ".venv"}


def known_domains(slug):
    """Customer email domains harvested from the opp's own notes — catches domains
    that share no tokens with the account name (blue-widget -> bwtrading.com).

    Partner domains are skipped: a reseller's address in the notes means they are on the
    deal, not that the deal is theirs, and adopting it pulls in every other opportunity
    that partner touches."""
    doms = set()
    for fn in ("OPP.md", ".salesforce.json", "CLAUDE.md"):
        fp = os.path.join(REPO, slug, fn)
        if not os.path.exists(fp):
            continue
        try:
            txt = open(fp, errors="ignore").read()
        except OSError:
            continue
        for m in re.finditer(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", txt):
            d = m.group(1).lower().rstrip(".")
            if d.endswith("spectrocloud.com") or d in FREEMAIL or d in PARTNER_DOMAINS:
                continue
            doms.add(d)
    return doms


def cal_range(g, lo, hi):
    """Calendar events across a date range in ONE call, not one per day."""
    if g == "gws":
        params = json.dumps({"calendarId": "primary", "timeMin": lo + "T00:00:00Z",
                             "timeMax": hi + "T00:00:00Z", "singleEvents": True,
                             "orderBy": "startTime"})
        p = _run([g, "calendar", "events", "list", "--params", params], 240)
        return _parse_json(p.stdout, "calendar").get("items", []) or []
    p = _run([g, "calendar", "events", "--from", lo, "--to", hi, "-j"], 240)
    d = _parse_json(p.stdout, "calendar")
    return (d.get("items", []) if isinstance(d, dict) else d) or []


def gmail_search(g, q, limit, snip=110):
    """One list + one metadata get per hit. Never format=full (~20k tok/message)."""
    p = _run([g, "gmail", "users", "messages", "list",
              "--params", json.dumps({"userId": "me", "q": q, "maxResults": limit})], 240)
    msgs = _parse_json(p.stdout, "gmail list").get("messages", []) or []
    out = []
    for m in msgs:
        gp = {"userId": "me", "id": m["id"], "format": "metadata",
              "metadataHeaders": ["From", "Subject", "Date"]}
        r = _run([g, "gmail", "users", "messages", "get", "--params", json.dumps(gp)], 240)
        d = _parse_json(r.stdout, "gmail get")
        h = {x["name"].lower(): x["value"] for x in (d.get("payload") or {}).get("headers", [])}
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(h.get("date") or "").strftime("%m-%d")
        except Exception:
            dt = (h.get("date") or "")[5:11].strip()
        out.append({"id": m["id"][:12], "date": dt,
                    "from": (h.get("from") or "")[:38], "subject": (h.get("subject") or "")[:56],
                    "snippet": " ".join((d.get("snippet") or "").split())[:snip]})
    return out


def wispr_load():
    """Wispr meetings from the local cache. Wispr sunsetted API export, so the cache is
    filled by /wispr-sync (MCP, once) and read here over CLI — MCP never sits in the hot path.
    The cache lives OUTSIDE the repo: transcripts are verbatim customer speech."""
    fp = os.path.join(WISPR_DIR, "meetings.jsonl")
    recs = []
    if os.path.exists(fp):
        for ln in open(fp, errors="ignore"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    meta = {}
    mp = os.path.join(WISPR_DIR, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp))
        except Exception:
            meta = {}
    return recs, meta


def note_template():
    """The OPP.md seed. Ships inside the package so the tool no longer needs a file
    in the data repo to scaffold one."""
    try:
        from importlib.resources import files
        return files("opp_axi").joinpath("templates/opp-note-template.md").read_text()
    except Exception:
        return ""


def wispr_state(recs, meta):
    """off | ok | stale | absent. `absent` and `ok`-with-no-matches are different facts."""
    if not WISPR_ENABLED:
        return "off"
    if not (meta or {}).get("last_sync"):
        return "absent"
    if wispr_sync_ts(meta)[0] is None:
        return "absent"
    return "stale" if wispr_staleness(meta) else "ok"


def wispr_line(recs, meta, window_end=None):
    """One line describing the wispr source, and a ledger entry when it is a real gap.

    Returns None when wispr is healthy and has nothing to say."""
    st = wispr_state(recs, meta)
    if st == "off":
        return "wispr: disabled (OPP_WISPR=off) — meetings were not searched"
    if st == "absent":
        return gap("wispr", "cache never synced — meetings were NOT searched",
                   "run /wispr-sync in a Claude session, or set OPP_WISPR=off")
    if st == "stale":
        w = wispr_staleness(meta, window_end)
        if w:
            _GAPS.append({"source": "wispr", "reason": "cache stale or partial",
                          "fix": "/wispr-sync"})
        return w
    return None


def wispr_sync_ts(meta):
    """last_sync as a datetime. Tolerates a bare YYYY-MM-DD (legacy) by treating it as
    that day's START — a date-only stamp cannot prove it covers that day's meetings."""
    ls = (meta or {}).get("last_sync")
    if not ls:
        return None, None
    try:
        if len(ls) <= 10:
            return datetime.strptime(ls[:10], "%Y-%m-%d"), True   # date-only: assume 00:00
        return datetime.fromisoformat(ls.replace("Z", "+00:00")).replace(tzinfo=None), False
    except ValueError:
        return None, None


def wispr_staleness(meta, window_end=None):
    """Loud warning beats a silent miss: a stale cache looks exactly like a quiet week.

    Day granularity is not enough. A sync at 12:00 and a meeting that ends at 20:30 the SAME
    day leaves the meeting uncached while the cache still reads "synced today" — which is
    exactly how a real meeting gets reported as having no record. So compare the sync
    TIMESTAMP against the end of the window being asked about, not calendar days.
    """
    ls = (meta or {}).get("last_sync")
    if not ls:
        return "wispr cache: NEVER SYNCED — run /wispr-sync (meetings not searched)"
    ts, date_only = wispr_sync_ts(meta)
    if ts is None:
        return "wispr cache: unreadable last_sync — run /wispr-sync"

    now = datetime.now()
    end = window_end or now
    if end > now:
        end = now

    age_days = (now.date() - ts.date()).days
    if age_days > WISPR_STALE_DAYS:
        return (f"wispr cache: STALE ({age_days}d old, last sync {ts.date()}) — meetings since "
                f"then are MISSING. Run /wispr-sync before trusting a quiet result.")

    # The gap that day-granularity misses: the window extends past the sync instant.
    gap_h = (end - ts).total_seconds() / 3600.0
    if gap_h > WISPR_GAP_HOURS:
        stamp = ts.strftime("%Y-%m-%d") if date_only else ts.strftime("%Y-%m-%d %H:%M")
        hint = " (date-only stamp — treated as 00:00; re-sync to record the time)" if date_only else ""
        return (f"wispr cache: PARTIAL — synced {stamp}{hint}, but you are asking through "
                f"{end.strftime('%Y-%m-%d %H:%M')}, a {gap_h:.0f}h gap. Meetings that ENDED in "
                f"that gap are NOT cached. A miss here is NOT evidence a meeting went unrecorded "
                f"— run /wispr-sync first.")
    return None


def wispr_freshness(meta, window_end=None):
    """Cache freshness as a fixed enum a caller can branch on, instead of grepping a
    sentence: off | absent | stale | partial | ok. Same status vocabulary `doctor` already
    uses for this connector (see _probe_wispr), plus `partial` — the case wispr_state()
    collapses into "stale": the cache is fresh enough by whole days, but the window being
    asked about extends past the sync instant, same rule as wispr_staleness() above.

    Returns (status, detail) where `detail` is a short human reason, "" when status is ok.
    """
    if not WISPR_ENABLED:
        return "off", "OPP_WISPR=off — meetings deliberately not searched"
    ls = (meta or {}).get("last_sync")
    if not ls:
        return "absent", "cache never synced — meetings were NOT searched"
    ts, date_only = wispr_sync_ts(meta)
    if ts is None:
        return "absent", "unreadable last_sync — run /wispr-sync"

    now = datetime.now()
    end = window_end or now
    if end > now:
        end = now

    age_days = (now.date() - ts.date()).days
    if age_days > WISPR_STALE_DAYS:
        return "stale", f"{age_days}d old, last sync {ts.date()}"

    gap_h = (end - ts).total_seconds() / 3600.0
    if gap_h > WISPR_GAP_HOURS:
        stamp = ts.strftime("%Y-%m-%d") if date_only else ts.strftime("%Y-%m-%d %H:%M")
        return "partial", f"synced {stamp}, asked through {end.strftime('%Y-%m-%d %H:%M')} ({gap_h:.0f}h gap)"
    return "ok", ""


def wispr_match(recs, toks, since, limit, snip):
    rows = []
    lo = since.strftime("%Y-%m-%d")
    for m in recs:
        st = (m.get("start") or "")[:10]
        if not st or st < lo:
            continue
        hay = norm_hay((m.get("title") or "") + " " + (m.get("summary") or ""))
        # Same rule as the calendar path: an industry word alone is not evidence. Without
        # this, Elevance Health matched a meeting whose summary said "running healthy" —
        # "health" is >4 chars, so the suffix-tolerant rule let it match "healthy".
        hits = [t for t in toks if text_hit(t, hay)]
        if not any(t not in INDUSTRY for t in hits):
            continue
        rows.append({"date": st[5:], "title": (m.get("title") or "")[:44],
                     "n": len(m.get("attendees") or []),
                     "summary": " ".join((m.get("summary") or "").split())[:snip]})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


def _cmd_wispr_json(a, recs, meta):
    """`wispr --json` — same gap/exit-code contract as the human path (wispr_line() below
    still records the ledger entry that drives E_PARTIAL in main()), machine-readable.

    FIELDS a caller can branch on instead of grepping a sentence:
      cache.status     — off | absent | stale | partial | ok  (see wispr_freshness())
      cache.last_sync  — raw last_sync string from the cache, or null if never synced
      window           — the query window this run asked over
    """
    wispr_line(recs, meta)          # side effect only: records the gap ledger entry
    st = wispr_state(recs, meta)
    since = (datetime.strptime(a.since, "%Y-%m-%d").date() if a.since
             else datetime.now().date() - timedelta(days=a.days))
    status, detail = wispr_freshness(meta)
    cache = {"status": status, "last_sync": (meta or {}).get("last_sync"),
             "cached_count": len(recs), "detail": detail}
    window = {"since": since.isoformat(), "days": a.days if not a.since else None}
    base = {"version": __version__, "window": window, "cache": cache}

    if st in ("off", "absent") or not recs:
        print(json.dumps({**base, "meetings": [],
                          "counts": {"meetings": 0, "matched": 0, "unmatched": 0}}, indent=2))
        return

    idx = opp_index()
    match = build_matcher(idx)
    rows, unmatched = [], 0
    for m in sorted(recs, key=lambda r: r.get("start") or "", reverse=True):
        mst = (m.get("start") or "")[:10]
        if not mst or mst < since.strftime("%Y-%m-%d"):
            continue
        slug = match((m.get("title") or "") + " " + (m.get("summary") or "")[:200], [])
        if not slug:
            unmatched += 1
        rows.append({"date": mst, "title": m.get("title") or "",
                     "n": len(m.get("attendees") or []), "opp": slug or None})
    print(json.dumps({**base, "meetings": rows,
                      "counts": {"meetings": len(rows), "matched": len(rows) - unmatched,
                                 "unmatched": unmatched}}, indent=2))


def cmd_wispr(a):
    recs, meta = wispr_load()
    if getattr(a, "json", False):
        return _cmd_wispr_json(a, recs, meta)
    line = wispr_line(recs, meta)
    st = wispr_state(recs, meta)
    if st in ("off", "absent"):
        # NOT "wispr[0]: (none)". An empty table is a claim that there were no
        # meetings; this is an admission that nobody looked.
        emit(line, nxt("opp-axi doctor") if st == "off" else
             nxt("run /wispr-sync in a Claude session", "OPP_WISPR=off to stop asking"))
        return
    if not recs:
        emit("wispr[0]{date,title,opp}: (none)  # cache synced "
             f"{(meta or {}).get('last_sync', '?')[:10]}, genuinely empty",
             f"\n{line}" if line else "", nxt("opp-axi evidence <slug>"))
        return
    idx = opp_index()
    match = build_matcher(idx)
    since = (datetime.strptime(a.since, "%Y-%m-%d").date() if a.since
             else datetime.now().date() - timedelta(days=a.days))
    rows, unmatched = [], 0
    for m in sorted(recs, key=lambda r: r.get("start") or "", reverse=True):
        st = (m.get("start") or "")[:10]
        if not st or st < since.strftime("%Y-%m-%d"):
            continue
        slug = match((m.get("title") or "") + " " + (m.get("summary") or "")[:200], [])
        if not slug:
            unmatched += 1
        rows.append({"date": st[5:], "title": (m.get("title") or "")[:48],
                     "n": len(m.get("attendees") or []), "opp": slug or "-"})
    emit(f"wispr since={since} cached={len(recs)} last_sync={(meta or {}).get('last_sync','?')[:10]}",
         toon("meetings", ["date", "title", "n", "opp"], rows),
         f"\nmeetings:{len(rows)} matched:{len(rows) - unmatched} unmatched:{unmatched}",
         f"\n{line}" if line else "",
         nxt("opp-axi evidence <slug>", "/wispr-sync to refresh"))


def cmd_evidence(a):
    """What actually happened on an opp — calendar + Zoom recaps + mail + repo activity.

    sweep says WHICH opps need an entry; this says WHAT it should say. Sources are all
    CLI-reachable (gws), so no MCP is involved. Slack and Wispr Flow are NOT covered.
    """
    idx = opp_index()
    slug, oid = resolve(a.ref, idx)
    since = (datetime.strptime(a.since, "%Y-%m-%d").date() if a.since
             else datetime.now().date() - timedelta(days=a.days))
    hi = datetime.now().date() + timedelta(days=1)
    g = gcli()
    if slug in idx:
        _al = idx[slug].get("aliases") or ()
        toks = match_tokens(slug, _acct(idx[slug]), _al)
        vocab = cover_vocab(slug, _acct(idx[slug]), _al)
        expl = explicit_tokens(slug, _al)
        doms = known_domains(slug)
    else:
        # no repo dir (18 of your open opps): fall back to the SF opp Name's account part
        recs = sf_query(f"SELECT Name FROM Opportunity WHERE Id = '{oid}'")
        nm = (recs[0]["Name"] if recs else "")
        slug = re.split(r"\s+-\s+", nm)[0].strip() or oid[:15]
        toks, doms = match_tokens("", slug), set()
        vocab, expl = cover_vocab("", slug), explicit_tokens(slug)
    if not toks and not doms:
        die(f"no name tokens or known domains for {a.ref} — cannot match evidence safely. "
            f"Scaffold a dir with /new-opp, or pass a slug.", E_NOTFOUND)

    # ── calendar: match on known domain first, else on a name token in the title.
    #    Any matched event teaches us new customer domains for the mail query below.
    cal_rows = []
    for ev in cal_range(g, since.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")):
        if (ev.get("status") or "") == "cancelled":
            continue
        summary = ev.get("summary") or ""
        atts = [x.get("email", "") for x in (ev.get("attendees") or [])]
        ext = sorted({e.split("@")[-1].lower() for e in atts
                      if "@" in e and not e.split("@")[-1].lower().endswith("spectrocloud.com")})
        by_dom = any(d in ext for d in doms) or bool(domain_hits(toks, vocab, expl, ext))
        # Title matching is anchored now (`t in summary` matched "health" inside
        # "HealthCare"), and a title hit on an industry word alone is not evidence: every
        # account in the sector owns it. "Centauri Health Solutions" is not an Elevance
        # Health meeting.
        title_toks = [t for t in toks if text_hit(t, norm_hay(summary))]
        by_tok = any(t not in INDUSTRY for t in title_toks)
        if not (by_dom or by_tok):
            continue
        # Learn ONLY from domain evidence. Learning from a title match is what turned one
        # loose row into a contaminated set: a single surname or industry word matched,
        # its attendees' domains were adopted as the account's own, and every later meeting
        # on those domains then matched by_dom — and the poisoned domains went on to drive
        # the Gmail query too.
        if by_dom:
            # Learn customer domains only. A partner on the invite is a route to market, not
            # the account — learning shi.com here is what made Tesla's SHI call read as Ivanti.
            doms |= {d for d in ext if d not in FREEMAIL and d not in PARTNER_DOMAINS}
        st = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
        cal_rows.append({"date": st[5:10], "summary": summary[:50],
                         "n": len(atts), "ext": ";".join(ext[:3])})

    after = since.strftime("%Y/%m/%d")
    dl = sorted(doms)[:4]

    # ── Zoom AI Companion recaps: they arrive as email, so gws reaches them.
    zoom_rows = []
    for m in gmail_search(g, f'from:no-reply@zoom.us after:{after}', a.limit, snip=200):
        subj = m["subject"].lower()
        if not any(text_hit(t, norm_hay(subj)) for t in toks):
            continue
        zoom_rows.append({"date": m["date"], "subject": m["subject"], "recap": m["snippet"]})

    # ── customer mail
    mail_rows = []
    if dl or toks:
        # Search on distinctive tokens only. An industry word as a bare `subject:` term
        # returns every account in the sector — `subject:healthcare` is what filled
        # MedImpact's evidence with GE HealthCare threads. Fall back to one only if the
        # account genuinely has nothing else.
        distinct = [t for t in toks if t not in INDUSTRY]
        subj_toks = sorted(distinct or toks, key=lambda t: (-len(t), t))[:2]
        terms = [f"from:{d} OR to:{d}" for d in dl] +                 [f'subject:{t}' for t in subj_toks]
        q = (f'after:{after} ({" OR ".join(terms)}) -from:no-reply@zoom.us '
             '-subject:"Invitation:" -subject:"Canceled:" -subject:"Cancelled:" '
             '-subject:"Accepted:" -subject:"Declined:" -subject:"Updated invitation"')
        seen = set()
        for m in gmail_search(g, q, a.limit):
            k = (m["date"], m["from"], m["subject"], m["snippet"][:60])
            if k in seen:            # thread replies often share an identical snippet head
                continue
            seen.add(k)
            mail_rows.append({"date": m["date"], "from": m["from"],
                              "subject": m["subject"], "snippet": m["snippet"]})

    # ── repo activity
    repo_rows = []
    base = os.path.join(REPO, slug)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(fp)).date()
            except OSError:
                continue
            if mt >= since:
                repo_rows.append({"mtime": mt.strftime("%m-%d"),
                                  "path": os.path.relpath(fp, REPO)})
    repo_rows.sort(key=lambda r: r["mtime"], reverse=True)
    repo_rows = repo_rows[:a.limit]

    ref = slug if slug in idx else a.ref
    wrecs, wmeta = wispr_load()
    wstate = wispr_state(wrecs, wmeta)
    wispr_rows = ([] if wstate in ("off", "absent")
                  else wispr_match(wrecs, toks, since, a.limit, 400 if a.full else 190))
    wwarn = wispr_line(wrecs, wmeta, hi)

    n = (len(cal_rows) + len(zoom_rows) + len(mail_rows) + len(repo_rows) + len(wispr_rows))
    stamp = datetime.now().strftime("%-m/%-d/%y")
    emit(
        f"evidence {slug} {since}..{hi - timedelta(days=1)} domains:{';'.join(dl) or '-'}",
        (f"wispr: {'disabled' if wstate == 'off' else 'UNAVAILABLE'} — not searched"
         if wstate in ("off", "absent")
         else toon("wispr", ["date", "title", "n", "summary"], wispr_rows)),
        toon("cal", ["date", "summary", "n", "ext"], cal_rows),
        toon("zoom", ["date", "subject", "recap"], zoom_rows),
        toon("mail", ["date", "from", "subject", "snippet"], mail_rows),
        toon("repo", ["mtime", "path"], repo_rows),
        f"\nsignals:{n} cal:{len(cal_rows)} zoom:{len(zoom_rows)} "
        f"mail:{len(mail_rows)} repo:{len(repo_rows)} wispr:{len(wispr_rows)}",
        f"\n{wwarn}" if wwarn else "",
        "\nNOTE: Slack is NOT searched (MCP-only, no CLI path)." if n else
        (f'\nNo signal in range — a genuine "{stamp} {INITIALS}: No new activity this week." '
         f"(Slack is not searched; check it before concluding.)" if not _GAPS else
         "\nNo signal found, but a source above was UNAVAILABLE — this is NOT evidence "
         "of a quiet week. Close the gap before writing 'no new activity'."),
        nxt(f'opp-axi activity {ref} --add "..."', f"opp-axi opp {ref}"))


PATTERNS_PATH = os.environ.get("OPP_PATTERNS") or os.path.expanduser(
    "~/code/ai-lawnmower/patterns/patterns.yaml")


def load_patterns():
    """Patterns are data, not code — adding one must never require editing this file."""
    try:
        import yaml
    except ImportError:
        die("pyyaml not installed; needed to read patterns.yaml", E_ERR)
    if not os.path.exists(PATTERNS_PATH):
        die(f"no patterns file at {PATTERNS_PATH} (set OPP_PATTERNS)", E_NOTFOUND)
    try:
        return yaml.safe_load(open(PATTERNS_PATH)) or []
    except Exception as e:
        die(f"unreadable patterns.yaml: {e}", E_ERR)


def suppression_for(pattern, slug, today):
    """Return the active `suppress` entry for `slug`, or None.

    Same patterns.yaml, same rules as ai-lawnmower's loop/triage.py, so one
    suppression governs both engines and they cannot drift apart. triage.py's
    targets are world paths (`accounts/<slug>`) and ours are bare slugs, so
    match on the trailing segment and both spellings agree.

    An entry whose `until` has passed is inert and the finding comes back --
    a suppression is a deferral, not a delete. PyYAML coerces an unquoted ISO
    date to datetime.date while triage.py's hand-rolled loader yields a str,
    so str() both; for ISO dates a lexical compare is a date compare.
    """
    for entry in (pattern.get("suppress") or []):
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target") or "")
        if target.rsplit("/", 1)[-1] != slug:
            continue
        if str(entry.get("until") or "") >= today:
            return entry
    return None


def repo_touched_since(slug, since):
    """Files under the opp dir modified since `since`, excluding the notes we write ourselves —
    OPP.md changing is our own footprint, not a customer signal.

    `DRAFT-*.md` counts as our own footprint too: writing a draft for Craig to send is
    the SE working the opp, not the customer producing an artifact to review. Measured
    9/2/26 -- dropping a draft into an opp dir raised both an
    customer-artifact-awaiting-review and a thin-sweep-entry finding against an opp
    that has had no contact at all."""
    base = os.path.join(REPO, slug)
    out = []
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn in ("OPP.md", "CLAUDE.md", ".salesforce.json") or fn.startswith("DRAFT-"):
                continue
            fp = os.path.join(root, fn)
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(fp)).date()
            except OSError:
                continue
            if mt >= since:
                out.append(os.path.relpath(fp, base))
    return out


def cmd_triage(a):
    """Signal -> pattern -> proposed work. The middle piece the system was missing.

    Cost-aware by construction: one SOQL query establishes sweep state for every open opp, and
    the expensive per-opp evidence fan-out runs ONLY for opps a pattern already flagged.
    """
    since = (datetime.strptime(a.since, "%Y-%m-%d").date() if a.since
             else datetime.now().date() - timedelta(days=a.days))
    patterns = {p["id"]: p for p in load_patterns()}
    idx = opp_index()
    i2s = id_to_slug(idx)

    recs = sf_query(
        "SELECT Id,Name,Amount,CloseDate,StageName,Sales_Engineer_Overview__c,POV_Pass__c,"
        f"Hands_on_Eval_POV_URL__c FROM Opportunity WHERE {OPEN_WHERE} ORDER BY CloseDate")

    findings, scanned, deep = [], 0, 0
    suppressed = []
    today_iso = datetime.now().date().isoformat()
    deep_done = set()

    seen = set()

    def add(pid, slug, why, detail):
        # An account with several open opps is scanned once per opp; the finding is about the
        # account, so emit it once. Without this, a multi-opp account reported twice.
        if (pid, slug) in seen:
            return
        seen.add((pid, slug))
        p = patterns.get(pid, {})
        entry = suppression_for(p, slug, today_iso)
        if entry is not None:
            suppressed.append({"pattern": pid, "slug": slug,
                               "until": str(entry.get("until")),
                               "why": " ".join(str(entry.get("why") or "").split())[:120]})
            return
        findings.append({"pattern": pid, "slug": slug, "priority": p.get("priority", 3),
                         "why": why, "work": " ".join((p.get("work") or "").split())[:150],
                         "detail": detail})

    for r in recs:
        scanned += 1
        slug = i2s.get(r["Id"][:15], "")
        act = r.get("Sales_Engineer_Overview__c")
        d, who = entry_date(act), entry_author(act)
        thin = bool(d and d >= since and is_placeholder(act))

        # --- cheap, SF-only patterns first -------------------------------
        if (r.get("StageName") or "") == "Prove Value" and not (r.get("Hands_on_Eval_POV_URL__c") or "").strip():
            add("entering-prove-value", slug or r["Id"][:15],
                "stage=Prove Value with no validation-plan URL", "Hands_on_Eval_POV_URL__c empty")

        # --- narrow to opps worth spending evidence on -------------------
        needs_look = thin or not d or (d and d < since) or (who and who != INITIALS)
        if not (needs_look and slug):
            continue
        if deep >= a.max_deep or slug in deep_done:
            continue
        deep_done.add(slug)
        deep += 1

        touched = repo_touched_since(slug, since)
        if touched:
            add("customer-artifact-awaiting-review", slug,
                f"{len(touched)} new file(s) in the opp dir since {since}",
                ", ".join(touched[:3]))

        wrecs, wmeta = wispr_load()
        toks = match_tokens(slug, _acct(idx.get(slug, {})), (idx.get(slug) or {}).get("aliases") or ())
        meetings = wispr_match(wrecs, toks, since, 5, 90) if toks else []
        if meetings and (not d or d < since):
            add("meeting-without-record", slug,
                f"{len(meetings)} meeting(s) since {since} with no SE Activity entry after them",
                meetings[0]["title"][:70])

        if thin:
            sig = len(touched) + len(meetings)
            if sig:
                add("thin-sweep-entry", slug,
                    f"entry is a placeholder but {sig} signal(s) exist", first_line(act)[:60])

    findings.sort(key=lambda f: (f["priority"], f["slug"]))
    _wr, _wm = wispr_load()
    stale = wispr_line(_wr, _wm)

    if getattr(a, "json", False):
        # FIELDS a caller can branch on instead of regexing "suppressed X for Y until":
        # `suppressed[]` states WHAT is suppressed (pattern), for WHOM (slug), UNTIL when.
        wstatus, wdetail = wispr_freshness(_wm)
        print(json.dumps({
            "version": __version__, "since": since.isoformat(), "se": ME,
            "scanned": scanned, "deep": deep,
            "findings": findings,
            "suppressed": suppressed,
            "wispr_cache": {"status": wstatus, "last_sync": (_wm or {}).get("last_sync"),
                            "detail": wdetail},
        }, indent=2))
    else:
        emit(f"triage since={since} se={ME} patterns={len(patterns)}",
             toon("findings", ["pattern", "slug", "why", "detail"], findings),
             f"\nscanned:{scanned} deep:{deep} findings:{len(findings)}"
             + (f" suppressed:{len(suppressed)}" if suppressed else ""),
             # Withheld, never silent: a suppression nobody can see is indistinguishable
             # from a pattern that quietly stopped working.
             ("\n" + "\n".join(
                 f"  suppressed {f['pattern']} for {f['slug']} until {f['until']}: {f['why']}"
                 for f in suppressed) if suppressed else ""),
             f"\n{stale}" if stale else "",
             "\nNOTE: Slack is not searched. Patterns are data — edit patterns.yaml, not opp-axi.",
             nxt("opp-axi triage --push  # queue these to the inbox",
                 "opp-axi evidence <slug>") if findings else nxt("nothing to queue"))

    if a.push and findings:
        _push_to_inbox(findings)


def _push_to_inbox(findings):
    """Hand findings to the dispatch inbox. Each becomes one item, carrying its reasoning so the
    morning report can say WHY without re-deriving it."""
    cli = shutil.which("dispatch")
    if not cli:
        print("  (dispatch CLI not on PATH — not queued)", file=sys.stderr)
        return
    # What is already open, so a finding is not filed twice. Without this every nightly
    # run re-files everything it still sees: measured 8/28/26, 12 of 22 open items were
    # duplicates -- four findings sitting there four times each. Craig, reading the
    # board: "There are so many failed in flight issues... I can't imagine all the
    # backlog items actually failed." They had not failed. They were the same four
    # things, four times, and the noise made the board unreadable.
    open_keys = set()
    seen = _run([cli, "ls", "--limit", "300", "--full"], timeout=60)
    if seen.returncode == 0:
        for line in (seen.stdout or "").split("\n"):
            m = re.match(r"\s+[0-9A-HJKMNP-TV-Z]{26},([a-z-]+),[^,]*,(.*)", line)
            # An item Craig has finished with is not a reason to stay silent if the
            # signal fires again -- only something still OPEN is.
            if m and m.group(1) in ("new", "routed", "working", "needs-input"):
                t = re.match(r'"?\[([a-z-]+)\]\s*([a-z0-9-]+):', m.group(2).strip())
                if t:
                    open_keys.add((t.group(1), t.group(2)))
    else:
        # Could not read the inbox. File nothing rather than risk another round of
        # duplicates: a missed finding reappears tomorrow, a duplicate never leaves.
        print("  (could not read the inbox — not queueing, to avoid duplicates)",
              file=sys.stderr)
        return

    ok = dup = 0
    for f in findings:
        if (f["pattern"], f["slug"]) in open_keys:
            dup += 1
            continue
        body = (f"[{f['pattern']}] {f['slug']}: {f['why']}. {f['work']}"
                + (f" ({f['detail']})" if f["detail"] else ""))
        p = _run([cli, "add", body], timeout=60)
        if p.returncode == 0:
            ok += 1
    print(f"\nqueued:{ok}/{len(findings)} to the inbox"
          + (f" ({dup} already open)" if dup else ""))


FIELDS = {
    "write": [
        ("SE Forecast", "SE_Forecast__c", "picklist", "Favorable|Needs Attention|At Risk"),
        ("SE Activity", "Sales_Engineer_Overview__c", "textarea 100k", "PREPEND; M/D/YY CS:"),
        ("Tech Risk Status", "Tech_Risk_Status__c", "picklist", "'\U0001f534 High'|'\U0001f7e2 Low' ONLY (emoji is the value)"),
        ("Tech Risk Rational", "Tech_Risk_Rational__c", "multipicklist", "semicolon-separated"),
        ("Tech Win", "POV_Pass__c", "boolean", "auto-stamps Technical_Win_Date__c"),
        ("Solution Validated", "Solution_Validated__c", "boolean", ""),
        ("POV Required", "Hands_on_Eval_Required__c", "boolean", ""),
        ("POV Start", "Hands_on_Eval_ActStartDate__c", "date", "YYYY-MM-DD"),
        ("POV Est Start", "Hands_on_Eval_EstStartDate__c", "date", "YYYY-MM-DD"),
        ("POV Est End", "Hands_on_Eval_EstEndDate__c", "date", "YYYY-MM-DD"),
        ("Hands-on Eval By", "Hands_on_Eval_By__c", "picklist", "Customer|Partner|Spectro Cloud"),
        ("Validation Plan URL", "Hands_on_Eval_POV_URL__c", "textarea 512", "docs-site URL"),
        ("Technical Notes", "Technical_Notes__c", "textarea 32k", "Lightning page only"),
        ("Integration Stack", "Integration_Stack__c", "textarea 100k", ""),
        ("Secondary Envs", "Secondary_Environment_s__c", "multipicklist", "AWS|Azure|GCP|Nutanix AHV|OpenStack|vCloud Director|VMware|Bare Metal|Other"),
    ],
    "never": [
        ("Next Steps", "Next_Steps__c", "AE-owned"),
        ("Stage", "StageName", "AE-owned"),
        ("Amount", "Amount", "AE-owned"),
        ("Close Date", "CloseDate", "AE-owned"),
        ("Current State", "Current_Technical_State__c", "AE-owned, read-only"),
        ("Future State", "Desired_Technical_State__c", "AE-owned, read-only"),
        ("Use Case Primary", "Use_Case_Primary__c", "AE-owned, read-only"),
    ],
    "dead": [
        ("SE Activity", "SE_Activity__c", "dead twin, 0 populated"),
        ("POV Required", "POV_Required__c", "dead twin, 0 populated"),
        ("POV Start Date", "POV_Start_Date__c", "dead twin, 0 populated"),
        ("Tech Win", "Tech_Approval__c", "dead twin, 0 populated"),
        ("Validation Plan", "Technical_Validation_Plan__c", "on NEITHER UI surface — invisible"),
    ],
}


def cmd_fields(a):
    """The SE field reference, on demand — so it stops costing ~2.8k tok of always-on CLAUDE.md."""
    sec = a.section
    out = []
    if sec in (None, "write"):
        out.append(toon("writable", ["label", "api", "type", "values"],
                        [dict(zip(("label", "api", "type", "values"), f)) for f in FIELDS["write"]]))
    if sec in (None, "never"):
        out.append(toon("neverWrite", ["label", "api", "why"],
                        [dict(zip(("label", "api", "why"), f)) for f in FIELDS["never"]]))
    if sec in (None, "dead"):
        out.append(toon("deadTwins", ["label", "api", "why"],
                        [dict(zip(("label", "api", "why"), f)) for f in FIELDS["dead"]]))
    out.append("\nrules: REST PATCH only (sf data update -v writes null for emoji, mangles newlines) "
               "| prepend SE Activity, never overwrite | write only where "
               f"SA_Assignment_Oppty__c='{ME}' | always verify after write")
    emit(*out, nxt("opp-axi fields write|never|dead"))


# ── cli ────────────────────────────────────────────────────────────────────
# ── connectors ───────────────────────────────────────────────
# The whole contract in one table. `need` is the only thing that decides what a
# failure means:
#   required — the tool cannot do its job. A command that needs it REFUSES (E_ERR).
#   optional — the tool works without it. A command that would have used it says
#              UNAVAILABLE and exits E_PARTIAL. It never renders as an empty result.
#
# This is the same rule loop/doctor.sh applies to the platform: a check that
# cannot run must refuse, never pass. "The gateway did not answer" must never
# render as "no results".

def _probe_sf():
    if not shutil.which("sf"):
        return "down", "sf not on PATH", "npm i -g @salesforce/cli"
    p = _run(["sf", "org", "display", "-o", ORG, "--json"], timeout=45)
    if p.returncode != 0:
        return "down", f"org {ORG} not authenticated", f"sf org login web --alias {ORG}"
    try:
        d = json.loads(p.stdout or "{}").get("result") or {}
    except json.JSONDecodeError:
        return "down", "sf returned non-JSON", f"sf org display -o {ORG}"
    who = d.get("username") or "?"
    if (d.get("connectedStatus") or "").lower() not in ("connected", ""):
        return "down", f"{who}: {d.get('connectedStatus')}", f"sf org login web --alias {ORG}"
    return "ok", f"{who} (org {ORG})", ""


def _probe_google():
    c = shutil.which("gws") or shutil.which("gog")
    if not c:
        return "down", "neither gws nor gog on PATH", "install gws, then: source ~/.profile"
    return "ok", f"{os.path.basename(c)} on PATH", ""


def _probe_wispr():
    if not WISPR_ENABLED:
        return "off", "OPP_WISPR=off — meetings deliberately not searched", ""
    recs, meta = wispr_load()
    st = wispr_state(recs, meta)
    if st == "absent":
        return "absent", f"no synced cache at {WISPR_DIR}", "/wispr-sync, or OPP_WISPR=off"
    if st == "stale":
        return "stale", (wispr_staleness(meta) or "stale")[:110], "/wispr-sync"
    return "ok", f"{len(recs)} meetings, last sync {(meta or {}).get('last_sync', '?')[:16]}", ""


def _probe_repo():
    if not os.path.isdir(REPO):
        return "down", f"no opp repo at {REPO}", "set OPP_REPO=/path/to/customer-opportunities"
    n = len(glob.glob(os.path.join(REPO, "*", ".salesforce.json")))
    if not n:
        return "down", f"{REPO} has no */.salesforce.json", "opp-axi scaffold"
    return "ok", f"{n} opp dirs at {REPO}", ""


def _probe_patterns():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return "absent", "pyyaml not installed — triage cannot run", "pip install pyyaml"
    if not os.path.exists(PATTERNS_PATH):
        return "absent", f"no patterns file at {PATTERNS_PATH}", "set OPP_PATTERNS=<path>"
    return "ok", PATTERNS_PATH, ""


def _probe_dispatch():
    if not shutil.which("dispatch"):
        return "absent", "dispatch not on PATH — triage --push cannot queue", "install dispatch"
    return "ok", "dispatch on PATH", ""


CONNECTORS = [
    ("salesforce", "required", "opportunities, SE Activity, account data",   _probe_sf),
    ("google",     "required", "calendar and gmail evidence",                _probe_google),
    ("opp-repo",   "required", "the local opp folders and .salesforce.json", _probe_repo),
    ("wispr",      "optional", "meeting transcripts for evidence/triage",    _probe_wispr),
    ("patterns",   "optional", "triage rules (patterns.yaml + pyyaml)",      _probe_patterns),
    ("dispatch",   "optional", "queueing triage findings to the inbox",      _probe_dispatch),
]

OK_STATES = ("ok", "off")


def _src(name):
    return "env" if os.getenv(name) else "default"


def cmd_doctor(a):
    """What is configured, what is not, and exactly what fixes each gap.

    Exit 0 when every REQUIRED connector is ok, E_ERR when one is not. An optional
    connector that is absent is reported but does not fail the run — that is the
    difference between "you cannot use this tool" and "you will get less from it"."""
    rows, results = [], []
    for name, need, why, probe in CONNECTORS:
        try:
            status, detail, fix = probe()
        except Exception as e:                  # a probe must never crash the doctor
            status, detail, fix = "down", f"probe raised {type(e).__name__}: {e}"[:120], ""
        results.append({"name": name, "need": need, "status": status,
                        "detail": detail, "fix": fix, "why": why})
        rows.append({"name": name, "need": need, "status": status,
                     "detail": detail + (f" | fix: {fix}" if fix else "")})

    cfg = [
        {"var": "OPP_REPO",      "value": REPO,     "source": _src("OPP_REPO")},
        {"var": "SF_ORG",        "value": ORG,      "source": _src("SF_ORG")},
        {"var": "OPP_SE",        "value": ME,       "source": _src("OPP_SE")},
        {"var": "OPP_INITIALS",  "value": INITIALS, "source": _src("OPP_INITIALS")},
        {"var": "OPP_WISPR",     "value": "on" if WISPR_ENABLED else "off",
         "source": _src("OPP_WISPR")},
        {"var": "OPP_WISPR_DIR", "value": WISPR_DIR, "source": _src("OPP_WISPR_DIR")},
        {"var": "OPP_PATTERNS",  "value": PATTERNS_PATH, "source": _src("OPP_PATTERNS")},
    ]

    bad = [r for r in results if r["need"] == "required" and r["status"] not in OK_STATES]
    soft = [r for r in results if r["need"] == "optional" and r["status"] not in OK_STATES]

    if getattr(a, "json", False):
        print(json.dumps({"version": __version__, "connectors": results, "config": cfg,
                          "required_down": [r["name"] for r in bad],
                          "optional_degraded": [r["name"] for r in soft]}, indent=2))
        sys.exit(E_ERR if bad else E_OK)

    emit(
        f"opp-axi {__version__} doctor",
        toon("connectors", ["name", "need", "status", "detail"], rows),
        toon("config", ["var", "value", "source"], cfg),
        "\n" + (f"REFUSING: {len(bad)} required connector(s) down — "
                 + ", ".join(r["name"] for r in bad) if bad
                 else "all required connectors ok"),
        (f"degraded: {', '.join(r['name'] for r in soft)} — commands that would use "
         f"these report UNAVAILABLE and exit {E_PARTIAL}, never an empty result." if soft else ""),
        nxt("opp-axi opps", "opp-axi doctor --json"))
    sys.exit(E_ERR if bad else E_OK)


def main():
    p = argparse.ArgumentParser(prog="opp-axi", description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("opps", help="list open opps (TOON)")
    s.add_argument("--at-risk", action="store_true")
    s.add_argument("--full", action="store_true", help="untruncated names")
    s.set_defaults(fn=cmd_opps)

    s = sub.add_parser("opp", help="one opp; activity truncated to latest entry")
    s.add_argument("ref", help="dir slug or SF opp id")
    s.add_argument("--full", action="store_true", help="full SE Activity + notes")
    s.set_defaults(fn=cmd_opp)

    s = sub.add_parser("sweep", help="weekly-sweep coverage: who is missing an entry")
    s.add_argument("--since", help="YYYY-MM-DD (default: 7 days ago)")
    s.add_argument("--secondary", action="store_true", help="also list opps where you are secondary SE (read-only)")
    s.add_argument("--closed", action="store_true", help="also list recently closed opps")
    s.add_argument("--closed-days", type=int, default=30)
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("activity", help="prepend an SE Activity entry (REST PATCH, verified)")
    s.add_argument("ref")
    s.add_argument("--add", required=True, help="entry text; date+initials auto-stamped")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_activity)

    s = sub.add_parser("cal", help="calendar, normalized + opp-matched")
    s.add_argument("--date", default="today", help="today|tomorrow|YYYY-MM-DD")
    s.set_defaults(fn=cmd_cal)

    s = sub.add_parser("mail", help="gmail search + headers in one shot")
    s.add_argument("query", help="gmail q: syntax, e.g. 'subject:<account> newer_than:2d'")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--full", action="store_true", help="longer snippets")
    s.set_defaults(fn=cmd_mail)

    s = sub.add_parser("evidence", help="what happened on an opp: cal + zoom recaps + mail + repo")
    s.add_argument("ref")
    s.add_argument("--since", help="YYYY-MM-DD (default: --days ago)")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=8, help="max rows per source")
    s.add_argument("--full", action="store_true", help="longer wispr summaries")
    s.set_defaults(fn=cmd_evidence)

    s = sub.add_parser("scaffold", help="create repo dirs for open opps that have none")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_scaffold)

    s = sub.add_parser("wispr", help="cached Wispr meetings, matched to opps")
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--json", action="store_true",
                  help="cache freshness (status enum, last_sync, window) as JSON")
    s.set_defaults(fn=cmd_wispr)

    s = sub.add_parser("triage", help="signal -> pattern -> proposed work")
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--max-deep", type=int, default=15,
                   help="cap on opps that get the expensive evidence fan-out")
    s.add_argument("--push", action="store_true", help="queue findings to the dispatch inbox")
    s.add_argument("--json", action="store_true",
                  help="findings + suppression state (pattern, slug, until) as JSON")
    s.set_defaults(fn=cmd_triage)

    s = sub.add_parser("doctor", help="what is configured, what is missing, how to fix it")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("fields", help="SE field reference (on demand)")
    s.add_argument("section", nargs="?", choices=["write", "never", "dead"])
    s.set_defaults(fn=cmd_fields)

    a = p.parse_args()
    if not a.cmd:
        cmd_overview(a)
    else:
        a.fn(a)
    # A command that finished while a declared source went unread did NOT do what it
    # was asked. Say so in the exit code, not only in stdout that nobody parses.
    if _GAPS:
        sys.stdout.flush()
        print("\nincomplete: " + "; ".join(
            f"{g['source']} ({g['reason']})" for g in _GAPS), file=sys.stderr)
        sys.exit(E_PARTIAL)


def run():
    """Console-script / zipapp entry point. Signal handling lives here so that both
    `python -m opp_axi` and the installed script behave identically."""
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    run()
