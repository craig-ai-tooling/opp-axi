"""scaffold must not write a bare `@OPP.md` import into the per-account CLAUDE.md
it creates.

A bare `@path` in CLAUDE.md eagerly loads the whole file into every session opened
in that directory -- up to 26k tokens always-on, for every one of the accounts this
tool scaffolds. customer-opportunities PR #36 replaces the bare import with a plain
link the assistant reads on demand instead, and that repo's automation/checks.py
`no-bare-import` check now FAILs CI on any bare `@path` line in a CLAUDE.md. Until
this generator matches, every new account re-introduces the bug PR #36 fixes.

The link text itself must be byte-identical to what customer-opportunities writes,
slug aside (verified against e.g. customer-opportunities/bio-rad/CLAUDE.md) --
including that there is no trailing newline. That is not cosmetic: a scaffolded
CLAUDE.md that merely avoids the bare-import regex but differs byte-for-byte from
the other 68 files in the fleet is a second, quieter divergence bug.

customer-opportunities W6 (9/14/26) moved the pointer from a bare `OPP.md` read to
`opp-axi brief <slug>` first, falling back to `OPP.md` only when the brief isn't
enough -- this test tracks that text, not the pre-W6 one.
"""
import os
import re
import tempfile
import unittest
from unittest import mock

from opp_axi import cli

# Same pattern as customer-opportunities/automation/checks.py IMPORT_RE (kept in
# sync on purpose -- see that file's `no-bare-import` check).
IMPORT_RE = re.compile(r'^[ \t]*@(\S+)', re.M)

# Byte-identical to customer-opportunities/bio-rad/CLAUDE.md (and 67 other
# per-account CLAUDE.md files) except the slug -- no trailing newline. FAKE_OPP
# below scaffolds into a dir named "acme", so the slug here is "acme".
EXPECTED_CLAUDE_MD = ("Run `opp-axi brief acme` before any work on this account. "
                       "Read [OPP.md](OPP.md) only when the brief isn't enough.")


class ScaffoldClaudeMd(unittest.TestCase):
    FAKE_OPP = {
        "Id": "006000000000001AAA",
        "Name": "Acme - Palette POC",
        "AccountId": "001000000000001AAA",
        "Account": {"Name": "Acme"},
        "StageName": "Discovery",
        "Amount": 100000,
        "CloseDate": "2026-12-31",
        "SE_Forecast__c": "Green",
        "Use_Case_Primary__c": "Modernization",
    }

    def _scaffold_one(self, d):
        with mock.patch.object(cli, "REPO", d), \
             mock.patch.object(cli, "sf_query", return_value=[self.FAKE_OPP]):
            a = mock.Mock(dry_run=False)
            cli.cmd_scaffold(a)
        with open(os.path.join(d, "acme", "CLAUDE.md")) as f:
            return f.read()

    def test_claude_md_has_no_bare_import_line(self):
        """The contract customer-opportunities CI enforces: no line matching
        its no-bare-import IMPORT_RE."""
        with tempfile.TemporaryDirectory() as d:
            claude_md = self._scaffold_one(d)
        offenders = [ln for ln in claude_md.splitlines() if IMPORT_RE.match(ln)]
        self.assertEqual(
            offenders, [],
            "a bare @-import eagerly loads all of OPP.md into every session, "
            "and fails customer-opportunities' no-bare-import CI check")

    def test_claude_md_is_byte_identical_to_customer_opportunities(self):
        with tempfile.TemporaryDirectory() as d:
            claude_md = self._scaffold_one(d)
        self.assertEqual(claude_md, EXPECTED_CLAUDE_MD)


if __name__ == "__main__":
    unittest.main()
