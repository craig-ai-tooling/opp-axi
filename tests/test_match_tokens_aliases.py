"""A multi-word alias is a phrase, not a bag of words.

`match_tokens` splits the slug and the account name into words, which it must --
"blue-yonder" has to match a meeting that says "Blue Yonder". It used to split
ALIASES the same way, which is different: an alias is a phrase someone wrote on
purpose, and its individual words are not claims about the account.

Measured 9/17/26 against the live repo. quiktrip carries the aliases "quik trip"
and "quick trip". Splitting them put the bare word `quick` in its token set, and
`quick` matched the Wispr meeting "shi/tesla quick precall" -- a Tesla call
attributed to QuikTrip. `meeting-without-record` fires off exactly that match, so
the next step would have been a session writing SE Activity onto the wrong
customer's record.
"""
import unittest

from opp_axi import cli


class AliasesAreNotSplitIntoWords(unittest.TestCase):
    QUIKTRIP_ALIASES = ("quiktrip", "quik trip", "quick trip", "qt")

    def _toks(self):
        return cli.match_tokens("quiktrip", "QuikTrip Corp", self.QUIKTRIP_ALIASES)

    def test_a_common_word_from_a_multiword_alias_is_not_a_token(self):
        toks = self._toks()
        for bad in ("quick", "quik", "trip"):
            self.assertNotIn(bad, toks,
                             f"{bad!r} came from splitting a multi-word alias and is "
                             "not evidence about this account")

    def test_the_phrases_and_the_real_name_survive(self):
        toks = self._toks()
        for good in ("quiktrip", "quick trip", "quik trip", "qt"):
            self.assertIn(good, toks)

    def test_the_tesla_precall_no_longer_matches_quiktrip(self):
        """The exact meeting that mis-matched, end to end through text_hit."""
        hay = cli.norm_hay("shi/tesla quick precall Quick precall to align on the "
                           "upcoming SHI/Tesla meeting")
        hits = [t for t in self._toks() if cli.text_hit(t, hay)]
        self.assertEqual([], hits, f"still matches QuikTrip on {hits}")

    def test_the_slug_and_account_name_are_still_split(self):
        """The fix must not stop a hyphenated slug matching its own name."""
        toks = cli.match_tokens("blue-yonder", "Blue Yonder", ())
        self.assertIn("blue", toks)
        self.assertIn("yonder", toks)

    def test_a_single_word_alias_is_unaffected(self):
        toks = cli.match_tokens("gehealthcare", "GE HealthCare", ("gehc",))
        self.assertIn("gehc", toks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
