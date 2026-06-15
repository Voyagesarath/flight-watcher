#!/usr/bin/env python3
"""
Health check — runs at 9:15pm IST to verify the 9pm flight scanner completed
and sent the Telegram report. If it failed, logs a warning and exits with code 1.

Usage:
  ./health_check.py              → exit 0 if today's report was sent, 1 if not
  python3 health_check.py --alert   → also send a Telegram alert on failure
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
HEALTH_FILE = "health.json"


def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def check_health(alert: bool = False) -> bool:
    """Check if today's 9pm report was sent. Return True if healthy, False if failed."""
    today = now_ist().date().isoformat()

    if not Path(HEALTH_FILE).exists():
        print(f"❌ {HEALTH_FILE} not found — no runs have occurred yet.")
        return False

    with open(HEALTH_FILE) as f:
        health = json.load(f)

    # Look for "run_completed" or "telegram_sent" after 21:00 IST today
    found_success = False
    for timestamp_str, event_data in health.items():
        try:
            ts = datetime.fromisoformat(timestamp_str).astimezone(IST)
            if ts.date().isoformat() != today:
                continue
            if ts.hour >= 21:  # 9pm or later
                event = event_data.get("event", "")
                if event in ("run_completed", "telegram_sent"):
                    found_success = True
                    print(f"✅ {event} at {ts.strftime('%H:%M IST')}")
                    break
        except (ValueError, AttributeError):
            continue

    if found_success:
        print("✅ Health check passed — 9pm report was sent successfully.")
        return True

    # Failure — log details and optionally alert
    print(f"❌ Health check failed — no 9pm report sent today ({today}).")
    print(f"   Last events in {HEALTH_FILE}:")
    for timestamp_str, event_data in sorted(health.items())[-5:]:
        ts_short = timestamp_str[-8:-3] if len(timestamp_str) > 8 else timestamp_str
        event = event_data.get("event", "?")
        details = event_data.get("details", "")
        detail_str = f" ({details})" if details else ""
        print(f"   {ts_short}: {event}{detail_str}")

    if alert:
        try:
            send_alert(today)
        except Exception as e:
            print(f"   ⚠️  Alert send failed: {e}")

    return False


def send_alert(date_str: str):
    """Send a Telegram alert that the health check failed."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not (bot_token and chat_id):
        print("   (Skipping alert — TELEGRAM_BOT_TOKEN/CHAT_ID not set)")
        return

    msg = f"""⚠️ <b>Flight Watcher Health Alert</b>
{date_str} 21:15 IST

The 9pm flight scan did not send a report today.
Check /Users/sarathkrishnan/flight-watcher/logs/flight-watcher.log for details.

Possible causes:
• Process timed out (>10 min hang)
• Google rate-limited all shards
• Telegram API unreachable
• Host internet down"""

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if result.get("ok"):
            print("   ✅ Alert Telegram sent.")
        else:
            print(f"   ❌ Alert failed: {result}")


if __name__ == "__main__":
    should_alert = "--alert" in sys.argv
    ok = check_health(alert=should_alert)
    sys.exit(0 if ok else 1)
