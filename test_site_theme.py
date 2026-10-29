"""Appearance state: decor switch, fallback when the settings read fails.

    venv/bin/python -m pytest test_site_theme.py -v

No database: _db() is patched to a stub connection.
"""
import json

import site_theme as st


class _Cur:
    def __init__(self, rows, fail=False):
        self._rows, self._fail = rows, fail
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k):
        if self._fail:
            raise RuntimeError('relation "app_settings" does not exist')
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows, fail=False):
        self._rows, self._fail = rows, fail
        self.closed = False
    def cursor(self): return _Cur(self._rows, self._fail)
    def close(self): self.closed = True


def _use(monkeypatch, theme: dict | None, fail=False):
    rows = [] if theme is None else [{"key": "theme", "value": json.dumps(theme)}]
    monkeypatch.setattr(st, "_db", lambda: _Conn(rows, fail))
    st._cache["state"] = None
    st._cache["at"] = 0.0


def test_decor_defaults_on_for_existing_themes(monkeypatch):
    _use(monkeypatch, {"auto": False, "skin": "autumn"})
    s = st.state(force=True)
    assert s["decor_on"] is True
    assert "bsdec" in s["decor"], "autumn has a leaves mood, so decor renders"


def test_decor_can_be_switched_off_without_losing_the_look(monkeypatch):
    _use(monkeypatch, {"auto": False, "skin": "autumn", "decor": False})
    s = st.state(force=True)
    assert s["decor_on"] is False
    assert s["decor"] == ""
    assert s["accent"] == st.PRESETS["autumn"]["accent"], "colours are untouched"


def test_a_failed_settings_read_never_breaks_a_page(monkeypatch):
    """base.html renders this on every request; it must degrade, not 500."""
    _use(monkeypatch, None, fail=True)
    s = st.state(force=True)
    assert s["skin"] == "default" and s["css"] == ""
    # And it falls back to the last good theme when there is one.
    _use(monkeypatch, {"auto": False, "skin": "holiday"})
    good = st.state(force=True)
    monkeypatch.setattr(st, "_db", lambda: _Conn([], fail=True))
    st._cache["at"] = 0.0
    assert st.state(force=True)["skin"] == good["skin"] == "holiday"


def test_custom_colours_survive_a_round_trip(monkeypatch):
    """The live site runs a hand-picked theme; a preset must not clobber it."""
    _use(monkeypatch, {"auto": False, "skin": "default", "bg": "#0a0a0a", "accent": "#48413d", "font": "serif", "emoji": "🎃"})
    s = st.state(force=True)
    assert (s["bg"], s["accent"], s["font_name"], s["emoji"]) == ("#0a0a0a", "#48413d", "serif", "🎃")
    assert "#48413d" in s["css"]
