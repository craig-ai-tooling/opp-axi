"""Customer-folder state in the Customer Success shared drive.

The convention this implements: every open opp gets a folder in the Customer
Success drive's `Customers` directory, holding the technical validation plan
(which is also hyperlinked on the opp) and the Success Plan Criteria doc that
Customer Success picks up at handover.

Sales engineers are usually readers on that drive rather than members of it, so
`canAddChildren` is false and the folder itself gets created by a content manager
days or weeks after the opp needs one. That asymmetry is the whole reason this
module exists: the check has to be safe to run before the folder exists, report
that as an ordinary state rather than an error, and fill only the gap on a later
run once somebody has made it.

The `Customers` folder is resolved by name at runtime rather than carried as an
id, so nothing here hardcodes one tenant's Drive layout. `OPP_CS_FOLDER`
short-circuits the lookup when the names differ.

Everything takes a `gws` callable so the Drive calls can be faked in tests.
`gws(args, params, body=None)` runs one Google CLI verb and returns parsed JSON.
"""
from __future__ import annotations

import os
import re

# Resolved by name so no internal folder id ships with the tool.
CS_DRIVE_NAME = os.environ.get("OPP_CS_DRIVE", "Customer Success")
CUSTOMERS_NAME = os.environ.get("OPP_CS_CUSTOMERS", "Customers")

FOLDER_MIME = "application/vnd.google-apps.folder"

# What JV asks for in each customer folder. `kind` is the stable key callers
# switch on; `label` is what a human reads in the TOON row.
VALIDATION_PLAN = "validation-plan"
SUCCESS_PLAN = "success-plan"

SUCCESS_PLAN_PREFIX = "Success Plan Criteria - "

_DRIVE_ID = re.compile(r"/(?:document|spreadsheets|presentation|file)/d/([A-Za-z0-9_-]{25,})")
_FOLDER_ID = re.compile(r"/folders/([A-Za-z0-9_-]{25,})")


