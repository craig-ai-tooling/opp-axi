"""meeting-without-record names the account a meeting is about, and says so once.

Measured 10/3/26: since 9/17 the finding was filed 7 times for loves, 6 for woven, and 3 each
for toyota, aunalytics, tesla and discount-tire. The session outcome notes give three causes,
and one rule answers each:

  B  A word several accounts own is not evidence for any of them. "Woven by Toyota" and
     "Toyota Material Handling North America" both own "toyota", so every Toyota call was
     attributed to toyota, tmhna AND woven. The woven session rejected it ("transcript
     never mentions Woven"), wrote nothing, and the finding came back 30 minutes later.
  C  A record with no `participants` whose attendees are all colleagues is read by its title
     only. "Craig/Team Speed Target Opp Weekly" is a pipeline review; its summary names six
     deals and it was attributed to tesla, toyota and woven.
  D  A meeting still about three or more accounts after B and C is a pipeline review.
  E  A meeting the account's OPP.md already cites by id (`Wispr 8f107d00`) was looked at.
  F  The finding names its meetings and carries an evidence key.

Every test here uses fixtures: sf_query, load_patterns, opp_index and wispr_load are patched
and REPO is a temp dir, so nothing reaches Salesforce, the real Wispr cache or the real opp
repo. The one git repo is a throwaway one with a fabricated origin/main ref.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli  # noqa: E402

CRAIG = "Craig Smith"
PAT = "Pat Buyer"        # an attendee nobody has seen on a Spectro invite
DANE = {"name": "Dane Ferguson", "emails": ["dane.ferguson@spectrocloud.com"]}
BILL = {"name": "Bill DeCoste", "emails": ["bill.decoste@spectrocloud.com"]}
GIT_ENV = {"GIT_AUTHOR_NAME": "Attribution Test", "GIT_AUTHOR_EMAIL": "attribution-test@example.com",
           "GIT_COMMITTER_NAME": "Attribution Test", "GIT_COMMITTER_EMAIL": "attribution-test@example.com"}


def entry(account, n, aliases=None):
    """One .salesforce.json: an account with a single open opp."""
    d = {"account_name": account,
         "opportunities": [{"id": f"006{n:012d}AAA", "primary": True, "status": "open"}]}
    if aliases:
        d["aliases"] = aliases
    return d


def mtg(wid, title, summary, day="2026-09-30", attendees=(CRAIG, PAT), participants=None):
    """A cache record. `wid` is the 8 hex chars an OPP.md note would cite."""
    m = {"id": f"{wid}-0000-4000-8000-000000000000", "title": title, "start": f"{day}T17:00:00Z",
         "attendees": list(attendees), "summary": summary, "has_transcript": True, "source": "wispr"}
    if participants is not None:
        m["participants"] = participants
    return m


# An all-colleague invite. It is internal, so it is never attributed to anyone, but it is how
# the cache learns that Dane Ferguson and Bill DeCoste are colleagues.
TEACHER = mtg("00000000", "Dane/Bill/Craig Sync", "Internal.",
              attendees=(CRAIG, "Dane Ferguson", "Bill DeCoste"), participants=[DANE, BILL])


def git(repo, *args):
    env = dict(os.environ)
    env.update(GIT_ENV)
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env, check=True)


class TriageCase(unittest.TestCase):
    PATTERNS = [{"id": "meeting-without-record", "priority": 1, "work": "Log the meeting."},
                {"id": "thin-sweep-entry", "priority": 2, "work": "Fill the entry in."},
                {"id": "entering-prove-value", "priority": 2, "work": "Add a plan."}]

    def setUp(self):
        cli._GAPS.clear()
        self.addCleanup(cli._GAPS.clear)
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)

    def note(self, slug, text):
        os.makedirs(os.path.join(self.repo.name, slug), exist_ok=True)
        with open(os.path.join(self.repo.name, slug, "OPP.md"), "w") as fh:
            fh.write(text)

    def triage(self, idx, wispr, entries=None, stages=None, since="2026-09-01"):
        entries, stages = entries or {}, stages or {}
        recs = [{"Id": d["opportunities"][0]["id"], "Name": slug, "Amount": 1000,
                 "CloseDate": "2026-12-01", "StageName": stages.get(slug, "Qualification"),
                 "Sales_Engineer_Overview__c": entries.get(slug, ""), "POV_Pass__c": False,
                 "Hands_on_Eval_POV_URL__c": ""} for slug, d in idx.items()]
        ns = argparse.Namespace(since=since, days=7, max_deep=60, push=False, json=True)
        with mock.patch.object(cli, "load_patterns", return_value=self.PATTERNS), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "wispr_load", return_value=(wispr, {})), \
             mock.patch.object(cli, "WISPR_ENABLED", True), \
             mock.patch.object(cli, "REPO", self.repo.name):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_triage(ns)
        return json.loads(buf.getvalue())

    @staticmethod
    def mwr(out):
        return {f["slug"]: f for f in out["findings"] if f["pattern"] == "meeting-without-record"}

    @staticmethod
    def counts(found):
        """slug -> the N in "N meeting(s) since ..."."""
        return {s: int(f["why"].split()[0]) for s, f in found.items()}


class RowsCarryIdAndStart(unittest.TestCase):
    def test_A_wispr_match_rows_carry_the_full_id_and_start(self):
        m = mtg("8f107d00", "Acme sync", "Acme renewal.")
        rows = cli.wispr_match([m], ["acme"], date(2026, 9, 1), 5, 90)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], m["id"])
        self.assertEqual(rows[0]["start"], m["start"])
        # The columns `evidence` prints are untouched.
        self.assertEqual((rows[0]["date"], rows[0]["title"], rows[0]["n"]), ("09-30", "Acme sync", 2))
        self.assertEqual(rows[0]["summary"], "Acme renewal.")


class SharedWords(TriageCase):
    def test_B_a_word_several_accounts_own_is_not_evidence_for_each_of_them(self):
        idx = {"toyota": entry("Toyota North America", 1),
               "tmhna": entry("Toyota Material Handling North America", 2),
               "woven": entry("Woven by Toyota", 3),
               # Two slugs of ONE account share "acme" and must both keep matching it.
               "acme": entry("Acme Robotics", 4), "acme-gtm": entry("Acme Robotics", 5)}
        wispr = [mtg("a1a1a1a1", "Toyota Weekly Touchpoint", "Weekly Toyota touchpoint on the Portworx POC."),
                 mtg("b2b2b2b2", "Prototype review", "Woven prototype review with the Arene team."),
                 mtg("c3c3c3c3", "Forklift telemetry", "TMHNA forklift telemetry kickoff."),
                 mtg("d4d4d4d4", "Acme sync", "Acme renewal.")]
        found = self.mwr(self.triage(idx, wispr))
        # Each account gets the meeting that names IT, and a plain Toyota call is toyota's alone.
        self.assertEqual(self.counts(found),
                         {"toyota": 1, "woven": 1, "tmhna": 1, "acme": 1, "acme-gtm": 1})
        self.assertIn("a1a1a1a1", found["toyota"]["detail"])
        self.assertIn("b2b2b2b2", found["woven"]["detail"])
        self.assertIn("c3c3c3c3", found["tmhna"]["detail"])


class ColleagueOnly(TriageCase):
    def test_C_a_participantless_record_of_colleagues_is_read_by_its_title(self):
        idx = {"acme": entry("Acme Corp", 1), "bravo": entry("Bravo Inc", 2)}
        wispr = [
            TEACHER,
            # Colleagues only: the summary naming both deals must not attribute it.
            mtg("11111111", "Craig/Team Pipeline Weekly", "Pipeline sync covering Acme and Bravo.",
                attendees=(CRAIG, "Dane Ferguson", "Bill DeCoste")),
            # Colleagues only, but the TITLE names the account: still acme's.
            mtg("22222222", "Acme standup", "Status only.", attendees=(CRAIG, "Dane Ferguson")),
            # One attendee nobody has seen on a Spectro invite: a customer call, summary counts.
            mtg("33333333", "Customer sync", "Bravo renewal terms.", attendees=(CRAIG, PAT)),
            # Nobody but the owner, and an empty list: a missing list is not a list of colleagues.
            mtg("44444444", "Solo notes", "Acme follow up.", attendees=(CRAIG,)),
            mtg("55555555", "Untracked call", "Bravo pricing.", attendees=()),
        ]
        found = self.mwr(self.triage(idx, wispr))
        self.assertEqual(self.counts(found), {"acme": 2, "bravo": 2})
        self.assertNotIn("11111111", found["acme"]["detail"] + found["bravo"]["detail"])
        self.assertIn("22222222", found["acme"]["detail"])


class PortfolioMeetings(TriageCase):
    IDX = {"acme": entry("Acme Corp", 1), "bravo": entry("Bravo Inc", 2),
           "cobalt": entry("Cobalt Ltd", 3), "delta": entry("Delta LLC", 4)}
    WISPR = [mtg("66666666", "Pipeline review", "Covered Acme, Bravo and Cobalt this week."),
             mtg("77777777", "Joint call", "Acme and Bravo joint call."),
             mtg("88888888", "Delta sync", "Delta status.")]

    def test_D_a_meeting_about_three_accounts_is_a_pipeline_review_not_a_customer_meeting(self):
        found = self.mwr(self.triage(self.IDX, self.WISPR))
        # The three-account review is dropped; the two-account call and the one-account call stay.
        self.assertEqual(self.counts(found), {"acme": 1, "bravo": 1, "delta": 1})
        self.assertIn("77777777", found["acme"]["detail"])
        self.assertNotIn("cobalt", found)

    def test_D_applies_to_meeting_without_record_only(self):
        # cobalt's placeholder entry is dated inside the window, so meeting-without-record has
        # its answer; thin-sweep-entry still counts the review as a signal that exists.
        out = self.triage(self.IDX, self.WISPR, entries={"cobalt": "9/25/26 CS: No new activity this week."})
        thin = [f for f in out["findings"] if f["pattern"] == "thin-sweep-entry"]
        self.assertEqual([f["slug"] for f in thin], ["cobalt"])
        self.assertIn("1 signal(s)", thin[0]["why"])
        self.assertEqual(self.counts(self.mwr(out)), {"acme": 1, "bravo": 1, "delta": 1})


class AlreadyHandled(TriageCase):
    IDX = {"acme": entry("Acme Corp", 1), "bravo": entry("Bravo Inc", 2), "cobalt": entry("Cobalt Ltd", 3)}
    WISPR = [mtg("8f107d00", "Acme sync", "Acme renewal.", day="2026-09-28"),
             mtg("99999999", "Acme call", "Acme again.", day="2026-09-29"),
             mtg("abababab", "Bravo call", "Bravo pricing.", day="2026-09-30"),
             mtg("cdcdcdcd", "Joint call", "Acme and Bravo planning.", day="2026-09-27"),
             mtg("ffffffff", "Cobalt call", "Cobalt kickoff.", day="2026-09-26")]

    def test_E_a_meeting_opp_md_cites_by_id_is_handled(self):
        # Case-insensitive, whole-word, and scoped to the slug whose note it is.
        self.note("acme", "# acme\n- 10/3/26: Wispr 8F107D00 reviewed, nothing for Acme.\n"
                          "- 10/3/26: Wispr 99999999ab is a different, longer id.\n"
                          "- 10/3/26: joint call (cdcdcdcd) logged here.\n")
        self.note("bravo", "- 10/3/26: Wispr 8f107d00 was Acme's, not ours.\n")
        # cobalt has no OPP.md at all: nothing is handled.
        found = self.mwr(self.triage(self.IDX, self.WISPR))
        self.assertEqual(self.counts(found), {"acme": 1, "bravo": 2, "cobalt": 1})
        self.assertIn("99999999", found["acme"]["detail"])
        self.assertNotIn("8f107d00", found["acme"]["detail"])
        self.assertIn("cdcdcdcd", found["bravo"]["detail"])    # cited in ACME's note only

    def test_E_origin_main_is_read_as_well_as_the_working_tree(self):
        # The checkout lags: the citation is on origin/main, the working tree has not pulled.
        idx = {"acme": entry("Acme Corp", 1)}
        wispr = [mtg("8f107d00", "Acme sync", "Acme renewal.")]
        repo = self.repo.name
        with mock.patch.dict(os.environ):
            for k in [k for k in os.environ if k.startswith("GIT_")]:
                del os.environ[k]                       # a hook's GIT_DIR must not redirect this
            git(repo, "init", "-q", "-b", "main")
            self.note("acme", "- 10/3/26: Wispr 8f107d00 reviewed.\n")
            git(repo, "add", "acme/OPP.md")
            git(repo, "commit", "-q", "-m", "notes")
            git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            self.note("acme", "# acme\nnothing logged here yet\n")
            self.assertEqual(self.mwr(self.triage(idx, wispr)), {})
            # And the citation is what did it: a different id on origin/main handles nothing.
            self.assertEqual(self.counts(self.mwr(self.triage(idx, [mtg("12121212", "Acme sync", "Acme.")]))),
                             {"acme": 1})


class FindingShape(TriageCase):
    def test_F_the_finding_names_its_meetings_and_carries_an_evidence_key(self):
        idx = {"acme": entry("Acme Corp", 1), "prove": entry("Prove Corp", 2)}
        long_title = "Acme four with a long title that runs past the forty-four character cut"
        wispr = [mtg("aaaaaaaa", "Acme one", "Acme.", day="2026-09-26"),
                 mtg("bbbbbbbb", "Acme two", "Acme.", day="2026-09-27"),
                 mtg("cccccccc", "Acme three", "Acme.", day="2026-09-28"),
                 mtg("dddddddd", long_title, "Acme.", day="2026-09-29")]
        out = self.triage(idx, wispr, stages={"prove": "Prove Value"})
        f = self.mwr(out)["acme"]
        # why, work and the prefix inputs keep their exact shape.
        self.assertEqual(f["why"], "4 meeting(s) since 2026-09-01 with no SE Activity entry after them")
        self.assertEqual(f["work"], "Log the meeting.")
        # Newest first, three at most, each as `Wispr <id8> <M/D> <title>`.
        self.assertEqual(f["detail"], f"Wispr dddddddd 9/29 {long_title[:44]}; "
                                      "Wispr cccccccc 9/28 Acme three; Wispr bbbbbbbb 9/27 Acme two")
        # The evidence key names ALL of them, sorted.
        self.assertEqual(f["evidence"], "wispr:aaaaaaaa,bbbbbbbb,cccccccc,dddddddd")
        # Every other pattern hashes its whitespace-normalised detail.
        pv = next(x for x in out["findings"] if x["pattern"] == "entering-prove-value")
        want = hashlib.sha1(" ".join(pv["detail"].split()).encode()).hexdigest()[:12]
        self.assertEqual(pv["detail"], "Hands_on_Eval_POV_URL__c empty")
        self.assertEqual(pv["evidence"], f"detail:{want}")
        self.assertTrue(all(x["evidence"] for x in out["findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
