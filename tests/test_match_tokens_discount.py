"""`discount` is a sales-negotiation word, not a claim about Discount Tire Centers.

Measured 9/21-9/24/26 against the live repo. Two unrelated meetings each
attributed a different customer's call to discount-tire-centers:

  9/21 - Dane/Craig Weekly Sync mentions Gentex's "discount offer" ("I hate the
         D word") - a bare-word hit on "discount".
  9/24 - CDW and Spectro Cloud - Tractor Supply Discussion says GPU/CPU pricing
         differences are "absorbed via discounting later" - a suffix hit,
         because match_tokens() lets tokens longer than 4 chars match prefixes
         ("aunalytics" in "aunalytics-related" is the intended case).

Same shape as quiktrip/"quick" (test_match_tokens_aliases.py): not a missed
match, but a real meeting filed against the wrong customer, which is exactly
what meeting-without-record turns into a session writing SE Activity onto the
wrong record.
"""
import unittest

from opp_axi.cli import match_tokens, norm_hay, text_hit


class DiscountIsNotAccountEvidence(unittest.TestCase):
    def _toks(self):
        return match_tokens("discount-tire-centers", "Discount Tire Centers", ())

    def test_discount_is_not_a_token(self):
        self.assertNotIn("discount", self._toks())

    def test_the_dane_gentex_call_no_longer_matches(self):
        hay = norm_hay("Speaker 2: I hate the D word. Gentex asked about a discount offer.")
        hits = [t for t in self._toks() if text_hit(t, hay)]
        self.assertEqual([], hits, f"still matches Discount Tire on {hits}")

    def test_the_tractor_supply_call_no_longer_matches(self):
        hay = norm_hay("GPU vs CPU differences absorbed via discounting later, "
                        "no caveat on the quote.")
        hits = [t for t in self._toks() if text_hit(t, hay)]
        self.assertEqual([], hits, f"still matches Discount Tire on {hits}")

    def test_a_real_discount_tire_meeting_still_matches(self):
        hay = norm_hay("Discount Tire Centers intro call - edge VMO scoping")
        hits = [t for t in self._toks() if text_hit(t, hay)]
        self.assertTrue(hits, "a genuine Discount Tire meeting must still match")

    def test_tire_and_centers_survive(self):
        toks = self._toks()
        self.assertIn("tire", toks)
        self.assertIn("centers", toks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
