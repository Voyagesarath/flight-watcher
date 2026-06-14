# ✈️ International Flight Price Tracker

Watches cheapest international fares from **TRV** and **COK** across 50+ destinations. Runs free on GitHub Actions 4× per day. Notifies you on **Telegram** when deals are found.

No server. No email password. No credit card.

---

## 🚀 Setup — one command

### Prerequisites
- [GitHub CLI](https://cli.github.com) installed
- Logged in: `gh auth login`

### Run the setup script

```bash
# Clone or download this folder, then:
cd flight-watcher
chmod +x setup.sh
./setup.sh
```

The script will:
1. Create a public GitHub repo
2. Push all files
3. Walk you through creating a Telegram bot (takes 2 minutes)
4. Save your bot token and chat ID as encrypted GitHub Secrets
5. Trigger a test run immediately

---

## 📱 What you'll receive on Telegram

```
✈️ Cheapest International Flights
📍 From TRV / COK  •  14 Jun 2026 08:30 IST
🔍 500 routes scanned

#1 Dubai, UAE
   💰 ₹8,200  📉 ₹300 cheaper
   🛫 TRV → DXB  •  2026-06-28
   ✈️  Air India Express  •  Direct  •  3h 35m
   🕐 06:15 → 08:20
─────────────────
#2 Colombo, Sri Lanka
   💰 ₹9,100  🆕
   ...
```

---

## ⚙️ Configuration (`watchlist.yaml`)

```yaml
settings:
  price_threshold_inr: 25000  # alert only below this price
  top_results: 10             # deals per message
  days_ahead: [14,21,30,45,60]
  adults: 2

excluded_countries:
  - TH  # Thailand
  - MY  # Malaysia
  - SG  # Singapore
  - VN  # Vietnam
  - KH  # Cambodia
  - LA  # Laos
  - KR  # South Korea
```

Edit and push — next run picks it up automatically.

---

## 📁 Files

```
flight-watcher/
├── setup.sh                         ← run this once
├── checker.py                       ← main script
├── watchlist.yaml                   ← your config
├── requirements.txt
├── state.json                       ← auto-updated price history
└── .github/workflows/flight-check.yml
```

## 🕐 Schedule

4× daily at **8:00 AM, 2:00 PM, 8:00 PM, 2:00 AM IST** (GitHub Actions free tier: 2,000 min/month, this uses ~480).
