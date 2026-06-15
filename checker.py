#!/usr/bin/env python3
"""
Flight Price Tracker — Telegram edition.

Scans cheapest international one-way fares from your origins (e.g. TRV/COK)
to your destinations across a set of departure dates, then sends a Telegram
report with three sections:

  1. 🏆 Top 10 cheapest fares right now
  2. 📉 Top 5 biggest price changes since the last check
  3. 📅 Cheaper-on-other-dates trends (and Google's low/typical/high signal)

Design goals: *accuracy* and *trust*.
  • Prices are forced to INR so US-based CI runners can't silently return USD.
  • Implausible / zero / wrong-currency prices are rejected, never reported.
  • A full price history is kept per route so trends and changes are real,
    not guessed.
  • Anything that fails fails loudly (you still get a heads-up message), and
    you can run it locally with --local to see the exact report without
    touching Telegram.
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import os
import random
import re
import signal
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Hard cap on every socket operation — prevents a hung TLS handshake or stalled
# TCP connection from freezing a worker thread indefinitely.
socket.setdefaulttimeout(45)
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml

from fast_flights import FlightData, Passengers
from fast_flights.core import get_flights_from_filter
from fast_flights.filter import TFSData

# ── Constants ────────────────────────────────────────────────────────────────

CONFIG_FILE = "watchlist.yaml"
STATE_FILE = "state.json"
CACHE_DIR = "cache"
HEALTH_FILE = "health.json"
RUN_TIMEOUT_SEC = 600  # 10 minutes — any run taking longer is a hang

# Maps IST hour → shard index for the 4×/day schedule.
# 9am→0, 1pm→1, 5pm→2, 9pm→3(last). The 9pm run combines all 4 caches and
# sends the single daily Telegram report.
SCHED_HOURS_4 = {9: 0, 13: 1, 17: 2, 21: 3}
STATE_VERSION = 2
HISTORY_LIMIT = 60               # observations kept per route+date
IST = timezone(timedelta(hours=5, minutes=30))

CURRENCY = "INR"                 # forced — do NOT rely on server geo
CURRENCY_SYMBOL = "₹"

# A real international one-way economy fare from India lives in this band.
# Anything outside it is almost certainly a parse error or a wrong-currency
# leak (e.g. "$187" read as ₹187), so we refuse to trust it.
MIN_PLAUSIBLE_INR = 1500
MAX_PLAUSIBLE_INR = 800000

# Other currency symbols that, if seen, mean the INR override failed.
FOREIGN_SYMBOLS = ("$", "€", "£", "¥", "₩", "AED", "SAR", "QAR")


# ── Time helpers ─────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def today_iso() -> str:
    return now_ist().date().isoformat()


def active_origin(all_origins: list) -> str:
    """Alternate origin by day-of-year: index 0 on odd days, 1 on even, etc."""
    return all_origins[now_ist().timetuple().tm_yday % len(all_origins)]


def record_health(event: str, details: str = ""):
    """Log health/status event for monitoring (timestamp, shard, success/fail)."""
    try:
        data = {}
        if os.path.exists(HEALTH_FILE):
            with open(HEALTH_FILE) as f:
                data = json.load(f)
        data[now_ist().isoformat()] = {"event": event, "details": details}
        with open(HEALTH_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️  Health logging failed: {e}")


def timeout_handler(signum, frame):
    """Raise TimeoutError when RUN_TIMEOUT_SEC is exceeded."""
    raise TimeoutError(f"Run exceeded {RUN_TIMEOUT_SEC}s timeout — likely a hang. Killing process.")


def set_run_timeout():
    """Set OS-level timeout for the entire process."""
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(RUN_TIMEOUT_SEC)


# ── Shard cache (accumulate results across the 4 daily runs) ─────────────────

def save_shard_cache(shard_idx: int, origin: str, results: list, scanned: int, priced: int):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"shard_{shard_idx}.json")
    with open(path, "w") as f:
        json.dump({
            "watch_day": watch_day_iso(),
            "origin": origin,
            "scanned": scanned,
            "priced": priced,
            "results": [asdict(r) for r in results],
        }, f)


def load_all_shard_caches(n_shards: int) -> tuple:
    """Returns (all_results, total_scanned, total_priced, origin)."""
    today = watch_day_iso()
    all_results, total_scanned, total_priced, origin = [], 0, 0, ""
    for i in range(n_shards):
        path = os.path.join(CACHE_DIR, f"shard_{i}.json")
        if not os.path.exists(path):
            print(f"  ⚠️  Shard {i} cache missing — that run may have failed")
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("watch_day") != today:
                print(f"  ⚠️  Shard {i} cache is from {data.get('watch_day')}, not today ({today}) — skipping")
                continue
            all_results.extend(RouteResult(**r) for r in data["results"])
            total_scanned += data.get("scanned", 0)
            total_priced += data.get("priced", 0)
            origin = data.get("origin", origin)
        except Exception as e:
            print(f"  ⚠️  Could not load shard {i} cache: {e}")
    return all_results, total_scanned, total_priced, origin


def clear_shard_caches(n_shards: int):
    for i in range(n_shards):
        try:
            os.remove(os.path.join(CACHE_DIR, f"shard_{i}.json"))
        except FileNotFoundError:
            pass


def watch_day_iso() -> str:
    """Logical watch-day. If a run ever happens before 6am it belongs to the previous day."""
    now = now_ist()
    if now.hour < 6:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def shard_slice(items: list, n_shards: int, override_idx: int = -1) -> tuple:
    """Return (this_shard_items, shard_idx, n_shards).
    For n_shards==4: uses IST hour via SCHED_HOURS_4 (true time-based sharding).
    For other values: falls back to day-of-year modulo.
    """
    if n_shards <= 1:
        return items, 0, 1
    if override_idx >= 0:
        idx = override_idx
    elif n_shards == 4:
        hour = now_ist().hour
        if hour not in SCHED_HOURS_4:
            # Not at a scheduled slot — bypass sharding so a manual run always
            # produces a usable report instead of silently writing the wrong cache.
            print(f"  ℹ️  Hour {hour:02d}:xx is not a scheduled shard slot {sorted(SCHED_HOURS_4)}.")
            print(f"     Running standalone (all {len(items)} destinations). Use --shard 0-3 to target a shard.")
            return items, 0, 1
        idx = SCHED_HOURS_4[hour]
    else:
        idx = now_ist().timetuple().tm_yday % n_shards
    return items[idx::n_shards], idx, n_shards


def fmt_date(iso: str) -> str:
    """'2026-11-13' -> 'Fri 13 Nov'. Falls back to the raw string."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %d %b")
    except ValueError:
        return iso


