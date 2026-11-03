import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"

FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://sigmapaulalt.github.io/SmartModmail/"
).rstrip("/") + "/"
ALLOWED_ORIGIN = os.getenv(
    "ALLOWED_ORIGIN",
    "https://sigmapaulalt.github.io"
)
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "modmail_bot")

app = FastAPI(title="SmartModmail API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

bearer = HTTPBearer(auto_error=False)
mongo_client = None
db = None


def utcnow():
    return datetime.now(timezone.utc)


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(status_code=503, detail=f"Missing environment variable: {name}")
    return value


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_serializer():
    return URLSafeTimedSerializer(
        required_env("SESSION_SECRET"),
        salt="smartmodmail-discord-oauth-state",
    )


@app.on_event("startup")
async def startup():
    global mongo_client, db
    mongo_uri = os.getenv("MONGODB_URI")
    if mongo_uri:
        mongo_client = AsyncIOMotorClient(
            mongo_uri,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
        )
        db = mongo_client[MONGODB_DB_NAME]
        await db.command("ping")
        await db.web_login_codes.create_index("expires_at", expireAfterSeconds=0)
        await db.web_sessions.create_index("expires_at", expireAfterSeconds=0)


@app.on_event("shutdown")
async def shutdown():
    if mongo_client is not None:
        mongo_client.close()


def get_db():
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB is not configured.")
    return db


@app.get("/")
async def root():
    return {"name": "SmartModmail API", "status": "online", "docs": "/docs"}


@app.get("/health")
async def health():
    if db is None:
        return {"status": "ok", "database": "not_configured"}
    try:
        await db.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        return {"status": "degraded", "database": "unavailable"}


@app.get("/auth/login")
async def discord_login():
    params = {
        "client_id": required_env("DISCORD_CLIENT_ID"),
        "redirect_uri": required_env("DISCORD_REDIRECT_URI"),
        "response_type": "code",
        "scope": "identify guilds",
        "state": state_serializer().dumps({"nonce": secrets.token_urlsafe(24)}),
        "prompt": "consent",
    }
    return RedirectResponse(DISCORD_OAUTH + "?" + urlencode(params))


@app.get("/auth/callback")
async def discord_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail="Discord login was cancelled.")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state.")

    try:
        state_serializer().loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Login state is invalid or expired.")

    database = get_db()
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": required_env("DISCORD_CLIENT_ID"),
                "client_secret": required_env("DISCORD_CLIENT_SECRET"),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": required_env("DISCORD_REDIRECT_URI"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_response.status_code != 200:
            raise HTTPException(status_code=502, detail="Discord token exchange failed.")
        token_data = token_response.json()

        user_response = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if user_response.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not retrieve Discord profile.")
        user_data = user_response.json()

    login_code = secrets.token_urlsafe(32)
    now = utcnow()
    await database.web_login_codes.insert_one({
        "code_hash": sha256(login_code),
        "access_token": token_data["access_token"],
        "user": {
            "id": user_data["id"],
            "username": user_data.get("username"),
            "global_name": user_data.get("global_name"),
            "avatar": user_data.get("avatar"),
        },
        "expires_at": now + timedelta(minutes=5),
        "created_at": now,
    })
    return RedirectResponse(FRONTEND_URL + "?login_code=" + login_code)


class ExchangeRequest(BaseModel):
    login_code: str


@app.post("/auth/exchange")
async def exchange_login_code(payload: ExchangeRequest):
    database = get_db()
    now = utcnow()
    login = await database.web_login_codes.find_one_and_delete({
        "code_hash": sha256(payload.login_code),
        "expires_at": {"$gt": now},
    })
    if not login:
        raise HTTPException(status_code=400, detail="Login code is invalid or expired.")

    session_token = secrets.token_urlsafe(48)
    await database.web_sessions.insert_one({
        "token_hash": sha256(session_token),
        "discord_access_token": login["access_token"],
        "user": login["user"],
        "expires_at": now + timedelta(hours=12),
        "created_at": now,
    })
    return {
        "access_token": session_token,
        "token_type": "bearer",
        "expires_in": 43200,
        "user": login["user"],
    }


async def current_session(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Please sign in with Discord.")
    database = get_db()
    session = await database.web_sessions.find_one({
        "token_hash": sha256(credentials.credentials),
        "expires_at": {"$gt": utcnow()},
    })
    if not session:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return session


@app.get("/api/me")
async def me(session=Depends(current_session)):
    return {"user": session["user"]}


@app.get("/api/guilds")
async def guilds(session=Depends(current_session)):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {session['discord_access_token']}"},
        )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Discord login expired.")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not retrieve Discord servers.")

    manageable = []
    for guild in response.json():
        permissions = int(guild.get("permissions", "0"))
        owner = bool(guild.get("owner", False))
        can_manage = owner or bool(permissions & 0x8) or bool(permissions & 0x20)
        if can_manage:
            manageable.append({
                "id": guild["id"],
                "name": guild["name"],
                "icon": guild.get("icon"),
                "owner": owner,
            })
    return {"guilds": manageable}


@app.post("/auth/logout")
async def logout(session=Depends(current_session)):
    await get_db().web_sessions.delete_one({"_id": session["_id"]})
    return {"ok": True}


@app.get("/api/config/{guild_id}")
async def config_not_ready(guild_id: str, session=Depends(current_session)):
    # Configuration writes remain disabled until the existing bot registers
    # a trusted guild-to-bot mapping and a safe runtime reload hook is added.
    raise HTTPException(
        status_code=501,
        detail="Bot configuration integration is not enabled yet.",
    )
