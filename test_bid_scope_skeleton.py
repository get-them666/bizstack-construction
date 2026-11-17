"""The spec-book takeoff skeleton: parses sections, prices nothing, repeats safely."""
from __future__ import annotations


_CHASE = [
    "033505 - CURING, SEALING, AND HARDENING CONCRETE FLOORS_v20.6.pdf",
    "092200 - LIGHT GAUGE METAL SUPPORT FRAMING_v20.6.pdf",
    "092900 - GYPSUM BOARD_v20.7r1.pdf",
    "096513 - RESILIENT WALL BASE_v20.6.pdf",
    "096813 - TILE CARPETING_v20.6.pdf",
    "099100 - PAINTS_v20.7r1.pdf",
    "072100 _ THERMAL INSULATION_v25.1.pdf",
    "018300 _ PERFORMANCE REQUIREMENTS 25.1.pdf",
    "265000 - LIGHTING v18.2.pdf",
]


def test_sections_parse_number_and_strip_version_stamps():
    import bid_scope_skeleton as skel

    sections = {s["number"]: s for s in skel.parse_spec_sections(_CHASE)}
    assert sections["033505"]["title"] == "CURING, SEALING, AND HARDENING CONCRETE FLOORS"
    assert sections["072100"]["title"] == "THERMAL INSULATION"
    assert sections["018300"]["title"] == "PERFORMANCE REQUIREMENTS"
    assert sections["265000"]["title"] == "LIGHTING"
    assert sections["092900"]["division"] == 9


def test_non_spec_files_are_ignored():
    import bid_scope_skeleton as skel

    assert skel.parse_spec_sections([
        "A-101 Floor Plans.pdf",
        "IMG_2043.JPG",
        "Contract - Revised.pdf",
        "254068  Hilltop Colonial Beach, VA (48100R032986)_BID SET Arch.pdf",
        "",
        None,
    ]) == []


def test_duplicate_sections_collapse():
    import bid_scope_skeleton as skel

    got = skel.parse_spec_sections([
        "019100 - Commissioning Requirements.pdf",
        "019100 - COMMISSIONING_v20.6.pdf",
    ])
    assert len(got) == 1


def test_demo_lines_exist_without_any_spec_book():
    import bid_scope_skeleton as skel

    lines = skel.build_skeleton([])
    assert len(lines) == 4
    assert all(ln["division"] is None for ln in lines)
    assert lines[0]["description"].startswith("Demolition")


def test_packages_only_appear_when_the_spec_book_has_them():
    import bid_scope_skeleton as skel

    lines = skel.build_skeleton(_CHASE)
    text = " | ".join(ln["description"] for ln in lines)
    assert "Division 09 — Resilient flooring" in text
    assert "Division 09 — Paint" in text
    assert "Division 09 — Carpet tile" in text
    assert "Division 26 — Lighting" in text
    # No plumbing or fire-alarm sections were uploaded, so no line asks for a
    # price on either trade.
    assert "Division 22" not in text
    assert "Division 28" not in text


def test_every_line_is_unpriced_and_uniquely_named():
    import bid_scope_skeleton as skel

    lines = skel.build_skeleton(_CHASE)
    names = [ln["description"] for ln in lines]
    assert len(names) == len(set(names))
    for ln in lines:
        assert ln["unit"], ln["description"]
        assert ln["category"] in ("labor", "material", "sub", "equipment", "other")
        # A skeleton that arrives pre-priced is an invented estimate.
        assert "qty" not in ln and "unit_cost" not in ln and "estimated" not in ln


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, existing):
        self._existing = existing

    def cursor(self, *args, **kwargs):
        return _Cursor(self._existing)


def test_seed_skips_what_is_already_there_and_never_prices_it():
    import bid_scope_skeleton as skel

    existing = [{"description": "Final clean & punch list"},
                {"description": "Something the owner typed"}]
    already = skel.build_skeleton(_CHASE)[0]["description"]

    calls = []
    original = skel.bid_costing.add_cost_line

    def fake(conn, bid_id, **kwargs):
        calls.append(kwargs)
        return len(calls)

    skel.bid_costing.add_cost_line = fake
    try:
        result = skel.seed_bid(_Conn(existing), 18393, filenames=_CHASE)
    finally:
        skel.bid_costing.add_cost_line = original

    assert result["skipped"] == 1
    assert result["added"] == result["considered"] - 1
    assert all(c["description"] != "Final clean & punch list" for c in calls)
    for call in calls:
        assert "qty" not in call and "unit_cost" not in call
        assert "estimated" not in call and "actual" not in call
    assert already != "Final clean & punch list"


def test_second_run_adds_nothing():
    import bid_scope_skeleton as skel

    seen = []

    def fake(conn, bid_id, **kwargs):
        seen.append(kwargs["description"])
        return len(seen)

    original = skel.bid_costing.add_cost_line
    skel.bid_costing.add_cost_line = fake
    try:
        first = skel.seed_bid(_Conn([]), 18393, filenames=_CHASE)
        conn = _Conn([{"description": d} for d in seen])
        second = skel.seed_bid(conn, 18393, filenames=_CHASE)
    finally:
        skel.bid_costing.add_cost_line = original

    assert first["added"] > 0
    assert second["added"] == 0


class _Recorder:
    """Cursor/connection pair that records SQL instead of running it."""

    def __init__(self, tables, names):
        self.tables = tables
        self.names = names
        self.sql = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        if "to_regclass" in sql:
            table = (params or [""])[0]
            self._rows = [{"t": table if table in self.tables else None}]
        else:
            self._rows = [{"name": n} for n in self.names]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, *args, **kwargs):
        return self


def test_bid_filenames_copes_with_two_different_table_shapes():
    import bid_scope_skeleton as skel

    conn = _Recorder(tables={"bid_files", "bid_documents"},
                     names=["092900 - GYPSUM BOARD_v20.7r1.pdf"])
    names = skel._bid_filenames(conn, 18393, "construction")
    assert names == ["092900 - GYPSUM BOARD_v20.7r1.pdf"] * 2
    selects = [s for s, _ in conn.sql if "SELECT" in s and "to_regclass" not in s]
    # bid_files has no company column; filtering on one raises on the live card.
    assert any("FROM bid_files" in s and "company" not in s for s in selects), selects
    assert any("FROM bid_documents" in s and "company" in s for s in selects), selects


def test_bid_filenames_survives_a_missing_table():
    import bid_scope_skeleton as skel

    conn = _Recorder(tables=set(), names=["never used"])
    assert skel._bid_filenames(conn, 1, "construction") == []


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print(f"\n{len(fns)} spec-book skeleton tests pass")


if __name__ == "__main__":
    _run()