def rupees(n: Optional[int]) -> str:
    return f"{CURRENCY_SYMBOL}{n:,}" if n is not None else "—"


# ── Config ───────────────────────────────────────────────────────────────────

def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (real env wins).

    Lets you run locally without exporting secrets into your shell history.
    No dependency — we parse it ourselves.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_config(path: str = CONFIG_FILE) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_dates(settings: dict) -> list[str]:
    """Fixed `departure_dates` (past dates dropped) or relative `days_ahead`."""
    today = today_iso()
    if settings.get("departure_dates"):
        dates = [str(d) for d in settings["departure_dates"] if str(d) >= today]
        return sorted(set(dates))
    base = now_ist()
    offsets = settings.get("days_ahead", [14, 30, 60])
    return [(base + timedelta(days=int(d))).strftime("%Y-%m-%d") for d in offsets]


# ── Price parsing (accuracy gate) ────────────────────────────────────────────

def parse_price(raw) -> Optional[int]:
    """
    Parse a price into INR. Returns None when the value can't be trusted:
    missing, zero, a non-INR currency, or wildly outside the plausible band.

    The fast_flights library already strips thousands separators, so we mostly
    guard against zero / wrong-currency / garbage.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
    else:
        s = str(raw)
        # If a foreign currency symbol is present, the INR override failed —
        # do not silently treat the number as rupees.
        if CURRENCY_SYMBOL not in s and any(sym in s for sym in FOREIGN_SYMBOLS):
            return None
        digits = re.sub(r"[^\d]", "", s)
        if not digits:
            return None
        n = int(digits)
    if n < MIN_PLAUSIBLE_INR or n > MAX_PLAUSIBLE_INR:
        return None
    return n


# ── Flight search ────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    price: int
    airline: str
    departure: str
    arrival: str
    duration: str
    stops: Optional[int]
    signal: str          # Google's "low" | "typical" | "high" (may be "")
    is_best: bool


def _normalise_stops(stops) -> Optional[int]:
    if isinstance(stops, int):
        return stops
    return None


def search_route(
    origin: str,
    dest_code: str,
    date: str,
    adults: int,
    seat: str,
    max_stops: Optional[int],
    fetch_mode: str,
    retries: int,
) -> Optional[SearchResult]:
    """
    Return the cheapest *trustworthy* fare for one route+date, or None.

    Prices are forced to INR via get_flights_from_filter(currency="INR"); the
    public get_flights() helper can't set currency, which is the root cause of
    USD-on-CI bugs.
    """
    tfs = TFSData.from_interface(
        flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest_code)],
        trip="one-way",
        seat=seat,
        passengers=Passengers(adults=adults),
        max_stops=max_stops,
    )

    last_err = ""
    for attempt in range(max(1, retries)):
        try:
            result = get_flights_from_filter(tfs, currency=CURRENCY, mode=fetch_mode)
            if not result or not result.flights:
                raise ValueError("no flights returned")

            priced = []
            for f in result.flights:
                p = parse_price(f.price)
                if p is not None:
                    priced.append((p, f))
            if not priced:
                raise ValueError("no trustworthy INR prices in result")

            best_price, best = min(priced, key=lambda pf: pf[0])
            return SearchResult(
                price=best_price,
                airline=best.name or "—",
                departure=best.departure or "",
                arrival=best.arrival or "",
                duration=best.duration or "",
                stops=_normalise_stops(best.stops),
                signal=(result.current_price or "").strip().lower(),
                is_best=bool(best.is_best),
            )
        except Exception as e:  # noqa: BLE001 — one bad route must not abort the run
            last_err = str(e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.7))

    # Turnstile / 401 blocks are expected noise; surface everything else.
    if last_err and "401" not in last_err and "turnstile" not in last_err.lower():
        print(f"    ⚠️  {origin}→{dest_code} {date}: {last_err[:160]}")
    return None


# ── State (price history) ────────────────────────────────────────────────────

def route_key(origin: str, dest_code: str, date: str) -> str:
    return f"{origin}-{dest_code}-{date}"


def load_state(path: str = STATE_FILE) -> dict:
    if not os.path.exists(path):
        return {"version": STATE_VERSION, "routes": {}}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "routes": {}}
    return migrate_state(raw)


def migrate_state(raw: dict) -> dict:
    """Upgrade the old flat {key: price} format to v2 history."""
    if isinstance(raw, dict) and raw.get("version") == STATE_VERSION and "routes" in raw:
        return raw
    routes: dict = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, int):
                routes[key] = {"history": [{"t": "", "p": val, "sig": "", "stops": None}]}
    return {"version": STATE_VERSION, "routes": routes}


def get_history(state: dict, key: str) -> list[dict]:
    entry = state["routes"].get(key)
    return entry["history"] if entry else []


def append_observation(state: dict, key: str, price: int, signal: str, stops):
    entry = state["routes"].setdefault(key, {"history": []})
    entry["history"].append({
        "t": now_ist().strftime("%Y-%m-%dT%H:%M%z"),
        "p": price,
        "sig": signal,
        "stops": stops,
    })
    entry["history"] = entry["history"][-HISTORY_LIMIT:]


def save_state(state: dict, path: str = STATE_FILE):
    """Atomic write so a crash mid-write can't corrupt the history."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ── Per-route analysis ───────────────────────────────────────────────────────

