"""Regression tests for the calendar/mail -> opportunity matcher.

The domain rule used to be an unanchored substring test (`tok in " ".join(domains)`), so
the token "health" matched gehealthcare.com and GE HealthCare threads were served as
evidence for MedImpact Healthcare, Elevance Health and Centauri Health. A domain hit
outranks every text hit, so the wrong account won outright, and cmd_evidence then LEARNED
the wrong domain and carried it into the Gmail query — one bad row contaminated the rest.
"""
import unittest

from opp_axi.cli import (INDUSTRY, PARTNER_DOMAINS, build_matcher, cover_vocab, domain_hits,
                         domain_labels,
                         explicit_tokens, match_tokens, norm_hay, segmentable, text_hit)


def _acct_parts(slug, account, aliases=()):
    return (match_tokens(slug, account, aliases),
            cover_vocab(slug, account, aliases),
            explicit_tokens(slug, aliases))


class DomainLabels(unittest.TestCase):
    def test_public_suffix_dropped(self):
        self.assertEqual(domain_labels(["acme.com"]), {"acme"})
        self.assertIn("acme", domain_labels(["acme.co.uk"]))
        self.assertNotIn("com", domain_labels(["acme.com"]))

    def test_hyphen_both_ways(self):
        labs = domain_labels(["tom-is.com"])
        self.assertIn("tom-is", labs)
        self.assertIn("tomis", labs)

    def test_splits_on_separators_and_drops_infra(self):
        self.assertEqual(domain_labels(["a.com;b.net"]), {"a", "b"})
        self.assertNotIn("www", domain_labels(["www.acme.com"]))


class Segmentable(unittest.TestCase):
    def test_account_must_explain_whole_label(self):
        ge = {"ge", "precision", "healthcare", "llc"}
        med = {"medimpact", "healthcare"}
        self.assertTrue(segmentable("gehealthcare", ge))
        self.assertFalse(segmentable("gehealthcare", med))

    def test_no_single_char_segments(self):
        self.assertFalse(segmentable("abc", {"a", "b", "c"}))


class DomainHits(unittest.TestCase):
    def test_the_bug_generic_token_cannot_claim_another_domain(self):
        """'health'/'healthcare' must not pull gehealthcare.com to another account."""
        for slug, acct in (("medimpact-healthcare", "MedImpact Healthcare"),
                           ("elevance", "Elevance Health"),
                           ("centauri-health", "Centauri Health Solutions")):
            toks, vocab, expl = _acct_parts(slug, acct)
            self.assertEqual(
                domain_hits(toks, vocab, expl, ["gehealthcare.com"]), [],
                f"{slug} must not match gehealthcare.com")

    def test_rightful_owner_still_matches(self):
        toks, vocab, expl = _acct_parts("ge-healthcare", "GE Precision Healthcare, LLC")
        self.assertTrue(domain_hits(toks, vocab, expl, ["gehealthcare.com"]))

    def test_concatenated_label_matches_on_suffix_token(self):
        toks, vocab, expl = _acct_parts("digital-realty", "Digital Realty")
        self.assertTrue(domain_hits(toks, vocab, expl, ["digitalrealty.com"]))

    def test_short_explicit_alias_still_matches(self):
        """A short slug/alias is declared on purpose and must survive the >=4 char gate."""
        toks, vocab, expl = _acct_parts("75f", "75F", ("75f",))
        self.assertTrue(domain_hits(toks, vocab, expl, ["75f.com"]))

    def test_unrelated_domain_does_not_match(self):
        toks, vocab, expl = _acct_parts("medimpact-healthcare", "MedImpact Healthcare")
        self.assertEqual(domain_hits(toks, vocab, expl, ["purestorage.com"]), [])


class TextHit(unittest.TestCase):
    def test_short_tokens_are_whole_word_only(self):
        self.assertFalse(text_hit("ti", norm_hay("it is time to go")))
        self.assertTrue(text_hit("ti", norm_hay("TI bare metal review")))

    def test_generic_word_does_not_match_inside_another(self):
        self.assertFalse(text_hit("health", norm_hay("GEHC Self-Hosted Palette")))

    def test_long_token_is_suffix_tolerant_by_design(self):
        """>4 char tokens match prefixes on purpose ("aunalytics" in "aunalytics-related"),
        which is also why "health" matches "healthy" — the INDUSTRY guard in the calendar
        and wispr paths is what stops that being treated as evidence, not this function."""
        self.assertTrue(text_hit("health", norm_hay("running healthy")))
        self.assertIn("health", INDUSTRY)


class Matcher(unittest.TestCase):
    IDX = {
        "ge-healthcare": {"account_name": "GE Precision Healthcare, LLC"},
        "medimpact-healthcare": {"account_name": "MedImpact Healthcare"},
        "elevance": {"account_name": "Elevance Health"},
        "digital-realty": {"account_name": "Digital Realty"},
        "ivanti": {"account_name": "Ivanti"},
    }

    def setUp(self):
        self.match = build_matcher(self.IDX)

    def test_domain_goes_to_the_right_account(self):
        self.assertEqual(self.match("", ["gehealthcare.com"]), "ge-healthcare")
        self.assertEqual(self.match("", ["medimpact.com"]), "medimpact-healthcare")
        self.assertEqual(self.match("", ["digitalrealty.com"]), "digital-realty")
        self.assertEqual(self.match("", ["ivanti.com"]), "ivanti")

    def test_unknown_domain_matches_nothing(self):
        self.assertEqual(self.match("", ["purestorage.com"]), "")
        self.assertEqual(self.match("", ["spectrocloud.com"]), "")

    def test_text_only_still_works(self):
        self.assertEqual(self.match("Ivanti technical deep dive", []), "ivanti")
        self.assertEqual(self.match("Weekly sync", []), "")


class PartnerDomains(unittest.TestCase):
    """A partner sits on many unrelated deals, so its domain is a route to market, not an
    account identity. SHI is on both Ivanti and Tesla; adopting shi.com as Ivanti's own
    domain made Tesla's SHI call read as Ivanti evidence."""

    def test_resellers_and_alliance_vendors_are_listed(self):
        for d in ("shi.com", "cdw.com", "softwareone.com", "wwt.com", "purestorage.com"):
            self.assertIn(d, PARTNER_DOMAINS)

    def test_a_customer_domain_is_not_treated_as_a_partner(self):
        for d in ("ivanti.com", "gehealthcare.com", "aunalytics.com", "tesla.com"):
            self.assertNotIn(d, PARTNER_DOMAINS)

    def test_partner_domain_does_not_match_an_unrelated_account(self):
        toks, vocab, expl = _acct_parts("ivanti", "Ivanti")
        self.assertEqual(domain_hits(toks, vocab, expl, ["shi.com", "everpuredata.com"]), [])

    def test_customer_name_in_the_title_still_matches(self):
        """The replacement signal for an internal meeting about a customer."""
        idx = {"tesla": {"account_name": "Tesla"}, "ivanti": {"account_name": "Ivanti"}}
        match = build_matcher(idx)
        self.assertEqual(match("Internal - Tesla VMware PowWOW", ["shi.com"]), "tesla")
        self.assertEqual(match("Ivanti Technical Deep Dive", []), "ivanti")


if __name__ == "__main__":
    unittest.main()
