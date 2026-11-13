"""The Send box's Find GC email step must pick the GC, never a bid board."""
import bid_contact


def test_existing_contact_wins():
    bid = {"contact_email": "wmohr@embreegroup.com", "contact_name": "Wesley Mohr",
           "owner": "Embree Construction Group", "notes": ""}
    out = bid_contact.find_gc_email(bid)
    assert out["ok"] and out["email"] == "wmohr@embreegroup.com"
    assert out["source"] == "bid card"


def test_picks_the_gc_domain_and_estimating_mailbox():
    bid = {"contact_email": "", "contact_name": "", "owner": "Loberg Construction",
           "notes": ("Send bids to estimating@lobergconstruction.com and cc "
                     "aeilermann@lobergconstruction.com. Do not reply to "
                     "no-reply@buildingconnected.com.")}
    out = bid_contact.find_gc_email(bid)
    assert out["ok"]
    assert out["email"] == "estimating@lobergconstruction.com"
    assert "aeilermann@lobergconstruction.com" in out["candidates"]
    assert all("buildingconnected" not in e for e in out["candidates"])


def test_ignores_bid_board_and_robot_addresses():
    bid = {"contact_email": "", "contact_name": "", "owner": "Loberg Construction",
           "notes": "team@buildingconnected.com no-reply@bidprime.com help@autodesk.com"}
    out = bid_contact.find_gc_email(bid)
    assert not out["ok"]


def test_role_mailbox_does_not_invent_a_person():
    bid = {"contact_email": "", "contact_name": "", "owner": "Loberg Construction",
           "notes": "Please email estimating@lobergconstruction.com for your trade."}
    out = bid_contact.find_gc_email(bid)
    assert out["ok"] and out["email"] == "estimating@lobergconstruction.com"
    assert out["name"] == ""


def test_person_mailbox_gets_a_readable_name():
    bid = {"contact_email": "", "contact_name": "", "owner": "Embree Construction Group",
           "notes": "Questions: wesley.mohr@embreegroup.com"}
    out = bid_contact.find_gc_email(bid)
    assert out["ok"] and out["email"] == "wesley.mohr@embreegroup.com"
    assert out["name"] == "Wesley Mohr"


def test_source_email_sender_is_a_fallback():
    bid = {"contact_email": "", "contact_name": "", "owner": "TurnKey Asset Management",
           "notes": "See attached scope."}
    out = bid_contact.find_gc_email(bid, ["contractors@turnkeyassetmgt.com"])
    assert out["ok"] and out["email"] == "contractors@turnkeyassetmgt.com"


def test_unrelated_address_is_not_guessed():
    bid = {"contact_email": "", "contact_name": "", "owner": "Loberg Construction",
           "notes": "The tenant's broker is someone@unrelated.example."}
    out = bid_contact.find_gc_email(bid)
    assert not out["ok"]


def test_empty_bid_says_to_type_it():
    out = bid_contact.find_gc_email({"contact_email": "", "owner": "", "notes": ""})
    assert not out["ok"]
    assert "Type the recipient" in out["error"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
