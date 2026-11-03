# SmartModmail API starter

## Render deployment

- Root Directory: `api`
- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`

## Environment variables

Set these in Render's Environment page. Never commit secret values to GitHub.

- `MONGODB_URI`: your rotated MongoDB connection string
- `MONGODB_DB_NAME`: `modmail_bot`
- `SESSION_SECRET`: a long random secret (at least 32 random characters)
- `DISCORD_CLIENT_ID`: Discord application's client ID
- `DISCORD_CLIENT_SECRET`: Discord OAuth2 client secret
- `DISCORD_REDIRECT_URI`: `https://YOUR-RENDER-SERVICE.onrender.com/auth/callback`
- `FRONTEND_URL`: `https://sigmapaulalt.github.io/SmartModmail/`
- `ALLOWED_ORIGIN`: `https://sigmapaulalt.github.io`

## Included

- API health endpoint
- Discord OAuth2 login
- One-time login-code exchange
- Authenticated user profile
- List of Discord servers where the signed-in user has Manage Server or Administrator permission
- MongoDB-backed temporary login codes and sessions

## Not yet enabled

This starter intentionally does not edit the bot's existing configuration collection. The bot's configuration schema and guild-to-bot mapping must first be verified, and the bot needs a safe runtime reload mechanism. Do not enable configuration writes until that integration is implemented and tested.

Do not put bot tokens, MongoDB credentials, Discord client secrets, or Render secrets in frontend JavaScript or GitHub.
