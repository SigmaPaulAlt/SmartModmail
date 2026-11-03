
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorClient
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired


# ==================================================
# CONFIGURATION
# ==================================================

FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://sigmapaulalt.github.io/SmartModmail/"
)

ALLOWED_ORIGIN = os.getenv(
    "ALLOWED_ORIGIN",
    "https://sigmapaulalt.github.io"
).strip().rstrip("/")

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "modmail_bot")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")


# ==================================================
# FASTAPI APPLICATION
# ==================================================

app = FastAPI(title="SmartModmail API")


# ==================================================
# CORS
# ==================================================

ALLOWED_ORIGINS = list(set([
    "https://sigmapaulalt.github.io",
    ALLOWED_ORIGIN
]))

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ==================================================
# DATABASE
# ==================================================

mongo = None
db = None

signer = URLSafeTimedSerializer(
    SESSION_SECRET or "development-only-change-me"
)


@app.on_event("startup")
async def startup():
    global mongo, db

    if MONGODB_URI:
        mongo = AsyncIOMotorClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000
        )

        db = mongo[MONGODB_DB_NAME]

        await mongo.admin.command("ping")

        await db.login_codes.create_index(
            "expires_at",
            expireAfterSeconds=0
        )

        await db.sessions.create_index(
            "expires_at",
            expireAfterSeconds=0
        )


@app.on_event("shutdown")
async def shutdown():
    if mongo:
        mongo.close()


# ==================================================
# CONFIGURATION VALIDATION
# ==================================================

def configured():
    required = {
        "MONGODB_URI": MONGODB_URI,
        "SESSION_SECRET": SESSION_SECRET,
        "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
        "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
        "DISCORD_REDIRECT_URI": DISCORD_REDIRECT_URI
    }

    missing = [
        key for key, value in required.items()
        if not value
    ]

    if missing:
        raise HTTPException(
            status_code=503,
            detail="Missing environment variables: "
            + ", ".join(missing)
        )

    if db is None:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is not connected."
        )


# ==================================================
# HEALTH CHECK
# ==================================================

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "smartmodmail-api",
        "mongodb_configured": bool(MONGODB_URI)
    }


# ==================================================
# DISCORD LOGIN
# ==================================================

@app.get("/auth/login")
async def auth_login():
    configured()

    state = signer.dumps({
        "nonce": secrets.token_urlsafe(24)
    })

    query = urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state
    })

    discord_url = (
        "https://discord.com/oauth2/authorize?"
        + query
    )

    return RedirectResponse(discord_url)


# ==================================================
# DISCORD OAUTH CALLBACK
# ==================================================

@app.get("/auth/callback")
async def auth_callback(
    code: str = None,
    state: str = None
):
    configured()

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth code or state."
        )

    try:
        signer.loads(state, max_age=600)

    except (BadSignature, SignatureExpired):
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state."
        )

    async with httpx.AsyncClient(timeout=20) as client:

        token_response = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail="Discord token exchange failed."
            )

        token_data = token_response.json()

        user_response = await client.get(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": (
                    "Bearer " + token_data["access_token"]
                )
            }
        )

        if user_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail="Could not retrieve Discord profile."
            )

        user = user_response.json()

    login_code = secrets.token_urlsafe(32)

    await db.login_codes.insert_one({
        "_id": login_code,
        "user": {
            "id": user["id"],
            "username": user.get("username"),
            "global_name": user.get("global_name"),
            "avatar": user.get("avatar")
        },
        "access_token": token_data["access_token"],
        "expires_at": (
            datetime.now(timezone.utc)
            + timedelta(minutes=2)
        )
    })

    return RedirectResponse(
        FRONTEND_URL + "?login_code=" + login_code
    )


# ==================================================
# EXCHANGE LOGIN CODE FOR SESSION
# ==================================================

@app.post("/auth/exchange")
async def exchange(request: Request):
    configured()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON request."
        )

    login_code = body.get("code")

    if not login_code:
        raise HTTPException(
            status_code=400,
            detail="Missing login code."
        )

    # Login codes can only be used once.
    record = await db.login_codes.find_one_and_delete({
        "_id": login_code
    })

    if not record:
        raise HTTPException(
            status_code=400,
            detail="Login code is invalid or expired."
        )

    # MongoDB commonly returns timezone-naive datetimes.
    # Convert to UTC before comparing.
    expires_at = record["expires_at"]

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(
            tzinfo=timezone.utc
        )

    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Login code is invalid or expired."
        )

    # Create dashboard session.
    session_token = secrets.token_urlsafe(48)

    await db.sessions.insert_one({
        "_id": session_token,
        "user": record["user"],
        "access_token": record["access_token"],
        "expires_at": (
            datetime.now(timezone.utc)
            + timedelta(days=7)
        )
    })

    return {
        "token": session_token,
        "user": record["user"]
    }


# ==================================================
# SESSION AUTHENTICATION
# ==================================================

async def session_for(request: Request):
    authorization = request.headers.get(
        "Authorization", ""
    )

    token = authorization.removeprefix(
        "Bearer "
    ).strip()

    if not token or db is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated."
        )

    session = await db.sessions.find_one({
        "_id": token,
        "expires_at": {
            "$gt": datetime.now(timezone.utc)
        }
    })

    if not session:
        raise HTTPException(
            status_code=401,
            detail="Session expired. Sign in again."
        )

    return session


# ==================================================
# CURRENT USER
# ==================================================

@app.get("/api/me")
async def me(request: Request):
    session = await session_for(request)

    return {
        "user": session["user"]
    }


# ==================================================
# DISCORD SERVERS
# ONLY ACTUAL OWNERS
# ==================================================

@app.get("/api/guilds")
async def guilds(request: Request):
    session = await session_for(request)

    async with httpx.AsyncClient(timeout=20) as client:

        response = await client.get(
            "https://discord.com/api/users/@me/guilds",
            headers={
                "Authorization": (
                    "Bearer " + session["access_token"]
                )
            }
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Discord server lookup failed."
        )

    # Only actual owners are included.
    # Administrator and Manage Server permissions
    # do not count as ownership.

    owned_guilds = [
        guild
        for guild in response.json()
        if guild.get("owner") is True
    ]

    return {
        "guilds": owned_guilds
    }


# ==================================================
# LOGOUT
# ==================================================

@app.post("/auth/logout")
async def logout(request: Request):
    authorization = request.headers.get(
        "Authorization", ""
    )

    token = authorization.removeprefix(
        "Bearer "
    ).strip()

    if token and db is not None:
        await db.sessions.delete_one({
            "_id": token
        })

    return {
        "ok": True
    }


# ==================================================
# CONFIGURATION PLACEHOLDER
# ==================================================

@app.get("/api/config/{guild_id}")
async def config_not_integrated(
    guild_id: str,
    request: Request
):
    await session_for(request)

    raise HTTPException(
        status_code=501,
        detail=(
            "Bot configuration is not integrated yet. "
            "The bot's MongoDB schema and reload mechanism "
            "must be connected before settings can be saved."
        )
    )
