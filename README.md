# ✈️ International Flight Price Tracker

Watches the cheapest international fares from your origins (e.g. **TRV** / **COK**) across a set
of departure dates, and sends you a **Telegram** report you can actually trust. Runs free on
GitHub Actions, 4× per day.

No server. No email password. No credit card.

## Why you can trust it

- **Prices are forced to INR.** Google Flights returns prices in the *server's* currency — on
  GitHub's US runners that's USD. The old code stored `$187` as `₹187`. This version forces
  `currency=INR`, so a fare is always a fare.
- **Bad data is rejected, never reported.** Zero prices, wrong-currency leaks, and values outside
  a plausible band (₹1,500–₹800,000) are dropped — silently wrong numbers don't reach you.
- **Real history, real trends.** Every run appends to a per-route price history (`state.json`),
  so "cheaper since last check" and "trending down" are measured, not guessed.
- **Silence never means "all good".** If a run retrieves no trustworthy prices at all, you still
  get a heads-up message.
- **Tested.** `python test_checker.py` covers the currency gate, state migration, trend math, and
  every report section. CI runs the tests before every scan.

---

## 📱 What you'll receive on Telegram

Three sections every run:

**1. 🏆 Top 10 cheapest** — the cheapest fares right now, with change vs last check and a value
badge (all-time low / below-usual / Google's low–high signal).

**2. 📉 Top 5 biggest changes since last check** — where the price moved most, old → new.

**3. 📅 Cheaper on other dates** — for each route, which departure date is cheapest, how much you'd
save vs the priciest date, plus a book-now / wait read from the trend and Google's signal.

```
✈️ Flight Watch
📍 TRV / COK  ·  14 Jun 2026, 08:30 IST
🔍 52 route-dates scanned · 50 priced

━━━━━━━━━━━━━━━━━━━━
🏆 TOP 10 CHEAPEST
━━━━━━━━━━━━━━━━━━━━
#1 ₹58,740 · London Heathrow (UK)
   TRV→LHR · Fri 09 Oct · 1 stop · 13 hr 50 min
   Gulf Air · 📉 ₹7,000 (-11%)
   🔥 all-time low
...

━━━━━━━━━━━━━━━━━━━━
📅 CHEAPER ON OTHER DATES
━━━━━━━━━━━━━━━━━━━━
✈️ London Heathrow (TRV→LHR)
   Cheapest: Fri 09 Oct ₹58,740 · Fri 16 Oct ₹58,740 · Fri 23 Oct ₹58,740
   💡 Save up to ₹23,010 (28%) vs Fri 30 Oct (₹81,750)
   🟢 great price — good time to book  ·  6 checks
```

Long reports are split across multiple Telegram messages — nothing is truncated.

---

## 🚀 Setup — one command

### Prerequisites
- [GitHub CLI](https://cli.github.com) installed
- Logged in: `gh auth login`

```bash
cd flight-watcher
chmod +x setup.sh
./setup.sh
```

The script creates a repo, pushes the files, walks you through making a Telegram bot, saves the
bot token + chat ID as encrypted GitHub Secrets, and triggers a first run.

---

## 🧪 Run it locally first (recommended)

You don't need Telegram to see the report — print it to your terminal:

```bash
pip install -r requirements.txt
python checker.py --local                 # full scan, printed to console
python checker.py --local --max-routes 6  # quick smoke test
python test_checker.py                    # offline unit tests
```

`--local` never sends Telegram and never needs the bot secrets.

---

## ⚙️ Configuration (`watchlist.yaml`)

```yaml
settings:
  origins: [TRV, COK]
  departure_dates: ["2026-10-02", "2026-10-09", ...]   # or use days_ahead: [14,30,60]
  price_threshold_inr: 999999   # soft target → ✅ badge; you always get the cheapest list
  top_results: 10               # how many cheapest fares to list
  seat: economy
  adults: 2

  # accuracy / reliability knobs
  max_stops: null               # drop itineraries with more stops (null = no limit)
  fetch_mode: fallback          # fast path, then browser fallback if Google blocks us
  retries: 3                    # attempts per route
  concurrency: 1                # parallel lookups (keep 1 on CI; raise for big dest lists)
  request_delay_sec: 1.0        # polite pause between sequential requests

excluded_countries: [TH, MY, SG, VN, KH, LA, KR]

destinations:
  - { code: LHR, city: "London Heathrow", country: "UK", country_iso: GB }
  - { code: CDG, city: "Paris CDG", country: "France", country_iso: FR }
```

Edit and push — the next run picks it up automatically.

---

## 📁 Files

```
flight-watcher/
├── setup.sh                          ← run once
├── checker.py                        ← scanner + report builder
├── test_checker.py                   ← offline tests (no network)
├── watchlist.yaml                    ← your config
├── requirements.txt
├── state.json                        ← auto-updated price history
└── .github/workflows/flight-check.yml
```

## 🕐 Schedule

4× daily at **8:00 AM, 2:00 PM, 8:00 PM, 2:00 AM IST**. Runs are serialized so two scans never
fight over `state.json`. GitHub Actions free tier is 2,000 min/month; this uses a small fraction.