@dataclass
class RouteResult:
    origin: str
    dest_code: str
    dest_city: str
    dest_country: str
    date: str
    price: int
    airline: str
    departure: str
    arrival: str
    duration: str
    stops: Optional[int]
    signal: str
    prev_price: Optional[int]      # last observation before this run
    all_time_low: int
    typical: int                   # median of history
    n_checks: int
    trend: str                     # "down" | "up" | "flat" | "new"

    @property
    def key(self) -> str:
        return route_key(self.origin, self.dest_code, self.date)

    @property
    def diff(self) -> Optional[int]:
        if self.prev_price is None:
            return None
        return self.price - self.prev_price

    def change_label(self) -> str:
        d = self.diff
        if d is None:
            return "🆕 first look"
        if d == 0:
            return "≈ no change"
        pct = round(100 * d / self.prev_price) if self.prev_price else 0
        if d < 0:
            return f"📉 {rupees(abs(d))} ({pct}%)"
        return f"📈 +{rupees(d)} (+{pct}%)"

    def value_label(self) -> str:
        """How good is this price vs its own history + Google's signal."""
        tags = []
        # History-based tags need ≥2 checks; on a first look everything would
        # trivially be its own "all-time low", which is misleading.
        if self.n_checks >= 2:
            if self.price <= self.all_time_low:
                tags.append("🔥 all-time low")
            elif self.typical and self.price < self.typical:
                save = round(100 * (self.typical - self.price) / self.typical)
                if save >= 5:
                    tags.append(f"👍 {save}% below usual")
        # Google's signal is independent of our history, so always trustworthy.
        if self.signal == "low":
            tags.append("🟢 Google: low")
        elif self.signal == "high":
            tags.append("🔴 Google: high")
        return " · ".join(tags)

    def stops_label(self) -> str:
        if self.stops is None:
            return "?"
        if self.stops == 0:
            return "Direct"
        return f"{self.stops} stop" + ("s" if self.stops > 1 else "")


