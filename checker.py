#!/usr/bin/env python3
"""
Flight Price Tracker — Telegram edition
Scans international fares from TRV/COK, sends cheapest deals via Telegram bot.
"""

import os
import json
import itertools
import urllib.request
import urllib.parse
import yaml
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

from fast_flights import FlightData, Passengers, get_flights

STATE_FILE = "state.json"


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Deal ───────────────────────────────────────────────────────────────────────

@dataclass
class Deal:
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
    stops: int
    prev_price: Optional[int] = None

    @property
    def state_key(self):
        return f"{self.origin}-{self.dest_code}-{self.date}"

    @property
    def change_tag(self):
        if self.prev_price is None:
            return "🆕"
        diff = self.price - self.prev_price
        if diff < -500:   return f"📉 ₹{abs(diff):,} cheaper"
        if diff >  500:   return f"📈 ₹{abs(diff):,} pricier"
        return "≈ same"


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open("watchlist.yaml") as f:
        return yaml.safe_load(f)

def build_dates(days_ahead: list) -> list:
    today = datetime.utcnow()
    return [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in days_ahead]


# ── Flight search ──────────────────────────────────────────────────────────────

def search_route(origin, dest_code, date, adults, seat) -> Optional[dict]:
    try:
        result = get_flights(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest_code)],
            trip="one-way",
            seat=seat,
            passengers=Passengers(adults=adults),
            fetch_mode="fallback",
        )
        if not result or not result.flights:
            return None
        valid = [f for f in result.flights if f.price]
        if not valid:
            return None

        def parse_price(p) -> int:
            """fast-flights returns price as int or string like '₹8,200' or '8200'."""
            if isinstance(p, int):
                return p
            # strip currency symbols, commas, spaces
            cleaned = str(p).replace("₹", "").replace(",", "").replace(" ", "").strip()
            # take only leading digits (e.g. "8200 per person" → "8200")
            import re
            m = re.search(r'\d+', cleaned)
            return int(m.group()) if m else 0

        best = min(valid, key=lambda f: parse_price(f.price))
        return {
            "price":     parse_price(best.price),
            "airline":   best.name,
            "departure": best.departure,
            "arrival":   best.arrival,
            "duration":  best.duration,
            "stops":     best.stops,
        }
    except Exception as e:
        print(f"    ⚠️  {origin}→{dest_code} {date}: {e}")
        return None


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(bot_token: str, chat_id: str, message: str):
    """Send a message via Telegram Bot API (no third-party library needed)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")

def build_telegram_message(deals: list, run_time: str, total_searched: int) -> str:
    lines = [
        f"✈️ <b>Cheapest International Flights</b>",
        f"📍 From TRV / COK  •  {run_time} IST",
        f"🔍 {total_searched} routes scanned\n",
    ]
    for i, d in enumerate(deals, 1):
        stops = "Direct" if d.stops == 0 else f"{d.stops} stop"
        lines.append(
            f"<b>#{i} {d.dest_city}, {d.dest_country}</b>\n"
            f"   💰 <b>₹{d.price:,}</b>  {d.change_tag}\n"
            f"   🛫 {d.origin} → {d.dest_code}  •  {d.date}\n"
            f"   ✈️  {d.airline}  •  {stops}  •  {d.duration}\n"
            f"   🕐 {d.departure} → {d.arrival}"
        )
        if i < len(deals):
            lines.append("─────────────────")

    lines.append("\n<i>Prices are indicative. Verify on airline site before booking.</i>")
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    config      = load_config()
    settings    = config["settings"]
    excluded    = set(config.get("excluded_countries", []))
    dests       = [d for d in config["destinations"] if d["country_iso"] not in excluded]
    origins     = settings["origins"]
    dates       = build_dates(settings["days_ahead"])
    threshold   = settings["price_threshold_inr"]
    top_n       = settings["top_results"]
    seat        = settings["seat"]
    adults      = settings["adults"]

    bot_token   = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id     = os.environ["TELEGRAM_CHAT_ID"]

    state       = load_state()
    deals: list[Deal] = []

    total = len(origins) * len(dests) * len(dates)
    print(f"\n🔍 Scanning {total} route×date combos")
    print(f"   Origins      : {', '.join(origins)}")
    print(f"   Dates        : {', '.join(dates)}")
    print(f"   Destinations : {len(dests)} (excluding {', '.join(excluded)})\n")

    for origin, dest, date in itertools.product(origins, dests, dates):
        code  = dest["code"]
        label = f"{origin}→{code} {date}"
        print(f"  Checking {label} ...", end="", flush=True)

        res = search_route(origin, code, date, adults, seat)
        if res is None:
            print(" no result")
            continue

        price = res["price"]
        key   = f"{origin}-{code}-{date}"
        prev  = state.get(key)
        state[key] = price

        print(f" ₹{price:,}" + (f" (was ₹{prev:,})" if prev else " (new)"))

        if price <= threshold:
            deals.append(Deal(
                origin       = origin,
                dest_code    = code,
                dest_city    = dest["city"],
                dest_country = dest["country"],
                date         = date,
                price        = price,
                airline      = res["airline"],
                departure    = res["departure"],
                arrival      = res["arrival"],
                duration     = res["duration"],
                stops        = res["stops"],
                prev_price   = prev,
            ))

    save_state(state)
    print(f"\n✅ {len(deals)} deals under ₹{threshold:,}")

    if not deals:
        print("😶 Nothing under threshold — no Telegram message sent.")
        return

    deals.sort(key=lambda d: d.price)
    top = deals[:top_n]

    run_time = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d %b %Y %H:%M")
    msg = build_telegram_message(top, run_time, total)

    send_telegram(bot_token, chat_id, msg)
    print(f"📱 Telegram message sent! Top deal: {top[0].dest_city} ₹{top[0].price:,}")


if __name__ == "__main__":
    main()
