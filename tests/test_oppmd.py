"""oppmd — the budgeted-brief parser and the no-read Log insert.

9/14/26: sessions stop reading whole OPP.md files. These tests pin the two contracts that
matter — parsing survives the real Log messiness (both date forms, a leading HTML comment,
a table nested in an entry, a single 3KB line) without crashing or mis-splitting entries,
and `brief`'s token budget is an actual cap, not a suggestion.
"""
import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "oppmd")
sys.path.insert(0, ROOT)

from opp_axi import oppmd                 # noqa: E402


def fixture(name):
    return os.path.join(FIXTURES, name)


def _tmp_copy(name):
    """A fixture is checked-in and read by other tests too — insert_log_entry mutates
    its file, so every insert test works on its own throwaway copy."""
    d = tempfile.mkdtemp()
    dst = os.path.join(d, "OPP.md")
    shutil.copyfile(fixture(name), dst)
    return dst


@contextlib.contextmanager
def _isolated_state_dir():
    """insert_log_entry's lock lives under OPP_AXI_STATE — point it at a throwaway dir so
    lock tests never touch the real ~/.local/state/opp-axi on this machine."""
    d = tempfile.mkdtemp()
    old = os.environ.get("OPP_AXI_STATE")
    os.environ["OPP_AXI_STATE"] = d
    try:
        yield d
    finally:
        if old is None:
            os.environ.pop("OPP_AXI_STATE", None)
        else:
            os.environ["OPP_AXI_STATE"] = old
        shutil.rmtree(d, ignore_errors=True)


# ── parsing ──────────────────────────────────────────────────────────────────
class ParseFrontmatterAndSections(unittest.TestCase):
    def test_frontmatter_keys(self):
        fm, _sections, _entries = oppmd.parse(open(fixture("iso.md")).read())
        self.assertEqual(fm["opp_name"], "Iso Corp - Land - Q3 FY2026")
        self.assertEqual(fm["stage"], "Alignment")
        self.assertEqual(fm["amount"], "250000")
        self.assertEqual(fm["sf_opp_id"], "006Rl00001IsoCorpAA")

    def test_sections_are_ordered_and_keyed_by_full_heading(self):
        _fm, sections, _entries = oppmd.parse(open(fixture("iso.md")).read())
        self.assertEqual(list(sections)[0], "Snapshot")
        self.assertIn("Decisions & commitments", sections)
        self.assertIn("Risks / blockers", sections)

    def test_find_section_is_a_case_insensitive_prefix_match(self):
        _fm, sections, _entries = oppmd.parse(open(fixture("iso.md")).read())
        self.assertEqual(oppmd.find_section(sections, "risks"), "Risks / blockers")
        self.assertEqual(oppmd.find_section(sections, "Decisions"), "Decisions & commitments")
        self.assertIsNone(oppmd.find_section(sections, "not-a-real-section"))


