"""`triage --push` remembers what a session already closed.

It used to skip a finding only while an item for the same (pattern, slug) was OPEN. A session
that closes the item without changing the signal (the woven sessions: "match rejected", no
SE Activity, nothing to write) was followed 30 minutes later by the identical finding.
Measured 10/3/26: woven filed at 20:53Z, 21:38Z and 22:08Z.

Now a finding is also skipped when a DONE item from the last seven days carries the same
(pattern, slug, evidence). `failed` never blocks a re-file. Items closed before evidence tags
existed are matched on the trailing `(...)` the old code wrote.

`dispatch` is a fake on PATH that speaks the real `ls --output json` / `add` interface, keeps
its items in a file and logs every call, so nothing here touches the real inbox.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli  # noqa: E402

FAKE = '''#!__PY__
import json, os, re, sys
from datetime import datetime, timedelta, timezone


def ts(text):
    # 3.10's fromisoformat wants a 3- or 6-digit fraction; dispatch trims zeros.
    text = re.sub(r"\\.(\\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6],
                  text.replace("Z", "+00:00"), count=1)
    return datetime.fromisoformat(text)


args = sys.argv[1:]
state = os.environ["FAKE_DISPATCH_DIR"]
with open(os.path.join(state, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\\n")
items_path = os.path.join(state, "items.json")
if args[:1] == ["ls"]:
    if os.path.exists(os.path.join(state, "ls-fails")):
        sys.stderr.write("dispatch: error: secret_unavailable\\n")
        sys.exit(1)
    items = json.load(open(items_path))
    want = [args[i + 1] for i, a in enumerate(args) if a == "--status"]
    if want and os.path.exists(os.path.join(state, "status-fails")):
        sys.stderr.write("dispatch: error: invalid_status\\n")
        sys.exit(1)
    if want:                                  # the server filters on status ...
        items = [i for i in items if i["status"] in want]
    if "--since" in args:                     # ... and on creation time
        hours = int(args[args.index("--since") + 1].rstrip("h"))
        cut = datetime.now(timezone.utc) - timedelta(hours=hours)
        items = [i for i in items if ts(i["created_at"]) >= cut]
    print(json.dumps({"items": items, "count": len(items)}))
elif args[:1] == ["add"]:
    items = json.load(open(items_path))
    items.append({"id": "01FAKE%04d" % len(items), "body": args[1], "source": "cli", "status": "new",
                  "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  "updated_at": "", "meta": {}, "idempotency_key": ""})
    json.dump(items, open(items_path, "w"))
    with open(os.path.join(state, "added.txt"), "a") as fh:
        fh.write(args[1] + "\\n")
    print(items[-1]["id"])
'''


def finding(slug="woven", pattern="meeting-without-record", evidence="wispr:8f107d00",
            detail="Wispr 8f107d00 9/28 Craig/Team Speed Target Opp Weekly"):
    return {"pattern": pattern, "slug": slug, "priority": 1, "work": "Log the meeting.",
            "why": "1 meeting(s) since 2026-09-26 with no SE Activity entry after them",
            "detail": detail, "evidence": evidence}


def body(f, tag=True, detail=None):
    """The body _push_to_inbox files for finding F. tag=False is the pre-evidence format."""
    d = f["detail"] if detail is None else detail
    b = f"[{f['pattern']}] {f['slug']}: {f['why']}. {f['work']} ({d})"
    return b + f" [evidence:{f['evidence']}]" if tag else b


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        path = os.path.join(self.dir.name, "dispatch")
        with open(path, "w") as fh:
            fh.write(FAKE.replace("__PY__", sys.executable))
        os.chmod(path, 0o755)
        env = mock.patch.dict(os.environ, {"PATH": self.dir.name + os.pathsep + os.environ.get("PATH", ""),
                                           "FAKE_DISPATCH_DIR": self.dir.name})
        env.start()
        self.addCleanup(env.stop)
        self.set_items([])

    def set_items(self, items):
        with open(os.path.join(self.dir.name, "items.json"), "w") as fh:
            json.dump(items, fh)

    def get_items(self):
        with open(os.path.join(self.dir.name, "items.json")) as fh:
            return json.load(fh)

    @staticmethod
    def item(text, status, hours_old=1.0):
        created = datetime.now(timezone.utc) - timedelta(hours=hours_old)
        return {"id": "01ITEM", "body": text, "source": "cli", "status": status,
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "updated_at": "",
                "meta": {}, "idempotency_key": ""}

    def push(self, *findings):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._push_to_inbox(list(findings))
        return rc, out.getvalue(), err.getvalue()

    def added(self):
        path = os.path.join(self.dir.name, "added.txt")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def calls(self):
        with open(os.path.join(self.dir.name, "calls.jsonl")) as fh:
            return [json.loads(ln) for ln in fh]


class OpenAndHandled(InboxCase):
    def test_G_an_open_item_for_the_same_pattern_and_slug_is_skipped(self):
        f = finding()
        # Different evidence on purpose: open means open, whatever it was about.
        self.set_items([self.item(body(finding(evidence="wispr:11111111")), "working")])
        rc, out, _ = self.push(f)
        self.assertEqual(rc, cli.E_OK)
        self.assertEqual(self.added(), [])
        self.assertIn("queued:0/1 to the inbox (1 already open, 0 already handled in the last 7d)", out)

    def test_G_every_open_status_counts_as_open(self):
        for status in ("new", "routing", "routed", "working", "needs-input"):
            with self.subTest(status=status):
                self.set_items([self.item(body(finding()), status)])
                _, out, _ = self.push(finding())
                self.assertEqual(self.added(), [])
                self.assertIn("(1 already open, 0 already handled in the last 7d)", out)

    def test_G_a_done_item_with_the_same_evidence_is_skipped(self):
        self.set_items([self.item(body(finding()), "done")])
        rc, out, _ = self.push(finding())
        self.assertEqual(rc, cli.E_OK)
        self.assertEqual(self.added(), [])
        self.assertIn("queued:0/1 to the inbox (0 already open, 1 already handled in the last 7d)", out)

    def test_G_a_done_item_with_different_evidence_does_not_block_a_new_meeting(self):
        f = finding(evidence="wispr:22222222", detail="Wispr 22222222 10/2 Toyota- Weekly Touchpoint")
        self.set_items([self.item(body(finding()), "done")])
        _, out, _ = self.push(f)
        self.assertEqual(self.added(), [body(f)])
        self.assertTrue(self.added()[0].endswith(" [evidence:wispr:22222222]"))
        self.assertIn("queued:1/1 to the inbox (0 already open, 0 already handled in the last 7d)", out)

    def test_G_a_trimmed_fraction_in_created_at_is_still_read(self):
        # dispatch trims trailing zeros (`19.93Z`, `19.9Z`, `19Z`); Python 3.10's fromisoformat
        # rejects those, and CI runs 3.10.
        for stamp in ("%Y-%m-%dT%H:%M:19.93Z", "%Y-%m-%dT%H:%M:19.9Z", "%Y-%m-%dT%H:%M:19Z"):
            with self.subTest(stamp=stamp):
                it = self.item(body(finding()), "done")
                it["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(stamp)
                self.set_items([it])
                _, out, _ = self.push(finding())
                self.assertEqual(self.added(), [])
                self.assertIn("(0 already open, 1 already handled in the last 7d)", out)

    def test_G_a_failed_item_never_blocks_a_refile(self):
        self.set_items([self.item(body(finding()), "failed")])
        _, out, _ = self.push(finding())
        self.assertEqual(self.added(), [body(finding())])
        self.assertIn("queued:1/1 to the inbox (0 already open, 0 already handled in the last 7d)", out)

    def test_G_a_done_item_outside_the_seven_day_window_is_forgotten(self):
        self.set_items([self.item(body(finding()), "done", hours_old=24 * 8)])
        self.push(finding())
        self.assertEqual(self.added(), [body(finding())])

    def test_G_items_for_other_slugs_and_patterns_do_not_interfere(self):
        self.set_items([self.item(body(finding(slug="toyota")), "working"),
                        self.item(body(finding(pattern="thin-sweep-entry")), "done"),
                        self.item("repo sync: ai-lawnmower has drifted [evidence:wispr:8f107d00]", "done")])
        _, out, _ = self.push(finding())
        self.assertEqual(self.added(), [body(finding())])
        self.assertIn("queued:1/1 to the inbox (0 already open, 0 already handled in the last 7d)", out)

    def test_G_an_item_still_open_after_a_week_still_blocks(self):
        # The 168h read cannot see it (it was created 10 days ago), the by-status read can.
        # Without it a stuck session is followed by a fresh duplicate every half hour.
        self.set_items([self.item(body(finding()), "working", hours_old=24 * 10)])
        _, out, _ = self.push(finding())
        self.assertEqual(self.added(), [])
        self.assertIn("(1 already open, 0 already handled in the last 7d)", out)


class LegacyItems(InboxCase):
    TITLE = "Craig/Team Speed Target Opp Weekly"

    def test_G_a_done_item_filed_before_evidence_tags_matches_on_its_trailing_title(self):
        # What the old code filed: `... (<newest meeting title>)`, no tag.
        self.set_items([self.item(body(finding(), tag=False, detail=self.TITLE), "done")])
        _, out, _ = self.push(finding())
        self.assertEqual(self.added(), [])
        self.assertIn("queued:0/1 to the inbox (0 already open, 1 already handled in the last 7d)", out)

    def test_G_a_legacy_item_about_another_meeting_does_not_match(self):
        self.set_items([self.item(body(finding(), tag=False, detail="Loves : Discuss SCS-4845"), "done")])
        self.push(finding())
        self.assertEqual(self.added(), [body(finding())])

    def test_G_a_legacy_title_cut_at_44_characters_matches(self):
        title = "Toyota- Weekly Touchpoint: Spectro Cloud and Portworx POC"
        f = finding(evidence="wispr:bf8559c5", detail=f"Wispr bf8559c5 10/2 {title[:44]}")
        self.set_items([self.item(body(f, tag=False, detail=title[:44]), "done")])
        self.push(f)
        self.assertEqual(self.added(), [])

    def test_G_other_patterns_match_a_legacy_item_on_their_detail(self):
        f = finding(pattern="entering-prove-value", detail="Hands_on_Eval_POV_URL__c empty",
                    evidence="detail:0123456789ab")
        self.set_items([self.item(body(f, tag=False), "done")])
        self.push(f)
        self.assertEqual(self.added(), [])


class WhatIsAsked(InboxCase):
    def test_G_the_inbox_is_read_as_json_over_the_last_168_hours(self):
        self.push(finding())
        ls = [c for c in self.calls() if c[0] == "ls"]
        self.assertIn(["ls", "--since", "168h", "--limit", "500", "--full", "--output", "json"], ls)

    def test_G_the_by_status_read_is_best_effort(self):
        # The 168h read is the one that must succeed. If the daemon rejects the by-status
        # read, say so on stderr and carry on with what the first read showed.
        open(os.path.join(self.dir.name, "status-fails"), "w").close()
        self.set_items([self.item(body(finding()), "done")])
        rc, out, err = self.push(finding(slug="toyota", detail="Wispr 22222222 10/2 Toyota- Weekly Touchpoint",
                                         evidence="wispr:22222222"))
        self.assertEqual(rc, cli.E_OK)
        self.assertEqual(len(self.added()), 1)
        self.assertTrue(self.added()[0].endswith(" [evidence:wispr:22222222]"))
        self.assertIn("could not list open items older than 7d", err)

    def test_G_an_unreadable_inbox_still_refuses(self):
        open(os.path.join(self.dir.name, "ls-fails"), "w").close()
        rc, out, err = self.push(finding())
        self.assertEqual(rc, cli.E_PARTIAL)
        self.assertEqual(self.added(), [])
        self.assertIn("could not read the inbox", err)
        # And it asked the way it is meant to, so this is not the old call failing the old way.
        self.assertEqual(self.calls()[0], ["ls", "--since", "168h", "--limit", "500", "--full", "--output", "json"])


class TheChurnLoop(InboxCase):
    """The whole loop through cmd_triage: file, a session closes it, the next tick."""

    IDX = {"woven": {"account_name": "Woven by Toyota",
                     "opportunities": [{"id": "006000000000001AAA", "primary": True, "status": "open"}]}}
    PATTERNS = [{"id": "meeting-without-record", "priority": 1, "work": "Log the meeting."}]

    def tick(self, wispr):
        recs = [{"Id": "006000000000001AAA", "Name": "woven", "Amount": 1, "CloseDate": "2026-12-01",
                 "StageName": "Qualification", "Sales_Engineer_Overview__c": "", "POV_Pass__c": False,
                 "Hands_on_Eval_POV_URL__c": ""}]
        ns = argparse.Namespace(since="2026-09-01", days=7, max_deep=60, push=True, json=False)
        out = io.StringIO()
        with mock.patch.object(cli, "load_patterns", return_value=self.PATTERNS), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=self.IDX), \
             mock.patch.object(cli, "wispr_load", return_value=(wispr, {})), \
             mock.patch.object(cli, "WISPR_ENABLED", True), \
             tempfile.TemporaryDirectory() as repo, mock.patch.object(cli, "REPO", repo), \
             contextlib.redirect_stdout(out):
            cli._GAPS.clear()
            self.addCleanup(cli._GAPS.clear)
            cli.cmd_triage(ns)
        return out.getvalue()

    @staticmethod
    def meeting(wid, day):
        return {"id": f"{wid}-0000-4000-8000-000000000000", "title": "Woven prototype review",
                "start": f"{day}T17:00:00Z", "attendees": ["Craig Smith", "Pat Buyer"],
                "summary": "Woven prototype review.", "has_transcript": True, "source": "wispr"}

    def test_G_a_finding_the_session_closed_is_not_refiled_on_the_next_tick(self):
        first = [self.meeting("b2b2b2b2", "2026-09-28")]
        self.tick(first)
        self.assertEqual(len(self.added()), 1)
        self.assertTrue(self.added()[0].endswith(" [evidence:wispr:b2b2b2b2]"))

        # The session looks, finds nothing for Woven, writes nothing, marks the item done.
        items = self.get_items()
        items[0]["status"] = "done"
        self.set_items(items)
        out = self.tick(first)
        self.assertEqual(len(self.added()), 1, "the identical finding must not be filed again")
        self.assertIn("queued:0/1 to the inbox (0 already open, 1 already handled in the last 7d)", out)

        # A genuinely new meeting is new evidence, so it files.
        self.tick(first + [self.meeting("c3c3c3c3", "2026-09-30")])
        self.assertEqual(len(self.added()), 2)
        self.assertTrue(self.added()[1].endswith(" [evidence:wispr:b2b2b2b2,c3c3c3c3]"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
