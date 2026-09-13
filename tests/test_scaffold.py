"""scaffold must not write a bare `@OPP.md` import into the per-account CLAUDE.md
it creates.

A bare `@path` in CLAUDE.md eagerly loads the whole file into every session opened
in that directory -- up to 26k tokens always-on, for every one of the accounts this
tool scaffolds. customer-opportunities PR #36 replaces the bare import with a plain
link the assistant reads on demand instead. Until this generator matches, every new
account re-introduces the bug PR #36 fixes.
"""
import os
import tempfile
import unittest
from unittest import mock

from opp_axi import cli


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
        return open(os.path.join(d, "acme", "CLAUDE.md")).read()

    def test_claude_md_is_a_link_not_a_bare_import(self):
        with tempfile.TemporaryDirectory() as d:
            claude_md = self._scaffold_one(d)
        self.assertNotIn(
            "@OPP.md", claude_md,
            "a bare @-import eagerly loads all of OPP.md into every session")
        self.assertIn("[OPP.md](OPP.md)", claude_md)
        self.assertIn("Read", claude_md)


if __name__ == "__main__":
    unittest.main()