def analyse(rr_origin, dest, date, res: SearchResult, history: list[dict]) -> RouteResult:
    """Combine a fresh search result with stored history into a RouteResult."""
    prev_price = history[-1]["p"] if history else None
    prices = [h["p"] for h in history] + [res.price]
    all_time_low = min(prices)
    typical = int(statistics.median(prices))
    n_checks = len(prices)

    trend = "new"
    if len(prices) >= 3:
        baseline = statistics.mean(prices[-5:-1])  # recent prior checks
        delta = res.price - baseline
        thresh = max(500, 0.03 * baseline)
        trend = "down" if delta < -thresh else "up" if delta > thresh else "flat"
    elif prev_price is not None:
        trend = "down" if res.price < prev_price else "up" if res.price > prev_price else "flat"

    return RouteResult(
        origin=rr_origin,
        dest_code=dest["code"],
        dest_city=dest["city"],
        dest_country=dest["country"],
        date=date,
        price=res.price,
        airline=res.airline,
        departure=res.departure,
        arrival=res.arrival,
        duration=res.duration,
        stops=res.stops,
        signal=res.signal,
        prev_price=prev_price,
        all_time_low=all_time_low,
        typical=typical,
        n_checks=n_checks,
        trend=trend,
    )


# ── Report sections ──────────────────────────────────────────────────────────

SEP = "━━━━━━━━━━━━━━━━━━━━"


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def section_cheapest(results: list[RouteResult], n: int, target: Optional[int]) -> list[str]:
    top = sorted(results, key=lambda r: r.price)[:n]
    lines = [SEP, f"🏆 <b>TOP {len(top)} CHEAPEST</b>", SEP]
    for i, r in enumerate(top, 1):
        under = " ✅ under target" if (target and r.price <= target) else ""
        head = f"<b>#{i} {rupees(r.price)}</b> · {esc(r.dest_city)} ({esc(r.dest_country)}){under}"
        meta = (f"   {r.origin}→{r.dest_code} · {fmt_date(r.date)} · "
                f"{r.stops_label()} · {esc(r.duration) or '—'}")
        tail = f"   {esc(r.airline)} · {r.change_label()}"
        val = r.value_label()
        if val:
            tail += f"\n   {val}"
        lines += [head, meta, tail]
    return lines


def section_changes(results: list[RouteResult], n: int) -> list[str]:
    changed = [r for r in results if r.diff not in (None, 0)]
    changed.sort(key=lambda r: abs(r.diff), reverse=True)
    top = changed[:n]
    lines = [SEP, f"📉 <b>TOP {len(top)} CHANGES vs LAST CHECK</b>", SEP]
    if not top:
        lines.append("No prior prices to compare yet — baseline saved.")
        return lines
    for i, r in enumerate(top, 1):
        arrow = "↓" if r.diff < 0 else "↑"
        pct = round(100 * r.diff / r.prev_price) if r.prev_price else 0
        lines.append(
            f"{i}. {esc(r.dest_city)} {r.origin}→{r.dest_code} {fmt_date(r.date)}\n"
            f"   {rupees(r.prev_price)} → <b>{rupees(r.price)}</b>  "
            f"{arrow}{rupees(abs(r.diff))} ({pct:+d}%)"
        )
    return lines


def _trend_phrase(r: RouteResult) -> str:
    has_history = r.n_checks >= 2
    if r.signal == "low" or (has_history and r.price <= r.all_time_low):
        return "🟢 great price — good time to book"
    if has_history and r.trend == "down":
        return "↓ trending down — may keep falling, watch it"
    if r.signal == "high" or (has_history and r.trend == "up"):
        return "↑ trending up — book sooner rather than later"
    if not has_history:
        return "first look — baseline saved, trend builds over next runs"
    return "→ steady"