class ParseLogEntries(unittest.TestCase):
    def test_iso_dated_entries_and_counts(self):
        _fm, _sections, entries = oppmd.parse(open(fixture("iso.md")).read())
        self.assertEqual(len(entries), 4)
        self.assertEqual([e["date"] for e in entries],
                         [date(2026, 9, 5), date(2026, 8, 25), date(2026, 8, 10), date(2026, 7, 1)])

    def test_table_stays_inside_its_entry(self):
        """A nested markdown table must not be mistaken for a new entry, and must survive
        into the entry's body/raw for later --grep matching."""
        _fm, _sections, entries = oppmd.parse(open(fixture("iso.md")).read())
        e = entries[0]
        self.assertEqual(e["headline"], "Entry E — table review.")
        self.assertIn("| Latency | 120ms | 40ms |", e["body"])
        self.assertIn("| Latency | 120ms | 40ms |", e["raw"])

    def test_mdy_dated_entries_and_first_sentence_fallback(self):
        _fm, _sections, entries = oppmd.parse(open(fixture("mdy.md")).read())
        self.assertEqual(len(entries), 3)
        self.assertEqual([e["date"] for e in entries],
                         [date(2026, 9, 5), date(2026, 8, 25), date(2026, 8, 1)])
        # third entry has no bold lead -> headline falls back to the first sentence
        self.assertEqual(entries[2]["headline"],
                         "First contact from an inbound webinar signup.")

    def test_leading_html_comment_is_not_an_entry(self):
        _fm, _sections, entries = oppmd.parse(open(fixture("html_comment.md")).read())
        self.assertEqual(len(entries), 2)
        self.assertNotIn("Newest first", entries[0]["raw"])
        self.assertEqual(entries[0]["headline"], "First entry after the comment.")

    def test_single_3kb_line_entry_survives_whole(self):
        _fm, _sections, entries = oppmd.parse(open(fixture("long_line.md")).read())
        self.assertEqual(len(entries), 2)
        huge = entries[0]
        self.assertGreater(len(huge["raw"]), 2500)
        self.assertLessEqual(len(huge["headline"]), 200)
        self.assertEqual(huge["headline"], "Huge single-line entry.")
        # the marker sits near the tail of the ~2.9KB line — proves the whole line was
        # kept, not just a truncated prefix
        self.assertIn("ZZMARKEREND", huge["raw"])

    def test_note_with_no_log_heading_parses_to_no_entries(self):
        _fm, sections, entries = oppmd.parse(open(fixture("no_log.md")).read())
        self.assertEqual(entries, [])
        self.assertNotIn("Log", sections)


