#!/usr/bin/env bash
# setup_railway_volume.sh — attach a persistent Volume to the bot service on Railway.
#
# Run this from your laptop (NOT from inside the Railway container). It:
#   1. Logs in to Railway CLI (opens browser the first time)
#   2. Links to the memecoin-bot-deploy project
#   3. Adds a 1 GB Volume mounted at /app/data
#   4. Removes any explicit DATABASE_URL env var so the V505 auto-detection wins
#   5. Triggers a redeploy
#   6. Waits for /health to come back green
#
# After this runs, the SQLite file lives on the Volume and survives redeploys.

set -euo pipefail

PROJECT_ID="${RAILWAY_PROJECT_ID:-39e008c8-837d-460b-ace2-8666052e728c}"
SERVICE_NAME="${RAILWAY_SERVICE_NAME:-memecoin-bot-deploy}"
VOLUME_PATH="${RAILWAY_VOLUME_PATH:-/app/data}"
VOLUME_SIZE_MB="${RAILWAY_VOLUME_SIZE_MB:-1024}"  # 1 GB
APP_URL="${APP_URL:-https://memecoin-bot-deploy-production.up.railway.app}"

echo "=== 1. Login to Railway (opens browser the first time) ==="
railway whoami 2>/dev/null || railway login

echo ""
echo "=== 2. Link the project (use --environment production) ==="
railway link --project "$PROJECT_ID" --environment production 2>&1 || true

echo ""
echo "=== 3. Add Volume ($VOLUME_SIZE_MB MB) mounted at $VOLUME_PATH ==="
railway volume add \
  --mount-path "$VOLUME_PATH" \
  --size "$VOLUME_SIZE_MB" \
  --service "$SERVICE_NAME" 2>&1 || {
    echo "❌ Failed to add Volume."
    echo "   Most likely the service name is wrong. List services with:"
    echo "   railway status"
    echo "   Then re-run with RAILWAY_SERVICE_NAME=<correct name> $0"
    exit 1
  }

echo ""
echo "=== 4. Remove the explicit DATABASE_URL env var (so V505 auto-detection wins) ==="
# V505 config.py prefers explicit DATABASE_URL over the Volume path.
# To let the auto-detection work, we must unset it.
railway variables unset DATABASE_URL --service "$SERVICE_NAME" 2>&1 || {
    echo "⚠️  Could not unset DATABASE_URL — it may not have been set, or you need to do this manually:"
    echo "   Railway dashboard → Variables → delete DATABASE_URL"
  }

echo ""
echo "=== 5. Trigger a redeploy ==="
railway redeploy --service "$SERVICE_NAME" 2>&1 || {
    echo "⚠️  Could not trigger redeploy via CLI. Pushing an empty commit instead."
    cd "$(dirname "$0")/.."
    git commit --allow-empty -m "Trigger redeploy after Volume attach" && git push origin main
  }

echo ""
echo "=== 6. Wait for /health to come back green ==="
echo "    Polling $APP_URL/health every 15s for up to 5 minutes..."
for i in $(seq 1 20); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$APP_URL/health" 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo "    ✅ Bot is back up (after $((i*15))s)"
    curl -s "$APP_URL/health"
    echo ""
    break
  fi
  echo "    attempt $i: HTTP $STATUS, waiting 15s..."
  sleep 15
done

echo ""
echo "=== 7. Verify the Volume is mounted ==="
echo "    The V505 code logs the resolved DATABASE_URL on startup."
echo "    Check Railway → Deploy Logs for:"
echo "      'V505: DATABASE_URL = sqlite+aiosqlite:////app/data/data/memecoin_bot.db'"
echo "    or run this from your laptop:"
echo "      railway ssh --service $SERVICE_NAME 'ls -la /app/data/'"

echo ""
echo "=== DONE ==="
echo "If all steps above succeeded, the SQLite file now persists across redeploys."
echo ""
echo "Next steps (in order):"
echo "  1. Open the dashboard: $APP_URL/"
echo "  2. Enter your API_AUTH_KEY when prompted"
echo "  3. Don't click 'ابدأ' yet — first verify the Volume works:"
echo "     a. Open a paper trade via the dashboard"
echo "     b. Force a redeploy (push any commit)"
echo "     c. Refresh the dashboard — the trade should still be there"
echo "  4. Once verified, you're ready to start real trading."
