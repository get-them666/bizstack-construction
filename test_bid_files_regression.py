"""Regressions for the bid File Cabinet.

Two bugs shipped here and both were invisible to the old DB-backed test:

1. ``_bid_files_list`` re-zipped rows against ``cur.description``, but the app
   connects with ``row_factory=dict_row``. Iterating a dict yields its keys, so
   every field came back as its own column name -- ``file_size`` was the string
   "file_size" and the bid page died on ``'str' / 1024``.
2. The upload route wrote one folder for the whole batch, so a dropped folder of
   plans, RFIs and permits all landed in the single selected folder.

No database is needed: the row shape is faked, which is exactly the thing that
broke.

Run directly (``venv/bin/python test_bid_files_regression.py``) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return self._rows


class _FakeDb:
    """Just enough connection for _bid_files_list: dict rows, as the app runs."""

    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)

    def commit(self):
        return None


def test_file_size_is_numeric_when_rows_are_dicts():
    from construction_main import _bid_files_list

    db = _FakeDb([{
        "id": 7, "bid_id": 3, "filename": "abc-plan.pdf",
        "original_filename": "A-101 Floor Plans.pdf",
        "file_path": "3/Plans & Drawings/abc-plan.pdf",
        "file_size": 2_048, "mime_type": "application/pdf",
        "folder": "Plans & Drawings", "uploaded_at": None,
        "uploaded_by": "hello@bizstackperks.com",
    }])
    rows = _bid_files_list(db, 3)
    assert len(rows) == 1
    row = rows[0]
    assert row["file_size"] == 2048
    assert isinstance(row["file_size"], int)
    assert row["size_kb"] == 2.0
    assert row["original_filename"] == "A-101 Floor Plans.pdf"
    assert row["id"] == 7


def test_string_file_size_from_legacy_row_is_coerced():
    from construction_main import _bid_files_list

    db = _FakeDb([{"id": 1, "bid_id": 2, "filename": "x.pdf",
                   "original_filename": "x.pdf", "file_path": "2/x.pdf",
                   "file_size": "4096", "mime_type": "", "folder": "Other",
                   "uploaded_at": None, "uploaded_by": ""}])
    row = _bid_files_list(db, 2)[0]
    assert row["file_size"] == 4096
    assert row["size_kb"] == 4.0


def test_auto_sort_routes_by_filename():
    from construction_main import _bid_files_folder_for

    cases = [
        ("A-101 Floor Plans.pdf", "Plans & Drawings"),
        ("254068  Hilltop Colonial Beach, VA (48100R032986)_BID SET Arch.pdf",
         "Plans & Drawings"),
        ("003153 - OWNER PAID TESTING AND INSPECTION SERVICES.pdf", "Bid Documents"),
        ("ExJ - Limited Asbestos Screen Report.pdf", "Inspections"),
        ("RFI 014 - Slab edge.pdf", "RFIs & Addenda"),
        ("270000 - RETAIL STRUCTURED CABLING.pdf", "Scope & Specifications"),
        ("013000 - ADMINISTRATIVE REQUIREMENTS v20.3.pdf", "Scope & Specifications"),
        ("ExO-NationalRecyclingProgram.pdf", "Scope & Specifications"),
        ("001116 - INVITATION TO BID.pdf", "Bid Documents"),
        ("Addendum 2.pdf", "RFIs & Addenda"),
        ("Scope of Work - Demolition.pdf", "Scope & Specifications"),
        ("Contract - Revised.pdf", "Contracts & Proposals"),
        ("Building Permit Application.pdf", "Permits"),
        ("Schedule - Phase 1.pdf", "Schedules"),
        ("IMG_2043.JPG", "Photos"),
        ("data.csv", "Other"),
    ]
    for filename, expected in cases:
        got = _bid_files_folder_for(filename, "")
        assert got == expected, f"{filename}: expected {expected}, got {got}"


def test_explicit_folder_beats_auto_sort():
    from construction_main import _bid_files_folder_for

    assert _bid_files_folder_for("A-101 Floor Plans.pdf", "Permits") == "Permits"
    # "Auto-sort by filename" is the empty option in the select.
    assert _bid_files_folder_for("A-101 Floor Plans.pdf", "Auto-sort") == \
        "Plans & Drawings"


def test_template_does_not_do_math_on_the_raw_size():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "templates", "construction", "bid_detail.html")
    html = open(path, encoding="utf-8").read()
    assert "file_size / 1024" not in html, (
        "The template must use size_kb from _bid_files_list; file_size is not "
        "guaranteed numeric in the template context")
    assert "f.size_kb" in html


def test_folder_stats_bucket_sizes_and_order():
    from construction_main import _bid_files_folder_stats

    rows = [
        {"folder": "Photos", "file_size": 2 * 1048576},
        {"folder": "Photos", "file_size": 1048576},
        {"folder": "Plans & Drawings", "file_size": 512},
        {"folder": "Vendor Boilerplate", "file_size": 1024},
        {"folder": None, "file_size": 0},
    ]
    stats = {s["name"]: s for s in _bid_files_folder_stats(rows)}
    assert stats["Photos"]["count"] == 2
    assert stats["Photos"]["mb"] == 3.0
    assert stats["Plans & Drawings"]["count"] == 1
    assert stats["Other"]["count"] == 1
    # Default folders come first, custom ones after, per the shared order.
    names = [s["name"] for s in _bid_files_folder_stats(rows)]
    assert names.index("Plans & Drawings") < names.index("Vendor Boilerplate")
    # Empty input must not invent rows -- the purge checklist only lists files
    # that exist.
    assert _bid_files_folder_stats([]) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall bid Files Cabinet regressions pass")