class DateParsing(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(oppmd._parse_date_str("2026-09-14"), date(2026, 9, 14))

    def test_mdy_two_digit_year(self):
        self.assertEqual(oppmd._parse_date_str("9/14/26"), date(2026, 9, 14))

    def test_mdy_four_digit_year(self):
        self.assertEqual(oppmd._parse_date_str("9/14/2026"), date(2026, 9, 14))

    def test_unparsable_is_none(self):
        self.assertIsNone(oppmd._parse_date_str("not a date"))
        self.assertIsNone(oppmd._parse_date_str(""))


# ── brief ──────────────────────────────────────────────────────────────────
class Brief(unittest.TestCase):
    def test_default_sections_present(self):
        out = oppmd.brief(fixture("iso.md"), "iso", since="2000-01-01")
        self.assertIn("## Snapshot", out)
        self.assertIn("## Decisions & commitments", out)
        self.assertIn("## Risks / blockers", out)
        self.assertIn("next: opp-axi brief iso", out)

    def test_since_window_filters_headlines(self):
        out = oppmd.brief(fixture("iso.md"), "iso", since="2026-08-20")
        self.assertIn("Entry E — table review.", out)     # 2026-09-05, in window
        self.assertIn("Entry D — kickoff recap.", out)     # 2026-08-25, in window
        self.assertNotIn("Entry C — early scoping.", out)  # 2026-08-10, out of window
        self.assertNotIn("Entry B — inbound lead.", out)   # 2026-07-01, out of window

    def test_section_flag_replaces_the_default_list(self):
        out = oppmd.brief(fixture("iso.md"), "iso", sections=["Technical environment"],
                          since="2000-01-01")
        self.assertIn("## Technical environment", out)
        self.assertNotIn("## Snapshot", out)

    def test_grep_prints_full_bodies_not_just_headlines(self):
        out = oppmd.brief(fixture("iso.md"), "iso", grep="table", since="2000-01-01")
        self.assertIn("| Latency | 120ms | 40ms |", out)        # full body, not a headline
        self.assertNotIn("Entry D", out)                          # non-matching entry excluded
        self.assertIn("Log matches /table/", out)

    def test_grep_is_case_insensitive(self):
        out = oppmd.brief(fixture("iso.md"), "iso", grep="TABLE REVIEW", since="2000-01-01")
        self.assertIn("Entry E", out)

    def test_bad_grep_pattern_raises(self):
        with self.assertRaises(oppmd.BadRegex):
            oppmd.brief(fixture("iso.md"), "iso", grep="(unclosed")

    def test_bad_since_raises(self):
        with self.assertRaises(oppmd.BadDate):
            oppmd.brief(fixture("iso.md"), "iso", since="not-a-date")

    def test_full_returns_the_raw_file_unchanged(self):
        raw = open(fixture("iso.md"), encoding="utf-8").read()
        out = oppmd.brief(fixture("iso.md"), "iso", full=True)
        self.assertEqual(out, raw.rstrip("\n"))

    def test_json_is_parseable_and_carries_the_window(self):
        import json
        out = oppmd.brief(fixture("iso.md"), "iso", since="2026-08-20", as_json=True)
        data = json.loads(out)
        self.assertEqual(data["opp"], "iso")
        self.assertEqual(data["since"], "2026-08-20")
        self.assertEqual(len(data["log_entries"]), 2)

    def test_no_log_section_does_not_crash_brief(self):
        out = oppmd.brief(fixture("no_log.md"), "nolog")
        self.assertIn("## Snapshot", out)
        self.assertNotIn("## Log", out)

    def test_budget_caps_output_and_reports_the_cut(self):
        out = oppmd.brief(fixture("big.md"), "big", since="2000-01-01", budget=300)
        self.assertLessEqual(len(out.encode("utf-8")), 1200)
        self.assertIn("budget 300tok: cut", out)
        self.assertIn("--full", out)          # the flag that shows everything, per spec

    def test_default_budget_is_1500_tokens_worth_of_bytes(self):
        self.assertEqual(oppmd.DEFAULT_BUDGET_TOKENS, 1500)


# ── log insert ───────────────────────────────────────────────────────────────
class InsertLogEntry(unittest.TestCase):
    def test_inserts_directly_above_the_newest_entry_iso_style(self):
        path = _tmp_copy("iso.md")
        with _isolated_state_dir():
            line = oppmd.insert_log_entry(path, "insert-iso-1", "New test entry",
                                          date_str="2026-09-20")
        self.assertEqual(line, "- 2026-09-20: New test entry")
        lines = open(path, encoding="utf-8").read().split("\n")
        log_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "## Log")
        self.assertEqual(lines[log_idx + 1], line)
        self.assertTrue(lines[log_idx + 2].startswith("- 2026-09-05:"))   # old newest, untouched

    def test_format_follows_the_files_own_style_not_the_input(self):
        """mdy.md's newest entry is M/D/YY; an ISO --date must still render M/D/YY."""
        path = _tmp_copy("mdy.md")
        with _isolated_state_dir():
            line = oppmd.insert_log_entry(path, "insert-mdy-1", "text", date_str="2026-09-20")
        self.assertEqual(line, "- 9/20/26: text")

    def test_inserts_below_a_leading_html_comment(self):
        path = _tmp_copy("html_comment.md")
        with _isolated_state_dir():
            oppmd.insert_log_entry(path, "insert-comment-1", "after the comment",
                                   date_str="2026-09-20")
        lines = open(path, encoding="utf-8").read().split("\n")
        comment_idx = next(i for i, ln in enumerate(lines) if "<!--" in ln)
        self.assertIn("after the comment", lines[comment_idx + 1])
        self.assertIn("2026-09-01", lines[comment_idx + 2])   # old newest, still right below

    def test_refuses_without_a_log_heading(self):
        path = _tmp_copy("no_log.md")
        with _isolated_state_dir(), self.assertRaises(oppmd.NoLogSection) as cm:
            oppmd.insert_log_entry(path, "insert-nolog-1", "x")
        self.assertEqual(str(cm.exception), path)   # the refusal names the path

    def test_bad_date_raises(self):
        path = _tmp_copy("iso.md")
        with _isolated_state_dir(), self.assertRaises(oppmd.BadDate):
            oppmd.insert_log_entry(path, "insert-baddate-1", "x", date_str="garbage")

    def test_default_date_is_today(self):
        path = _tmp_copy("iso.md")
        with _isolated_state_dir():
            line = oppmd.insert_log_entry(path, "insert-today-1", "no date given")
        self.assertEqual(line, f"- {date.today().isoformat()}: no date given")

    def test_write_is_atomic_no_temp_file_left_behind(self):
        path = _tmp_copy("iso.md")
        with _isolated_state_dir():
            oppmd.insert_log_entry(path, "insert-atomic-1", "clean write")
        leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".oppmd-")]
        self.assertEqual(leftovers, [])

    def test_a_failed_replace_leaves_the_original_untouched(self):
        """The write goes to a temp file first; only os.replace makes it live. If that
        final step fails, the original note must be exactly as it was, and the temp file
        must not linger."""
        path = _tmp_copy("iso.md")
        before = open(path, encoding="utf-8").read()
        with _isolated_state_dir(), mock.patch("opp_axi.oppmd.os.replace",
                                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                oppmd.insert_log_entry(path, "insert-fail-1", "should not land")
        self.assertEqual(open(path, encoding="utf-8").read(), before)
        leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".oppmd-")]
        self.assertEqual(leftovers, [])


