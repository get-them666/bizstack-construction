"""Tests for zip_lookup.

Two things this suite exists to protect, both load-bearing:

  1. The module NEVER invents a ZIP. A wrong ZIP is worse than a missing one,
     because it unblocks a skip trace that can then bill against the wrong
     house. Every no-match path is asserted to return zipcode "".
  2. A provider outage is NOT cached as "this address has no ZIP". If it were,
     one afternoon of Census being down would permanently poison every
     address looked up during it, with no way to recover short of a restart.

No network. Providers are patched.
"""

import unittest
from unittest import mock

import zip_lookup


def _resp(payload):
    return mock.Mock(
        read=mock.Mock(return_value=__import__("json").dumps(payload).encode()),
        __enter__=lambda s: s,
        __exit__=lambda s, *a: False,
    )


CENSUS_HIT = {
    "result": {"addressMatches": [{
        "matchedAddress": "5540 BARNHOLLOW RD, NORFOLK, VA, 23502",
        "addressComponents": {"zip": "23502", "city": "NORFOLK", "state": "VA"},
        "coordinates": {"x": -76.2149, "y": 36.8493},
    }]}
}


class CensusProviderTests(unittest.TestCase):
    def test_returns_zip_and_components(self):
        with mock.patch.object(zip_lookup, "_get_json", return_value=CENSUS_HIT):
            out = zip_lookup._census("5540 Barnhollow Road", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23502")
        self.assertEqual(out["city"], "NORFOLK")
        self.assertEqual(out["source"], "census")
        self.assertEqual(out["confidence"], "exact")

    def test_no_matches_returns_none(self):
        with mock.patch.object(zip_lookup, "_get_json", return_value={"result": {"addressMatches": []}}):
            self.assertIsNone(zip_lookup._census("1 Nowhere Rd", "Nowhere", "VA"))

    def test_match_without_zip_returns_none(self):
        payload = {"result": {"addressMatches": [{"matchedAddress": "x",
                                                  "addressComponents": {}}]}}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            self.assertIsNone(zip_lookup._census("1 Nowhere Rd", "Nowhere", "VA"))


class ArcgisProviderTests(unittest.TestCase):
    def test_accepts_point_address(self):
        payload = {"candidates": [{"attributes": {
            "Addr_type": "PointAddress", "Match_addr": "5540 Barnhollow Rd, Norfolk, Virginia, 23502",
            "Postal": "23502", "City": "Norfolk", "Region": "Virginia", "Score": 100}}]}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            out = zip_lookup._arcgis("5540 Barnhollow Rd", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23502")
        self.assertEqual(out["source"], "arcgis")

    def test_locality_hit_is_refused(self):
        """A city-level match must not be passed off as a street ZIP."""
        payload = {"candidates": [{"attributes": {
            "Addr_type": "Locality", "Match_addr": "Norfolk, Virginia, 23510",
            "Postal": "23510"}}]}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            self.assertIsNone(zip_lookup._arcgis("5540 Barnhollow Rd", "Norfolk", "VA"))

    def test_zip_plus_four_is_trimmed(self):
        payload = {"candidates": [{"attributes": {
            "Addr_type": "StreetAddress", "Match_addr": "1 A St, Norfolk, Virginia, 23510-1234",
            "Postal": "23510-1234"}}]}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            out = zip_lookup._arcgis("1 A St", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23510")

    def test_falls_back_to_match_addr_for_zip(self):
        payload = {"candidates": [{"attributes": {
            "Addr_type": "StreetAddress", "Match_addr": "1 A St, Norfolk, Virginia 23510", "Postal": ""}}]}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            out = zip_lookup._arcgis("1 A St", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23510")


class LookupZipTests(unittest.TestCase):
    def setUp(self):
        zip_lookup._CACHE.clear()

    def test_blank_street_is_refused(self):
        out = zip_lookup.lookup_zip("", "Norfolk", "VA")
        self.assertFalse(out["ok"])
        self.assertEqual(out["zipcode"], "")

    def test_census_hit_is_returned(self):
        with mock.patch.object(zip_lookup, "_census", return_value={
                "zipcode": "23502", "source": "census", "confidence": "exact"}) as c:
            out = zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
        self.assertTrue(out["ok"])
        self.assertEqual(out["zipcode"], "23502")
        c.assert_called_once()

    def test_falls_back_to_arcgis_when_census_misses(self):
        with mock.patch.object(zip_lookup, "_census", return_value=None), \
             mock.patch.object(zip_lookup, "_arcgis", return_value={
                 "zipcode": "23510", "source": "arcgis", "confidence": "exact"}) as a:
            out = zip_lookup.lookup_zip("1 A St", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23510")
        a.assert_called_once()

    def test_both_miss_reports_no_zip_and_no_guess(self):
        with mock.patch.object(zip_lookup, "_census", return_value=None), \
             mock.patch.object(zip_lookup, "_arcgis", return_value=None):
            out = zip_lookup.lookup_zip("999 Nowhere Pl", "Nowhere", "ZZ")
        self.assertFalse(out["ok"])
        self.assertEqual(out["zipcode"], "")
        self.assertEqual(out["source"], "")

    def test_success_is_cached(self):
        with mock.patch.object(zip_lookup, "_census", return_value={
                "zipcode": "23502", "source": "census", "confidence": "exact"}) as c:
            zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
            second = zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
        self.assertEqual(second["zipcode"], "23502")
        self.assertTrue(second["cached"])
        self.assertEqual(c.call_count, 1)

    def test_cache_key_is_case_and_space_insensitive(self):
        with mock.patch.object(zip_lookup, "_census", return_value={
                "zipcode": "23502", "source": "census", "confidence": "exact"}) as c:
            zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
            zip_lookup.lookup_zip("  5540  BARNHOLLOW  RD ", "norfolk", "va")
        self.assertEqual(c.call_count, 1)

    def test_miss_is_not_cached(self):
        with mock.patch.object(zip_lookup, "_census", return_value=None) as c, \
             mock.patch.object(zip_lookup, "_arcgis", return_value=None) as a:
            zip_lookup.lookup_zip("999 Nowhere Pl", "Nowhere", "ZZ")
            zip_lookup.lookup_zip("999 Nowhere Pl", "Nowhere", "ZZ")
        self.assertEqual(c.call_count, 2)
        self.assertEqual(a.call_count, 2)

    def test_outage_raises_and_is_not_cached(self):
        """The poisoning case: an outage must never become a permanent 'no ZIP'."""
        boom = zip_lookup.ZipLookupError("HTTP 503")
        with mock.patch.object(zip_lookup, "_census", side_effect=boom), \
             mock.patch.object(zip_lookup, "_arcgis", side_effect=boom):
            with self.assertRaises(zip_lookup.ZipLookupError):
                zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
        self.assertEqual(zip_lookup._CACHE, {})

    def test_one_provider_down_still_returns_from_the_other(self):
        with mock.patch.object(zip_lookup, "_census", side_effect=zip_lookup.ZipLookupError("HTTP 503")), \
             mock.patch.object(zip_lookup, "_arcgis", return_value={
                 "zipcode": "23502", "source": "arcgis", "confidence": "exact"}):
            out = zip_lookup.lookup_zip("5540 Barnhollow Rd", "Norfolk", "VA")
        self.assertEqual(out["zipcode"], "23502")
        self.assertEqual(out["source"], "arcgis")


class SuggestTests(unittest.TestCase):
    def test_short_input_returns_nothing(self):
        self.assertEqual(zip_lookup.suggest_address("5"), [])

    def test_blank_returns_nothing(self):
        self.assertEqual(zip_lookup.suggest_address(""), [])

    def test_labels_and_zips_returned(self):
        payload = {"candidates": [{"attributes": {
            "Match_addr": "5540 Barnhollow Rd, Norfolk, Virginia, 23502",
            "Postal": "23502", "City": "Norfolk", "Region": "Virginia",
            "Addr_type": "PointAddress"}, "location": {"y": 36.8, "x": -76.2}}]}
        with mock.patch.object(zip_lookup, "_get_json", return_value=payload):
            out = zip_lookup.suggest_address("5540 Barnhollow")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["zipcode"], "23502")

    def test_provider_failure_returns_empty_not_raises(self):
        """Autocomplete is a UI nicety; it must never break the page."""
        with mock.patch.object(zip_lookup, "_get_json",
                               side_effect=zip_lookup.ZipLookupError("down")):
            self.assertEqual(zip_lookup.suggest_address("5540 Barn"), [])


class ConfigTests(unittest.TestCase):
    def test_always_configured(self):
        self.assertTrue(zip_lookup.is_configured())


if __name__ == "__main__":
    unittest.main()
