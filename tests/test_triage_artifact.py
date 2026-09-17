"""The customer-artifact signal must not be gated on our own paperwork.

`cmd_triage` narrows to opps "worth spending evidence on" before running the
expensive Wispr fan-out, and the customer-artifact check used to sit below that
narrowing. So it only ran when the SE Activity was stale, thin, or written by
someone else — which couples two unrelated questions. "Is our record current" has
nothing to do with "is a customer waiting on us", and the common case breaks on
the second: write an entry after a call, the customer sends the export the next
morning, and the artifact never fires because the entry we just wrote made the opp
look handled.

Measured 9/16/26 against the live repo: Denali's RVTools export was saved into the
opp directory, the opp carried a same-day CS entry, and triage reported nothing.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from opp_axi import cli


class ArtifactSignalIsUngated(unittest.TestCase):
    """repo_touched_since is the whole signal; these pin when it is consulted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._repo = cli.REPO
        cli.REPO = self.tmp.name
        self.addCleanup(lambda: setattr(cli, "REPO", self._repo))
        self.slug = "acme"
        os.makedirs(os.path.join(self.tmp.name, self.slug))

    def _write(self, name, body="x" * 64):
        path = os.path.join(self.tmp.name, self.slug, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_a_customer_file_is_seen(self):
        self._write("mail-inbox/2026-09-16-RVTools_export.zip")
        touched = cli.repo_touched_since(self.slug, date.today() - timedelta(days=3))
        self.assertIn("mail-inbox/2026-09-16-RVTools_export.zip", touched)

    def test_the_ungated_check_sees_only_what_the_customer_sent(self):
        # The whole-directory walk counts files WE create. Running that ungated
        # turned one real finding into six on 9/16/26, five of them our own
        # assessments, terraform and mirror scripts. Scoping to mail-inbox is what
        # makes "Customer sent files and is blocked on us" true of what fires.
        self._write("migration-assessment/our_own_assessment.pdf")
        self._write("eks-connect-tenants/main.tf")
        since = date.today() - timedelta(days=3)
        self.assertEqual([], cli.repo_touched_since(self.slug, since, subdir=cli.MAIL_INBOX))
        self.assertEqual(2, len(cli.repo_touched_since(self.slug, since)),
                         "the broad walk still sees our files, for the gated check below")

        self._write("mail-inbox/2026-09-16-RVTools_export.zip")
        arrived = cli.repo_touched_since(self.slug, since, subdir=cli.MAIL_INBOX)
        self.assertEqual(["mail-inbox/2026-09-16-RVTools_export.zip"], arrived,
                         "paths stay relative to the opp dir, not the subdir")

    def test_a_missing_mail_inbox_is_empty_not_an_error(self):
        self.assertEqual([], cli.repo_touched_since(
            self.slug, date.today() - timedelta(days=3), subdir=cli.MAIL_INBOX))

    def test_our_own_notes_are_not_a_customer_signal(self):
        # OPP.md changing is our footprint. A draft we wrote for Craig to send is
        # the SE working the opp, not the customer producing something to review.
        for name in ("OPP.md", "CLAUDE.md", ".salesforce.json", "DRAFT-reply.md"):
            self._write(name)
        self.assertEqual([], cli.repo_touched_since(self.slug, date.today() - timedelta(days=3)))

    def test_a_file_older_than_the_window_does_not_fire(self):
        path = self._write("mail-inbox/old.zip")
        old = (date.today() - timedelta(days=30))
        stamp = __import__("time").mktime(old.timetuple())
        os.utime(path, (stamp, stamp))
        self.assertEqual([], cli.repo_touched_since(self.slug, date.today() - timedelta(days=3)))

    def test_the_check_runs_before_the_staleness_gate(self):
        # Structural, because the bug was one of ORDER: the call sat under a
        # `continue` that current, CS-authored opps always took. Read the source and
        # assert the artifact check is reached first, so a future reorder that
        # re-couples them fails here rather than silently going quiet in production.
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        artifact = src.index("customer-artifact-awaiting-review")
        gate = src.index("needs_look = ")
        self.assertLess(artifact, gate,
                        "the customer-artifact check must not sit below the needs_look gate")
        self.assertIn("subdir=MAIL_INBOX", src[:gate],
                      "the ungated check must stay scoped to what the customer sent")

    def test_the_expensive_fanout_is_still_rationed(self):
        # The hoist must not also lift the Wispr matching, which is what --max-deep
        # exists to cap.
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        self.assertLess(src.index("needs_look = "), src.index("wispr_load()"),
                        "the Wispr fan-out must stay behind the gate")
        self.assertLess(src.index("deep >= a.max_deep"), src.index("wispr_load()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ArtifactFindingStopsOnceItIsLogged(unittest.TestCase):
    """The signal answers "a customer file is here", which stays true for the whole
    --since window. Nothing answered "and we have dealt with it".

    Measured 9/16/26 on toyota: the artifact was reviewed and logged at 19:52Z
    (customer-opportunities#75), and `customer-artifact-awaiting-review` was filed
    four more times over the next five hours, routing a session each time. The
    window is seven days and triage runs every thirty minutes, so left alone that
    one docx was good for roughly 336 filings.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._repo = cli.REPO
        cli.REPO = self.tmp.name
        self.addCleanup(lambda: setattr(cli, "REPO", self._repo))
        self.slug = "acme"
        os.makedirs(os.path.join(self.tmp.name, self.slug, cli.MAIL_INBOX))

    def _note(self, body):
        with open(os.path.join(self.tmp.name, self.slug, "OPP.md"), "w") as f:
            f.write(body)

    def test_an_unlogged_artifact_still_fires(self):
        self._note("# acme\n\n## Log\n- 2026-09-15: kickoff call.\n")
        self.assertEqual(
            ["mail-inbox/2026-09-16-RVTools_export.zip"],
            cli.unreviewed_artifacts(self.slug, ["mail-inbox/2026-09-16-RVTools_export.zip"]),
        )

    def test_an_artifact_named_in_the_log_is_done(self):
        self._note(
            "# acme\n\n## Log\n- 2026-09-16: **RVTools export mailed in.** Consolidation run; "
            "raw file at `acme/mail-inbox/2026-09-16-RVTools_export.zip`, gitignored.\n"
        )
        self.assertEqual(
            [], cli.unreviewed_artifacts(self.slug, ["mail-inbox/2026-09-16-RVTools_export.zip"])
        )

    def test_a_second_artifact_fires_while_the_first_stays_done(self):
        """Per file, not per opp. The same customer sending a second export is new work."""
        self._note("- 2026-09-16: logged `acme/mail-inbox/2026-09-16-RVTools_export.zip`.\n")
        self.assertEqual(
            ["mail-inbox/2026-09-17-support_bundle.tgz"],
            cli.unreviewed_artifacts(
                self.slug,
                ["mail-inbox/2026-09-16-RVTools_export.zip",
                 "mail-inbox/2026-09-17-support_bundle.tgz"],
            ),
        )

    def test_a_word_like_artifact_in_the_note_does_not_mark_it_done(self):
        """The done-check matches the filename, never a word.

        patterns.yaml's own done_when is `matches: [artifact, validated, ...]`.
        toyota/OPP.md carries "artifact" 12 times and "validated" 9 times from
        unrelated entries, so that check reads as handled before anything arrives.
        """
        self._note("- 2026-09-01: validated the profile. Left an artifact in the repo.\n")
        self.assertEqual(
            ["mail-inbox/2026-09-16-RVTools_export.zip"],
            cli.unreviewed_artifacts(self.slug, ["mail-inbox/2026-09-16-RVTools_export.zip"]),
        )

    def test_no_opp_note_means_unreviewed_not_handled(self):
        """A customer is blocked on us, so an unreadable note fires. It never mutes."""
        self.assertEqual(
            ["mail-inbox/2026-09-16-RVTools_export.zip"],
            cli.unreviewed_artifacts(self.slug, ["mail-inbox/2026-09-16-RVTools_export.zip"]),
        )