class LockIsExclusive(unittest.TestCase):
    def test_second_lock_attempt_is_blocked(self):
        with _isolated_state_dir():
            with oppmd._locked("lock-test-slug"):
                lock_path = os.path.join(oppmd._state_dir(), "oppmd-lock-test-slug.lock")
                fd2 = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd2)


# ── CLI wiring ───────────────────────────────────────────────────────────────
class BriefLogCli(unittest.TestCase):
    """End-to-end through argparse + resolve() + the thin cli.py wrappers — not just the
    oppmd module in isolation."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.state = tempfile.mkdtemp()          # keep the flock lock file off the real machine
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        d = os.path.join(self.repo, "clitest")
        os.makedirs(d)
        shutil.copyfile(fixture("iso.md"), os.path.join(d, "OPP.md"))
        with open(os.path.join(d, ".salesforce.json"), "w") as f:
            f.write('{"account_name": "Clitest", "opportunities": '
                    '[{"id": "006Rl00001IsoCorpAA", "name": "Clitest - Deal", '
                    '"primary": true, "status": "open"}]}')
        nolog = os.path.join(self.repo, "nologtest")
        os.makedirs(nolog)
        shutil.copyfile(fixture("no_log.md"), os.path.join(nolog, "OPP.md"))
        with open(os.path.join(nolog, ".salesforce.json"), "w") as f:
            f.write('{"account_name": "Nologtest", "opportunities": '
                    '[{"id": "006Rl00001NologLLCA", "name": "Nologtest - Deal", '
                    '"primary": true, "status": "open"}]}')

    def _run(self, *args, **envkw):
        e = dict(os.environ)
        e["OPP_REPO"] = self.repo
        e["OPP_AXI_STATE"] = self.state
        e.update(envkw)
        return subprocess.run([sys.executable, "-m", "opp_axi", *args],
                              capture_output=True, text=True, cwd=ROOT, env=e, timeout=60)

    def test_brief_via_cli(self):
        r = self._run("brief", "clitest", "--since", "2000-01-01")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## Snapshot", r.stdout)
        self.assertIn("next: opp-axi brief clitest", r.stdout)

    def test_log_via_cli_actually_writes_the_file(self):
        r = self._run("log", "clitest", "CLI smoke entry", "--date", "2026-09-20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("inserted: - 2026-09-20: CLI smoke entry"))
        note = open(os.path.join(self.repo, "clitest", "OPP.md"), encoding="utf-8").read()
        self.assertIn("- 2026-09-20: CLI smoke entry", note)

    def test_log_via_cli_refuses_without_log_section(self):
        r = self._run("log", "nologtest", "should refuse")
        self.assertEqual(r.returncode, 2)          # E_USAGE
        self.assertIn("no '## Log' heading", r.stderr)


if __name__ == "__main__":
    unittest.main()