def section_trends(results: list[RouteResult], max_alt_dates: int = 3) -> list[str]:
    """For each origin→dest, show the cheapest date(s) and the spread vs the worst."""
    lines = [SEP, "📅 <b>CHEAPER ON OTHER DATES</b>", SEP]
    groups: dict[tuple, list[RouteResult]] = {}
    for r in results:
        groups.setdefault((r.origin, r.dest_code), []).append(r)

    any_insight = False
    for (origin, code), rs in sorted(groups.items()):
        if len(rs) < 2:
            continue
        rs_sorted = sorted(rs, key=lambda r: r.price)
        cheapest = rs_sorted[0]
        priciest = rs_sorted[-1]
        spread = priciest.price - cheapest.price
        any_insight = True

        lines.append(f"✈️ <b>{esc(cheapest.dest_city)}</b> ({origin}→{code})")
        alts = " · ".join(
            f"{fmt_date(r.date)} {rupees(r.price)}" for r in rs_sorted[:max_alt_dates]
        )
        lines.append(f"   Cheapest: {alts}")
        if spread > 0:
            pct = round(100 * spread / priciest.price)
            lines.append(
                f"   💡 Save up to {rupees(spread)} ({pct}%) vs "
                f"{fmt_date(priciest.date)} ({rupees(priciest.price)})"
            )
        checks = cheapest.n_checks
        lines.append(f"   {_trend_phrase(cheapest)}  ·  {checks} check" + ("s" if checks != 1 else ""))
    if not any_insight:
        lines.append("Need ≥2 dates per route to compare — add more departure_dates.")
    return lines


def section_all_destinations(results: list[RouteResult]) -> list[str]:
    """Full leaderboard: cheapest date per destination, sorted by price."""
    lines = [SEP, "📊 <b>ALL DESTINATIONS — CHEAPEST DATE</b>", SEP]
    groups: dict[tuple, list[RouteResult]] = {}
    for r in results:
        groups.setdefault((r.origin, r.dest_code), []).append(r)

    bests = [min(rs, key=lambda r: r.price) for rs in groups.values()]
    bests.sort(key=lambda r: r.price)

    for i, r in enumerate(bests, 1):
        val = r.value_label()
        val_str = f" · {val}" if val else ""
        lines.append(
            f"<b>#{i} {rupees(r.price)}</b> · {esc(r.dest_city)} ({esc(r.dest_country)})\n"
            f"   {r.origin}→{r.dest_code} · {fmt_date(r.date)} · {r.stops_label()} · {esc(r.airline)}{val_str}"
        )
    return lines


def build_report(
    results: list[RouteResult],
    scanned: int,
    priced: int,
    origins: list[str],
    top_n: int,
    target: Optional[int],
    shard_idx: int = 0,
    n_shards: int = 1,
    n_dests: int = 0,
    total_dests: int = 0,
    daily_summary: bool = False,
) -> list[str]:
    when = now_ist().strftime("%d %b %Y, %H:%M IST")
    header = [
        "✈️ <b>Flight Watch</b>",
        f"📍 {' / '.join(origins)}  ·  {when}",
        f"🔍 {scanned} route-dates · {priced} priced",
    ]
    if daily_summary:
        header.append(f"🗂 Daily · {total_dests} destinations · {n_shards} shards")
    elif n_shards > 1:
        header.append(f"🗂 Shard {shard_idx+1}/{n_shards} · {n_dests} of {total_dests} destinations")
    lines = header + [""]
    lines += section_cheapest(results, top_n, target) + [""]
    lines += section_changes(results, 5) + [""]
    lines += section_trends(results) + [""]
    lines.append("<i>Prices in INR · indicative · verify on the airline site before booking.</i>")
    return lines


# ── Telegram ─────────────────────────────────────────────────────────────────

