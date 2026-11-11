"""BuildingConnected attachment discovery, without network access."""
import bid_attachments as ba


def test_opportunity_id_from_deep_link_and_reference():
    assert ba._bc_opportunity_id(
        {"external_url": "https://app.buildingconnected.com/opportunities/Ab3dEf9h/info"}
    ) == "Ab3dEf9h"
    assert ba._bc_opportunity_id({"reference": "bc:Zx81Yq22"}) == "Zx81Yq22"
    assert ba._bc_opportunity_id({"reference": "bc:short"}) == ""


def test_walk_finds_named_download_urls_in_any_shape():
    payload = {"results": [{"file": {"fileName": "A-101.pdf",
                                     "downloadUrl": "https://x.test/a101"}}]}
    out = []
    ba._walk_file_nodes(payload, out)
    assert out == [{"name": "A-101.pdf", "url": "https://x.test/a101"}]


def test_walk_ignores_urls_without_a_name():
    out = []
    ba._walk_file_nodes({"href": "https://x.test/x"}, out)
    assert out == []


def test_no_opportunity_id_says_so(monkeypatch):
    monkeypatch.setattr("bid_opportunities.get_bid",
                        lambda *a, **k: {"id": 1, "reference": "", "external_url": ""},
                        raising=False)
    # get_bid is imported inside the function, so patch the module it uses.
    import bid_opportunities
    monkeypatch.setattr(bid_opportunities, "get_bid", lambda *a, **k: {"id": 1})
    res = ba.extract_buildingconnected(None, 1, "construction", "owner@example.com")
    assert res["saved"] == 0
    assert "opportunity id" in res["reason"]
