"""Client portal password hashing.

Email/password is the portal's front door, so the hash must be salted and a
malformed stored value must fail closed rather than let someone in.
"""
import portal_service


def test_password_round_trip():
    stored = portal_service.hash_password("correct horse battery staple")
    assert portal_service.verify_password("correct horse battery staple", stored)
    assert not portal_service.verify_password("wrong", stored)


def test_password_hashes_are_salted():
    assert portal_service.hash_password("same password") != portal_service.hash_password("same password")


def test_malformed_hash_fails_closed():
    assert not portal_service.verify_password("anything", "")
    assert not portal_service.verify_password("anything", "not-a-hash")