def norm(name):
    """Fold an account or folder name to a comparison key.

    The live Customers listing has `Chipotle `, `Sanofi ` and `Sientis (Nokia)`,
    so trailing space and punctuation cannot be load-bearing.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def plan_file_ids(plan_url):
    """Drive file ids referenced by an opp's Validation Plan URL field.

    The field is free text holding several labelled links, so this pulls every
    /d/<id> it can see and keeps document order -- first link wins as the
    validation plan, which matches how the field is written.
    """
    seen, out = set(), []
    for m in _DRIVE_ID.finditer(plan_url or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def ls(gws, parent):
    """Direct children of a folder, trashed excluded."""
    out, page = [], None
    while True:
        params = {"q": f"'{parent}' in parents and trashed = false",
                  "supportsAllDrives": True, "includeItemsFromAllDrives": True,
                  "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 200}
        if page:
            params["pageToken"] = page
        d = gws(["drive", "files", "list"], params) or {}
        out.extend(d.get("files") or [])
        page = d.get("nextPageToken")
        if not page:
            return out


def get(gws, fid, fields="id,name,mimeType,capabilities"):
    return gws(["drive", "files", "get"],
               {"fileId": fid, "supportsAllDrives": True, "fields": fields}) or {}


class DriveLayoutError(RuntimeError):
    """The Customers folder could not be located. Never rendered as `no folders`."""


def resolve_customers(gws):
    """Drive id of the `Customers` folder, looked up by name.

    `OPP_CS_FOLDER` wins outright. Otherwise: find the shared drive by name, then
    its `Customers` child. A failure here raises rather than returning an empty
    listing -- an unreachable source must never read as "this account has no
    customer folders".
    """
    env = os.environ.get("OPP_CS_FOLDER")
    if env:
        return env
    d = gws(["drive", "drives", "list"], {"pageSize": 100}) or {}
    drives = [x for x in (d.get("drives") or [])
              if (x.get("name") or "").strip().lower() == CS_DRIVE_NAME.lower()]
    if not drives:
        raise DriveLayoutError(
            f"no shared drive named {CS_DRIVE_NAME!r}; set OPP_CS_FOLDER to the "
            "Customers folder id, or OPP_CS_DRIVE to the drive name")
    root = drives[0]["id"]
    # Customers is not necessarily a direct child of the drive root -- on the live
    # drive it sits one level down, under `Customer Related `. Search the whole
    # drive by name rather than assuming a depth.
    q = (f"name = '{CUSTOMERS_NAME}' and mimeType = '{FOLDER_MIME}' "
         "and trashed = false")
    found = (gws(["drive", "files", "list"],
                 {"q": q, "driveId": root, "corpora": "drive",
                  "supportsAllDrives": True, "includeItemsFromAllDrives": True,
                  "fields": "files(id,name)", "pageSize": 10}) or {}).get("files") or []
    if len(found) == 1:
        return found[0]["id"]
    raise DriveLayoutError(
        f"shared drive {CS_DRIVE_NAME!r} has "
        f"{'no' if not found else len(found)} {CUSTOMERS_NAME!r} folder(s); "
        "set OPP_CS_FOLDER to the folder id")


def folder_match(gws, account, customers=None, children=None):
    """(state, folder) for `account`'s customer folder.

    state is `found`, `none` when the Customers listing genuinely has no folder
    for this account, or `ambiguous` when two or more could be it. Those last two
    are not the same fact and must not be collapsed: `none` means somebody still
    has to create the folder, `ambiguous` means a human has to say which of
    `Purple Team` and `Purple Team Software` this opp belongs in. Filing a
    customer's plan into the wrong customer's folder is the failure this guards.

    Exact normalized match wins. A single prefix match is accepted after that --
    `Rehrig` against a `Rehrig Pacific` folder is one customer.
    """
    key = norm(account)
    if not key:
        return "none", None
    kids = children if children is not None else ls(gws, customers or resolve_customers(gws))
    folders = [f for f in kids if f.get("mimeType") == FOLDER_MIME]
    for f in folders:
        if norm(f.get("name")) == key:
            return "found", f
    near = [f for f in folders
            if norm(f.get("name")).startswith(key) or key.startswith(norm(f.get("name")))]
    if len(near) == 1:
        return "found", near[0]
    return ("ambiguous" if near else "none"), None


def find_folder(gws, account, customers=None, children=None):
    """The customer folder for `account`, or None. See folder_match for the
    distinction between absent and ambiguous."""
    return folder_match(gws, account, customers, children)[1]


def _by_id(files):
    return {f.get("id"): f for f in files}


def required_state(gws, account, plan_url, folder, contents=None):
    """Which required artifacts are in `folder`, and where the missing ones live.

    Returns a list of rows: kind, label, state, id. `state` is `present` when the
    artifact is already in the folder, `copyable` when it exists elsewhere in
    Drive and can be copied in, and `absent` when it does not exist at all and a
    human has to write it.
    """
    kids = contents if contents is not None else ls(gws, folder["id"])
    here = _by_id(kids)
    names = {norm(f.get("name")): f for f in kids}
    rows = []

    plan_ids = plan_file_ids(plan_url)
    plan_id = plan_ids[0] if plan_ids else None
    if plan_id and plan_id in here:
        rows.append({"kind": VALIDATION_PLAN, "state": "present", "id": plan_id,
                     "name": here[plan_id].get("name", "")})
    elif plan_id:
        meta = get(gws, plan_id, "id,name,mimeType")
        rows.append({"kind": VALIDATION_PLAN, "state": "copyable", "id": plan_id,
                     "name": meta.get("name", "")})
    else:
        rows.append({"kind": VALIDATION_PLAN, "state": "absent", "id": "",
                     "name": "no Drive link in Hands_on_Eval_POV_URL__c"})

    want = SUCCESS_PLAN_PREFIX + account
    hit = names.get(norm(want))
    if hit:
        rows.append({"kind": SUCCESS_PLAN, "state": "present", "id": hit["id"],
                     "name": hit.get("name", "")})
    else:
        found = search_by_name(gws, want)
        if found:
            rows.append({"kind": SUCCESS_PLAN, "state": "copyable",
                         "id": found["id"], "name": found.get("name", "")})
        else:
            rows.append({"kind": SUCCESS_PLAN, "state": "absent", "id": "",
                         "name": want})
    return rows


def search_by_name(gws, name):
    """A non-trashed Drive file with exactly this name, or None.

    Used to find an artifact that already exists somewhere else -- typically
    parked in the AE's or the SE's own folder while the customer folder is
    still missing.
    """
    q = "name = '{}' and trashed = false".format((name or "").replace("'", "\\'"))
    d = gws(["drive", "files", "list"],
            {"q": q, "supportsAllDrives": True, "includeItemsFromAllDrives": True,
             "fields": "files(id,name,mimeType)", "pageSize": 10}) or {}
    files = d.get("files") or []
    return files[0] if files else None


def copy_into(gws, fid, folder_id, name=None):
    """Copy a Drive file into a folder. Returns the new file."""
    body = {"parents": [folder_id]}
    if name:
        body["name"] = name
    return gws(["drive", "files", "copy"],
               {"fileId": fid, "supportsAllDrives": True,
                "fields": "id,name,mimeType"}, body) or {}


def can_write(gws, folder_id, meta=None):
    """Whether this account may add children to the folder."""
    m = meta if meta is not None else get(gws, folder_id)
    return bool((m.get("capabilities") or {}).get("canAddChildren"))
