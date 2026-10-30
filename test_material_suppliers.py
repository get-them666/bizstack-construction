"""Manual material pricing: seeding, and the price a receipt produces.

    venv/bin/python -m pytest test_material_suppliers.py -v

No database. The cursor answers by statement shape, so what is pinned is the
behaviour that would otherwise break silently:

  * the known yards are seeded only into an empty book, so a supplier the
    owner deliberately deleted does not reappear on the next page load;
  * a price entered by hand is stored in cents, with the item stripped and a
    unit defaulted, because that is the row the picker later reads;
  * a blank, negative or unlabelled price is refused rather than written as
    zero -- a zeroed price book is worse than an empty one.
"""
import pytest

import supplier_prices as sp


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.conn.sql.append((s, params))
        if s.startswith("select count(*) as n from material_suppliers"):
            self._rows = [{"n": self.conn.existing}]
        elif s.startswith("insert into material_suppliers"):
            self.conn.next_id += 1
            self._rows = [{"id": self.conn.next_id}]
        elif s.startswith("insert into material_supplier_prices"):
            self.conn.prices.append(params)
            self._rows = [{"id": len(self.conn.prices)}]
        else:
            self._rows = []
        self.rowcount = len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, existing=0):
        self.existing = existing
        self.next_id = 0
        self.prices = []
        self.sql = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass


def _inserted_suppliers(conn):
    return [p for s, p in conn.sql if s.startswith("insert into material_suppliers")]


def test_seeds_the_known_yards_when_the_book_is_empty():
    conn = _Conn(existing=0)
    assert sp.seed_default_suppliers(conn) == len(sp.DEFAULT_SUPPLIERS)
    assert len(_inserted_suppliers(conn)) == len(sp.DEFAULT_SUPPLIERS)
    # Seeded yards carry no prices; they are places for a price to land.
    assert conn.prices == []


def test_does_not_reseed_a_populated_book():
    conn = _Conn(existing=3)
    assert sp.seed_default_suppliers(conn) == 0
    assert _inserted_suppliers(conn) == []


def test_a_hand_entered_price_is_stored_in_cents_and_normalised():
    conn = _Conn(existing=1)
    n = sp.add_price(conn, 7, item_number="  2X4-8-SPF  ", description="",
                     uom="", price_cents=428)
    assert n == 1
    supplier_id, item, description, uom, cents, company = conn.prices[0]
    assert supplier_id == 7
    assert item == "2X4-8-SPF"
    assert description == "2X4-8-SPF"       # falls back to the item number
    assert uom == "each"                     # blank unit defaults
    assert cents == 428
    assert company == "construction"


def test_a_hand_entered_price_keeps_its_description_and_unit():
    conn = _Conn(existing=1)
    sp.add_price(conn, 7, item_number="OSB-716", description="7/16 OSB 4x8",
                 uom="sheet", price_cents=1495)
    _, item, description, uom, cents, _ = conn.prices[0]
    assert (item, description, uom, cents) == ("OSB-716", "7/16 OSB 4x8", "sheet", 1495)


@pytest.mark.parametrize("kwargs", [
    {"item_number": "X", "price_cents": None},
    {"item_number": "X", "price_cents": -1},
    {"item_number": "", "description": "", "price_cents": 100},
])
def test_an_unusable_price_is_refused_not_written_as_zero(kwargs):
    conn = _Conn(existing=1)
    with pytest.raises(sp.SupplierError):
        sp.add_price(conn, 7, **kwargs)
    assert conn.prices == []
