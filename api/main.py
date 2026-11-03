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

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://sigmapaulalt.github.io/SmartModmail/")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://sigmapaulalt.github.io")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "modmail_bot")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")

app = FastAPI(title="SmartModmail API")
app.add_middleware(CORSMiddleware, allow_origins=[ALLOWED_ORIGIN], allow_credentials=True,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "Authorization"])
mongo = None
db = None
signer = URLSafeTimedSerializer(SESSION_SECRET or "development-only-change-me")


@app.on_event("startup")
async def startup():
    global mongo, db
    if MONGODB_URI:
        mongo = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        db = mongo[MONGODB_DB_NAME]
        await mongo.admin.command("ping")
        await db.login_codes.create_index("expires_at", expireAfterSeconds=0)
        await db.sessions.create_index("expires_at", expireAfterSeconds=0)


@app.on_event("shutdown")
async def shutdown():
    if mongo:
        mongo.close()


@app.get("/health")
async def health():
    return {"ok": True, "service": "smartmodmail-api", "mongodb_configured": bool(MONGODB_URI)}


def configured():
    required = {"MONGODB_URI": MONGODB_URI, "SESSION_SECRET": SESSION_SECRET,
                "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID, "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
                "DISCORD_REDIRECT_URI": DISCORD_REDIRECT_URI}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise HTTPException(503, "Missing environment variables: " + ", ".join(missing))
    if db is None:
        raise HTTPException(503, "MongoDB is not connected")


@app.get("/auth/login")
async def auth_login():
    configured()
    state = signer.dumps({"nonce": secrets.token_urlsafe(24)})
    query = urlencode({"client_id": DISCORD_CLIENT_ID, "redirect_uri": DISCORD_REDIRECT_URI,
                       "response_type": "code", "scope": "identify guilds", "state": state})
    return RedirectResponse("https://discord.com/oauth2/authorize?" + query)


@app.get("/auth/callback")
async def auth_callback(code: str = None, state: str = None):
    configured()
    if not code or not state:
        raise HTTPException(400, "Missing OAuth code or state.")
    try:
        signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, "Invalid or expired OAuth state.")
    async with httpx.AsyncClient(timeout=20) as client:
        token_res = await client.post("https://discord.com/api/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": DISCORD_REDIRECT_URI
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if token_res.status_code != 200:
            raise HTTPException(400, "Discord token exchange failed.")
        token_data = token_res.json()
        me_res = await client.get("https://discord.com/api/users/@me",
                                  headers={"Authorization": "Bearer " + token_data["access_token"]})
        if me_res.status_code != 200:
            raise HTTPException(400, "Could not retrieve Discord profile.")
        user = me_res.json()
    one_time = secrets.token_urlsafe(32)
    await db.login_codes.insert_one({"_id": one_time, "user": {
        "id": user["id"], "username": user.get("username"), "global_name": user.get("global_name"),
        "avatar": user.get("avatar")}, "access_token": token_data["access_token"],
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=2)})
    return RedirectResponse(FRONTEND_URL + "?login_code=" + one_time)


@app.post("/auth/exchange")
async def exchange(request: Request):
    configured()
    body = await request.json()
    code = body.get("code")
    if not code:
        raise HTTPException(400, "Missing login code.")
    record = await db.login_codes.find_one_and_delete({"_id": code})
    if not record or record["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(400, "Login code is invalid or expired.")
    session_token = secrets.token_urlsafe(48)
    await db.sessions.insert_one({"_id": session_token, "user": record["user"],
        "access_token": record["access_token"],
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7)})
    return {"token": session_token, "user": record["user"]}


async def session_for(request: Request):
    bearer = request.headers.get("Authorization", "")
    token = bearer.removeprefix("Bearer ").strip()
    if not token or db is None:
        raise HTTPException(401, "Not authenticated.")
    session = await db.sessions.find_one({"_id": token, "expires_at": {"$gt": datetime.now(timezone.utc)}})
    if not session:
        raise HTTPException(401, "Session expired. Sign in again.")
    return session


@app.get("/api/me")
async def me(request: Request):
    s = await session_for(request)
    return {"user": s["user"]}


@app.get("/api/guilds")
async def guilds(request: Request):
    s = await session_for(request)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://discord.com/api/users/@me/guilds",
            headers={"Authorization": "Bearer " + s["access_token"]})
    if response.status_code != 200:
        raise HTTPException(502, "Discord server lookup failed.")
    # Strict ownership: do not treat Administrator or Manage Server as ownership.
    owned = [g for g in response.json() if g.get("owner") is True]
    return {"guilds": owned}


@app.post("/auth/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token and db is not None:
        await db.sessions.delete_one({"_id": token})
    return {"ok": True}


@app.get("/api/config/{guild_id}")
async def config_not_integrated(guild_id: str, request: Request):
    await session_for(request)
    raise HTTPException(501, "Bot configuration is not integrated yet. Connect the bot's real MongoDB schema and reload mechanism before saving settings.")
