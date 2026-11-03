Render settings (Root Directory: api)
Build: pip install -r requirements.txt
Start: uvicorn main:app --host 0.0.0.0 --port $PORT
Health: /health

Keep your existing Render environment variables. Confirm:
FRONTEND_URL=https://sigmapaulalt.github.io/SmartModmail/
ALLOWED_ORIGIN=https://sigmapaulalt.github.io
DISCORD_REDIRECT_URI=https://smartmodmail-api.onrender.com/auth/callback

Discord Developer Portal OAuth2 Redirects must contain the exact DISCORD_REDIRECT_URI.
This API only lists guilds where Discord returns owner=true. It does not consider Administrator or Manage Server permission sufficient.

IMPORTANT: Configuration pages are a redesigned UI but saving is intentionally not enabled. The current bot's actual MongoDB config schema and runtime reload mechanism have not been provided, so guessing would risk corrupting or writing unused settings. The /api/config endpoint remains HTTP 501 until the bot source is integrated.
