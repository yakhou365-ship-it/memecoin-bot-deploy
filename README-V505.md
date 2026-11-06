# Railway Volume setup (V505)

Without a persistent Volume, every Railway redeploy wipes the SQLite file
(`memecoin_bot.db`) — which means **open trades + risk state + peak_equity
all disappear mid-trade**. The bot would then:
- Forget it has live positions on-chain (tokens still in the wallet but no
  DB row tracking them).
- Reset `consecutive_losses = 0` → cooldown gates can't fire.
- Reset `peak_equity` → drawdown gate starts from zero.

**This is a hard requirement before any real trading.**

---

## Setup steps (do this once, takes ~2 minutes)

1. **Open Railway dashboard** → `memecoin-bot-deploy` project → service settings.

2. **Settings → Volumes → Add Volume**:
   - Mount path: `/app/data`
   - Name: `bot-data` (or whatever you like)
   - Size: 1 GB is plenty (the DB will be a few MB even after months of trades).
   - Click **Add Volume**.

3. **Verify the env var**:
   - Railway auto-injects `RAILWAY_VOLUME_MOUNT_PATH=/app/data` when a Volume
     is attached. The V505 code in `app/core/config.py` reads this and rewrites
     `DATABASE_URL` to `sqlite+aiosqlite:////app/data/memecoin_bot.db`
     (absolute path on the Volume).
   - If you have an explicit `DATABASE_URL` env var set in Railway,
     **delete it** so the auto-detection takes over (otherwise your
     explicit value wins and the Volume is bypassed).

4. **Redeploy**:
   - Railway → Settings → Restart, or trigger a deploy via GitHub push.
   - On startup, the bot will create the SQLite file fresh on the Volume.

5. **Verify**:
   - Visit `https://memecoin-bot-deploy-production.up.railway.app/health` —
     should return `{"status":"ok",...}`.
   - Visit the dashboard, start trading (paper), open a paper trade.
   - Force a redeploy (push any commit).
   - **Check the dashboard again**: if the paper trade is still there,
     the Volume works. If it's gone, the Volume isn't mounted correctly.

---

## How the V505 code detects the Volume

In `backend/app/core/config.py`:

```python
def _resolve_database_url() -> str:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit  # operator override
    volume_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return f"sqlite+aiosqlite:///{volume_path.rstrip('/')}/data/memecoin_bot.db"
    if os.path.isdir("/app/data"):
        return "sqlite+aiosqlite:////app/data/memecoin_bot.db"
    return "sqlite+aiosqlite:///./data/memecoin_bot.db"  # local dev
```

Order of preference:
1. Explicit `DATABASE_URL` env var (always wins — be careful!)
2. `RAILWAY_VOLUME_MOUNT_PATH` injected by Railway when a Volume is attached
3. `/app/data` directory exists (Docker absolute path)
4. `./data/memecoin_bot.db` (local development)

---

## Backups

The Volume is a single SQLite file. To back it up:

```bash
# On your laptop, with Railway CLI installed and authed:
railway ssh --service memecoin-bot-deploy
# Inside the container:
cp /app/data/memecoin_bot.db /tmp/backup-$(date +%Y%m%d).db
exit
# Download via Railway's volume file browser (CLI doesn't support SCP yet).
```

Or simpler: install `sqlite3` on the container, run `.dump` to SQL, and
expose it via a new `/api/database/export` endpoint (already exists —
see `app/api/routes.py::export_database`).

---

## What happens if you DON'T set up the Volume?

- Every Railway redeploy (auto-deploy on git push, manual restart, config
  change) wipes the SQLite file.
- The bot starts "fresh" each time, doesn't know about open positions.
- Risk state (`consecutive_losses`, `peak_equity`, `circuit_breaker_active`)
  resets → cooldown / drawdown gates can't fire correctly.
- **Tokens may be left in the wallet with no DB tracking** — manual
  recovery needed (check Solscan for the wallet's SPL token balances).