def chunk_lines(lines: list[str], limit: int = 3800) -> list[str]:
    """Pack whole lines into <=limit-char messages (no mid-tag splits)."""
    chunks, buf, size = [], [], 0
    for line in lines:
        add = len(line) + 1
        if size + add > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def send_telegram(bot_token: str, chat_id: str, message: str) -> str:
    """Send Telegram message. Returns message_id on success, raises on failure."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                raise RuntimeError(f"Telegram error: {result}")
            msg_id = result.get("result", {}).get("message_id", "?")
            record_health("telegram_sent", f"msg_id={msg_id}")
            return str(msg_id)
    except urllib.error.HTTPError as e:
        record_health("telegram_failed", f"HTTP {e.code}")
        raise RuntimeError(f"Telegram HTTP {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        record_health("telegram_error", str(e)[:100])
        raise


def deliver(lines: list[str], bot_token: Optional[str], chat_id: Optional[str], local: bool):
    text = "\n".join(lines)
    if local or not (bot_token and chat_id):
        if not local:
            print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead.\n")
            record_health("no_credentials", "token/chat_id missing")
        # Strip simple HTML tags for console readability.
        plain = re.sub(r"</?(b|i)>", "", text)
        print(plain)
        return
    try:
        for part in chunk_lines(lines):
            send_telegram(bot_token, chat_id, part)
    except Exception as e:
        record_health("delivery_failed", str(e)[:100])
        raise


# ── Main ─────────────────────────────────────────────────────────────────────

def run_scan(config: dict, args) -> tuple[list[RouteResult], dict, int, int]:
    settings = config["settings"]
    excluded = set(config.get("excluded_countries", []))
    all_dests = [d for d in config["destinations"] if d.get("country_iso") not in excluded]
    n_shards = int(settings.get("shards", 1))
    dests, shard_idx, n_shards = shard_slice(all_dests, n_shards, args.shard)
    all_origins = settings["origins"]
    origins = [active_origin(all_origins)] if n_shards > 1 else all_origins
    dates = build_dates(settings)
    seat = settings.get("seat", "economy")
    adults = int(settings.get("adults", 1))
    max_stops = settings.get("max_stops")
    fetch_mode = settings.get("fetch_mode", "fallback")
    retries = int(settings.get("retries", 3))
    concurrency = max(1, int(settings.get("concurrency", 1)))
    delay = float(settings.get("request_delay_sec", 1.0))

    combos = list(itertools.product(origins, dests, dates))
    if args.max_routes:
        combos = combos[: args.max_routes]
    scanned = len(combos)

    dest_label = (f"{len(dests)}/{len(all_dests)} (shard {shard_idx+1}/{n_shards})"
                  if n_shards > 1 else
                  f"{len(dests)} (excluding {', '.join(sorted(excluded)) or 'none'})")
    print(f"\n🔍 Scanning {scanned} route-dates")
    print(f"   Origins      : {', '.join(origins)}")
    print(f"   Destinations : {dest_label}")
    print(f"   Dates        : {len(dates)} ({dates[0]} … {dates[-1]})" if dates else "   Dates: none")
    print(f"   Currency     : {CURRENCY} (forced) · fetch_mode={fetch_mode} · concurrency={concurrency}\n")

    state = load_state()
    results: list[RouteResult] = []

    def work(origin, dest, date) -> Optional[RouteResult]:
        res = search_route(origin, dest["code"], date, adults, seat, max_stops, fetch_mode, retries)
        if res is None:
            return None
        key = route_key(origin, dest["code"], date)
        history = list(get_history(state, key))  # snapshot before append
        rr = analyse(origin, dest, date, res, history)
        return rr

    if concurrency == 1:
        for origin, dest, date in combos:
            label = f"{origin}→{dest['code']} {date}"
            print(f"  {label} …", end="", flush=True)
            rr = work(origin, dest, date)
            if rr is None:
                print(" no result")
            else:
                print(f" {rupees(rr.price)}" + (f" (was {rupees(rr.prev_price)})" if rr.prev_price else " (new)"))
                results.append(rr)
                append_observation(state, rr.key, rr.price, rr.signal, rr.stops)
            if delay:
                time.sleep(delay)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # Pace *submissions* by `delay` so the request launch rate stays at
            # ~1/delay regardless of worker count — concurrency bounds how many
            # are in flight, but we never fire a burst that trips Google's limit.
            futs = {}
            for o, d, dt in combos:
                futs[pool.submit(work, o, d, dt)] = (o, d, dt)
                if delay:
                    time.sleep(delay)
            for fut in as_completed(futs):
                o, d, dt = futs[fut]
                rr = fut.result()
                label = f"{o}→{d['code']} {dt}"
                if rr is None:
                    print(f"  {label} … no result")
                else:
                    print(f"  {label} … {rupees(rr.price)}")
                    results.append(rr)
                    append_observation(state, rr.key, rr.price, rr.signal, rr.stops)

    return results, state, scanned, len(results), shard_idx, n_shards, len(dests), len(all_dests), origins


def main(argv=None):
    parser = argparse.ArgumentParser(description="Flight price tracker → Telegram")
    parser.add_argument("--local", action="store_true",
                        help="Print the report to the console instead of sending Telegram.")
    parser.add_argument("--max-routes", type=int, default=0,
                        help="Limit number of route-dates scanned (debug).")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not write state.json (debug).")
    parser.add_argument("--shard", type=int, default=-1,
                        help="Override auto-detected shard index (0-based). "
                             "Total shards come from watchlist.yaml settings.shards.")
    args = parser.parse_args(argv)

    load_dotenv()  # pick up TELEGRAM_* from a local .env if present
    config = load_config()
    settings = config["settings"]
    top_n = int(settings.get("top_results", 10))
    target = settings.get("price_threshold_inr")
    if isinstance(target, int) and target >= MAX_PLAUSIBLE_INR:
        target = None  # "show everything" sentinel — treat as no target

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    results, state, scanned, priced, shard_idx, n_shards, n_dests, total_dests, origins = run_scan(config, args)

    if not args.no_save:
        save_state(state)

    print(f"\n✅ {priced}/{scanned} route-dates priced.")

    # ── Shard accumulation ────────────────────────────────────────────────────
    # Every run saves its shard to disk. Intermediate shards exit after caching
    # (silent in production, brief summary in --local mode). The last shard
    # (9pm) combines all 4 caches and delivers the two daily Telegram messages.
    if n_shards > 1:
        origin = origins[0] if origins else ""
        save_shard_cache(shard_idx, origin, results, scanned, priced)

        if shard_idx < n_shards - 1:
            if args.local:
                print(f"🗂 Shard {shard_idx+1}/{n_shards} cached · {priced}/{scanned} priced")
                if results:
                    c = min(results, key=lambda r: r.price)
                    print(f"   Cheapest this shard: {c.dest_city} {rupees(c.price)} ({c.origin}→{c.dest_code})")
            else:
                print(f"🗂 Shard {shard_idx+1}/{n_shards} cached — no Telegram until shard {n_shards}/{n_shards} (9 pm)")
            return

        # Last shard — combine all and deliver
        print("🗂 Last shard — combining all shards for daily report …")
        all_results, total_scanned, total_priced, combined_origin = load_all_shard_caches(n_shards)
        clear_shard_caches(n_shards)
        report_results  = all_results or results
        report_scanned  = total_scanned or scanned
        report_priced   = total_priced or priced
        report_origins  = [combined_origin] if combined_origin else origins
        daily = True
    else:
        report_results  = results
        report_scanned  = scanned
        report_priced   = priced
        report_origins  = origins
        daily = False

    # ── Deliver ───────────────────────────────────────────────────────────────
    if not report_results:
        warn = [
            "✈️ <b>Flight Watch</b>",
            f"⚠️ {now_ist().strftime('%d %b %Y, %H:%M IST')}",
            f"No prices retrieved across {report_scanned} route-dates today.",
            "Google may be rate-limiting — will retry tomorrow.",
        ]
        deliver(warn, bot_token, chat_id, args.local)
        return

    # Message 1: top 10 + changes + trends
    msg1 = build_report(report_results, report_scanned, report_priced, report_origins,
                        top_n, target,
                        shard_idx, n_shards, n_dests, total_dests,
                        daily_summary=daily)
    deliver(msg1, bot_token, chat_id, args.local)

    # Message 2: full destination table (all 75 sorted by cheapest price)
    msg2 = section_all_destinations(report_results)
    deliver(msg2, bot_token, chat_id, args.local)

    cheapest = min(report_results, key=lambda r: r.price)
    print(f"📱 Report delivered. Cheapest: {cheapest.dest_city} {rupees(cheapest.price)} "
          f"({cheapest.origin}→{cheapest.dest_code} {cheapest.date})")


if __name__ == "__main__":
    try:
        set_run_timeout()
        main()
        record_health("run_completed", "success")
    except TimeoutError as e:
        print(f"❌ {e}")
        record_health("run_timeout", f"exceeded {RUN_TIMEOUT_SEC}s")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Run failed: {e}")
        record_health("run_failed", str(e)[:100])
        sys.exit(1)
