#!/usr/bin/env python3
"""
Offline tests for the trust-critical logic: price parsing (the currency gate),
state migration, trend analysis, and the three report sections.

Run:  python -m pytest test_checker.py -q   (or)   python test_checker.py
No network required.
"""

import checker as C
from checker import SearchResult


# ── parse_price: the accuracy gate ───────────────────────────────────────────

def test_parse_inr_ok():
    assert C.parse_price("₹17760") == 17760
    assert C.parse_price("₹8,200") == 8200
    assert C.parse_price(17760) == 17760


def test_parse_rejects_foreign_currency():
    # The whole point: a USD leak must NOT be read as rupees.
    assert C.parse_price("$187") is None
    assert C.parse_price("€200") is None
    assert C.parse_price("£150") is None


def test_parse_rejects_zero_and_garbage():
    assert C.parse_price("0") is None
    assert C.parse_price("") is None
    assert C.parse_price(None) is None
    assert C.parse_price("sold out") is None


def test_parse_rejects_implausible():
    assert C.parse_price("₹50") is None          # too low (likely USD leak)
    assert C.parse_price("₹9000000") is None     # absurdly high


# ── state migration ──────────────────────────────────────────────────────────

def test_migrate_flat_to_v2():
    old = {"COK-DXB-2026-10-02": 17760, "TRV-LHR-2026-10-02": 41000}
    new = C.migrate_state(old)
    assert new["version"] == C.STATE_VERSION
    assert new["routes"]["COK-DXB-2026-10-02"]["history"][0]["p"] == 17760


def test_migrate_empty():
    assert C.migrate_state({})["routes"] == {}


def test_append_and_limit():
    state = {"version": C.STATE_VERSION, "routes": {}}
    for p in range(C.HISTORY_LIMIT + 10):
        C.append_observation(state, "K", 10000 + p, "typical", 0)
    hist = state["routes"]["K"]["history"]
    assert len(hist) == C.HISTORY_LIMIT          # trimmed
    assert hist[-1]["p"] == 10000 + C.HISTORY_LIMIT + 9  # newest kept


# ── analyse / trends ─────────────────────────────────────────────────────────

def _dest(code="DXB", city="Dubai", country="UAE"):
    return {"code": code, "city": city, "country": country, "country_iso": "AE"}


def _res(price, signal="typical", stops=0):
    return SearchResult(price, "IndiGo", "06:00", "08:00", "3h", stops, signal, True)


def test_analyse_new_route():
    rr = C.analyse("COK", _dest(), "2026-10-02", _res(17760), [])
    assert rr.prev_price is None
    assert rr.trend == "new"
    assert rr.all_time_low == 17760


def test_analyse_detects_drop():
    hist = [{"p": 20000, "sig": "", "stops": 0, "t": ""}]
    rr = C.analyse("COK", _dest(), "2026-10-02", _res(16000), hist)
    assert rr.prev_price == 20000
    assert rr.diff == -4000
    assert rr.trend == "down"
    assert "📉" in rr.change_label()


def test_analyse_all_time_low_value_tag():
    hist = [{"p": 20000, "sig": "", "stops": 0, "t": ""},
            {"p": 18000, "sig": "", "stops": 0, "t": ""}]
    rr = C.analyse("COK", _dest(), "2026-10-02", _res(15000), hist)
    assert rr.all_time_low == 15000
    assert "all-time low" in rr.value_label()


# ── report sections ──────────────────────────────────────────────────────────

def _rr(origin, code, city, date, price, prev=None, signal="typical"):
    hist = [{"p": prev, "sig": "", "stops": 0, "t": ""}] if prev is not None else []
    return C.analyse(origin, _dest(code, city), date, _res(price, signal), hist)


def test_section_cheapest_sorted_and_capped():
    rs = [_rr("COK", "DXB", "Dubai", "2026-10-02", 30000),
          _rr("COK", "LHR", "London", "2026-10-02", 16000),
          _rr("TRV", "CDG", "Paris", "2026-10-02", 22000)]
    out = "\n".join(C.section_cheapest(rs, 2, target=None))
    assert "TOP 2 CHEAPEST" in out
    # cheapest (London 16000) appears before Paris; Dubai excluded (cap 2)
    assert out.index("London") < out.index("Paris")
    assert "Dubai" not in out


def test_section_changes_by_magnitude():
    rs = [_rr("COK", "LHR", "London", "2026-10-02", 16000, prev=20000),  # -4000
          _rr("COK", "DXB", "Dubai", "2026-10-02", 17000, prev=17500),   # -500
          _rr("TRV", "CDG", "Paris", "2026-10-02", 25000)]               # new, no diff
    out = "\n".join(C.section_changes(rs, 5))
    assert out.index("London") < out.index("Dubai")  # bigger change first
    assert "Paris" not in out                         # no prior price


def test_section_trends_finds_cheaper_date():
    rs = [_rr("COK", "LHR", "London", "2026-10-02", 22000),
          _rr("COK", "LHR", "London", "2026-11-13", 16000),
          _rr("COK", "LHR", "London", "2026-12-25", 24000)]
    out = "\n".join(C.section_trends(rs))
    assert "CHEAPER ON OTHER DATES" in out
    assert "Save up to" in out
    # cheapest date (13 Nov) should be listed first in the alt list
    assert "13 Nov" in out


def test_chunk_lines_splits_long():
    lines = [f"line {i} " + "x" * 100 for i in range(200)]
    chunks = C.chunk_lines(lines, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


if __name__ == "__main__":
    import sys
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
