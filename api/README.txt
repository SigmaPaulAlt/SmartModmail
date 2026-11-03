SmartModmail API (FastAPI)

Render settings (Root Directory: api)
  Build:  pip install -r requirements.txt
  Start:  uvicorn main:app --host 0.0.0.0 --port $PORT
  Health: /health

Environment variables (unchanged):
  MONGODB_URI, MONGODB_DB_NAME (default modmail_bot), SESSION_SECRET,
  DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET,
  DISCORD_REDIRECT_URI=https://smartmodmail-api.onrender.com/auth/callback
  FRONTEND_URL=https://sigmapaulalt.github.io/SmartModmail/
  ALLOWED_ORIGIN=https://sigmapaulalt.github.io

HOW SAVING WORKS
  The dashboard never writes bot settings straight into MongoDB. It queues a
  change in the dashboard_changes collection; the bot (bot.py) picks it up
  within ~5 seconds, applies it through its own config manager, saves it, and
  marks the change applied/failed. This keeps the bot's in-memory config and
  the database consistent (writing directly would be overwritten by the bot).

  The bot also reports its real, effective settings in dashboard_instances
  (effective_config) every 30s and after every applied change, so the
  dashboard always shows true values, including defaults.

  Only the settings listed in PAGES (api/main.py) can be changed, and the same
  allowlist is enforced inside the bot (DASHBOARD_EDITABLE_KEYS in bot.py).
  Keep the two lists in sync when adding settings.

Only the actual Discord server owner can use the dashboard for a server.
The Owner permission level cannot be edited from the dashboard.
