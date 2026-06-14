#!/usr/bin/env bash
# ============================================================
#  Flight Watcher — One-shot GitHub setup via gh CLI
#  Run this once from inside the flight-watcher folder.
#  Prerequisites: git, gh (GitHub CLI), logged in via `gh auth login`
# ============================================================

set -euo pipefail

REPO_NAME="flight-watcher"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✈️  Flight Watcher — GitHub Setup                  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 0. Check prerequisites ────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
  echo "❌  GitHub CLI (gh) not found."
  echo "    Install: https://cli.github.com"
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "❌  Not logged in to GitHub CLI."
  echo "    Run: gh auth login"
  exit 1
fi

GH_USER=$(gh api user --jq '.login')
echo "✅  Logged in as: $GH_USER"
echo ""

# ── 1. Create GitHub repo ─────────────────────────────────────────────────────
echo "📦  Creating GitHub repo: $GH_USER/$REPO_NAME ..."

if gh repo view "$GH_USER/$REPO_NAME" &>/dev/null; then
  echo "    Repo already exists — skipping creation."
else
  gh repo create "$REPO_NAME" \
    --public \
    --description "✈️ Free international flight price tracker from TRV/COK via GitHub Actions + Telegram" \
    --confirm 2>/dev/null || \
  gh repo create "$REPO_NAME" \
    --public \
    --description "✈️ Free international flight price tracker from TRV/COK via GitHub Actions + Telegram"
  echo "    ✅ Repo created: https://github.com/$GH_USER/$REPO_NAME"
fi

echo ""

# ── 2. Initialise git and push files ─────────────────────────────────────────
echo "📤  Pushing files to GitHub ..."

git init -b main
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$GH_USER/$REPO_NAME.git"
git add .
git commit -m "feat: initial flight watcher setup" 2>/dev/null || \
  echo "    (nothing new to commit)"
git push -u origin main --force
echo "    ✅ Files pushed."
echo ""

# ── 3. Telegram bot token ─────────────────────────────────────────────────────
echo "🤖  TELEGRAM BOT SETUP"
echo "    ─────────────────────────────────────────────────────"
echo "    Step 1: Open Telegram and search for @BotFather"
echo "    Step 2: Send /newbot"
echo "    Step 3: Give it a name (e.g. 'My Flight Tracker')"
echo "    Step 4: Give it a username (e.g. 'sarath_flights_bot')"
echo "    Step 5: Copy the token BotFather gives you"
echo "    ─────────────────────────────────────────────────────"
echo ""
read -rsp "    Paste your Telegram Bot Token (hidden): " BOT_TOKEN
echo ""

gh secret set TELEGRAM_BOT_TOKEN \
  --body "$BOT_TOKEN" \
  --repo "$GH_USER/$REPO_NAME"
echo "    ✅ TELEGRAM_BOT_TOKEN saved to GitHub Secrets."
echo ""

# ── 4. Telegram chat ID ───────────────────────────────────────────────────────
echo "🆔  GET YOUR CHAT ID"
echo "    ─────────────────────────────────────────────────────"
echo "    Step 1: Open Telegram, find your new bot by its username"
echo "    Step 2: Send it any message (e.g. 'hello')"
echo "    Step 3: Open this URL in your browser (replace TOKEN):"
echo ""
echo "    https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
echo ""
echo "    Step 4: Find the number after \"id\": inside \"chat\": {}"
echo "            It looks like: 123456789"
echo "    ─────────────────────────────────────────────────────"
echo ""
read -rp "    Paste your Chat ID: " CHAT_ID

gh secret set TELEGRAM_CHAT_ID \
  --body "$CHAT_ID" \
  --repo "$GH_USER/$REPO_NAME"
echo "    ✅ TELEGRAM_CHAT_ID saved to GitHub Secrets."
echo ""

# ── 5. Enable Actions ─────────────────────────────────────────────────────────
echo "⚙️   Enabling GitHub Actions on the repo ..."
gh api \
  --method PUT \
  "repos/$GH_USER/$REPO_NAME/actions/permissions" \
  --field enabled=true \
  --field allowed_actions=all \
  --silent
echo "    ✅ Actions enabled."
echo ""

# ── 6. Trigger a test run ─────────────────────────────────────────────────────
echo "🚀  Triggering a test run now ..."
gh workflow run "flight-check.yml" --repo "$GH_USER/$REPO_NAME"
echo "    ✅ Run triggered!"
echo ""
echo "    Watch it live:"
echo "    👉 https://github.com/$GH_USER/$REPO_NAME/actions"
echo ""
echo "    If a deal is found, you'll get a Telegram message in ~3 minutes."
echo "    If nothing is under your ₹25,000 threshold, raise it in watchlist.yaml."
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅  Setup complete!                                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Repo  : https://github.com/$GH_USER/$REPO_NAME"
echo "  Runs  : 8am, 2pm, 8pm, 2am IST every day"
echo "  Config: edit watchlist.yaml and push to change anything"
echo ""
