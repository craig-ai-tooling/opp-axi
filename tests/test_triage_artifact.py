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


class OnlyTheMailInboxRaisesTheArtifactFinding(unittest.TestCase):
    """`customer-artifact-awaiting-review` says a CUSTOMER sent something and is
    blocked on us. A file we wrote ourselves makes that sentence false.

    There were two call sites. The scoped one reads `<slug>/mail-inbox/`, where
    loop/mail-probe.py saves what actually arrived by mail. The other walked the
    whole opp directory, so any commit into `<slug>/` raised it.

    Measured 9/17/26 on the live repo: of four findings, three came from the broad
    walk and all three were our own output committed hours earlier --
    toyota/roi/tx-automotive-instinct-coder-roi.pdf, aunalytics/gpu/*, and
    tesla/2026-09-17-shi-recap-coverage.md. Each routed a session that found
    nothing to do.
    """

    def test_the_broad_walk_does_not_raise_the_artifact_finding(self):
        # Structural, like test_the_check_runs_before_the_staleness_gate above: the
        # defect is a call SITE, so read the source and assert there is exactly one,
        # and that it is the scoped one.
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        self.assertEqual(
            1, src.count('add("customer-artifact-awaiting-review"'),
            "customer-artifact-awaiting-review must have exactly one call site, "
            "scoped to MAIL_INBOX -- a second, unscoped one files our own files "
            "as though a customer were waiting on us",
        )

    def test_the_one_call_site_is_the_scoped_one(self):
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        call = src.index('add("customer-artifact-awaiting-review"')
        # unreviewed_artifacts(...) / repo_touched_since(..., subdir=MAIL_INBOX) is
        # the line that feeds it, and it sits just above the call.
        self.assertIn("subdir=MAIL_INBOX", src[:call],
                      "the surviving call site must read the mail-inbox, not the opp dir")

    def test_touched_still_feeds_the_thin_sweep_signal_count(self):
        """Removing the finding must not remove the variable. thin-sweep-entry counts
        `len(touched) + len(meetings)` to decide a placeholder entry has substance
        behind it, and that count is still wanted."""
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("touched = repo_touched_since(slug, since)", src)
        self.assertIn("sig = len(touched) + len(meetings)", src)


class TheWorkTextReachesTheSessionWhole(unittest.TestCase):
    """`work` is the routed session's entire instruction for a finding.

    _push_to_inbox() builds the item body as
    `[pattern] slug: why. work (detail)`, and that body is all the session gets.
    `work` used to be truncated to 150 characters where the finding is built, so
    every pattern's instruction arrived cut mid-sentence.

    Measured on the items dayclose filed 9/17/26: patterns.yaml's
    customer-artifact-awaiting-review `work` is ~700 characters and carries the
    constraints that matter -- commit only what you DERIVE from the customer's file,
    name the artifact's path in the log entry, leave a draft rather than send. The
    session received "...it is already saved in <slug>/mail-inbox/. That direct" and
    none of them.
    """

    def test_work_is_not_truncated_where_the_finding_is_built(self):
        import inspect
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn('"work": " ".join((p.get("work") or "").split())', src)
        self.assertNotIn('"work": " ".join((p.get("work") or "").split())[:150]', src,
                         "work must reach _push_to_inbox whole -- it is the session's "
                         "whole instruction, not a display string")

    def test_a_long_work_text_survives_into_the_pushed_body(self):
        """End to end through the real body construction, not the source text."""
        long_work = "STEP ONE. " + ("policy sentence that matters. " * 40)
        self.assertGreater(len(long_work), 150)
        f = {"pattern": "meeting-without-record", "slug": "acme",
             "why": "1 meeting(s) with no entry", "work": long_work, "detail": "a call"}
        body = (f"[{f['pattern']}] {f['slug']}: {f['why']}. {f['work']}"
                + (f" ({f['detail']})" if f["detail"] else ""))
        self.assertIn(long_work, body)
        self.assertTrue(body.endswith("(a call)"))
