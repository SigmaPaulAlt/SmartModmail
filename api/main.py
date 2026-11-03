import os, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorClient
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://sigmapaulalt.github.io/SmartModmail/')
ALLOWED_ORIGIN = os.getenv('ALLOWED_ORIGIN', 'https://sigmapaulalt.github.io').strip().rstrip('/')
MONGODB_URI = os.getenv('MONGODB_URI', '')
MONGODB_DB_NAME = os.getenv('MONGODB_DB_NAME', 'modmail_bot')
SESSION_SECRET = os.getenv('SESSION_SECRET', '')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', '')

app = FastAPI(title='SmartModmail API')
app.add_middleware(CORSMiddleware, allow_origins=list(set(['https://sigmapaulalt.github.io', ALLOWED_ORIGIN])), allow_credentials=True, allow_methods=['GET','POST','PUT','DELETE','OPTIONS'], allow_headers=['*'])
mongo = None
db = None
signer = URLSafeTimedSerializer(SESSION_SECRET or 'development-only-change-me')

EDITABLE_CONFIG_KEYS = {
    'prefix','mention','main_category_id','fallback_category_id','log_channel_id','mention_channel_id','update_channel_id',
    'main_color','error_color','user_typing','mod_typing','reply_without_command','anon_reply_without_command','plain_reply_without_command',
    'thread_auto_close','thread_auto_close_response','thread_creation_response','thread_creation_footer','thread_creation_title',
    'thread_creation_send_dm_embed','thread_close_title','thread_close_response','thread_self_close_response','thread_close_footer',
    'require_close_reason','show_timestamp','close_on_leave','close_on_leave_reason','alert_on_mention','silent_alert_on_mention',
    'anonymous_snippets','plain_snippets','thread_show_roles','thread_show_account_age','thread_show_join_age','thread_contact_silently',
    'recipient_thread_close','thread_min_characters','thread_min_characters_title','thread_min_characters_response','thread_min_characters_footer',
    'thread_creation_menu_enabled','thread_creation_menu_embed_text','thread_creation_menu_dropdown_placeholder'
}

@app.on_event('startup')
async def startup():
    global mongo, db
    if MONGODB_URI:
        mongo = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        db = mongo[MONGODB_DB_NAME]
        await mongo.admin.command('ping')
        await db.login_codes.create_index('expires_at', expireAfterSeconds=0)
        await db.sessions.create_index('expires_at', expireAfterSeconds=0)
        await db.dashboard_instances.create_index([('guild_id',1),('kind',1)])

@app.on_event('shutdown')
async def shutdown():
    if mongo: mongo.close()

@app.get('/health')
async def health():
    return {'ok': True, 'service': 'smartmodmail-api', 'mongodb_configured': bool(MONGODB_URI)}

def configured():
    required = {'MONGODB_URI':MONGODB_URI,'SESSION_SECRET':SESSION_SECRET,'DISCORD_CLIENT_ID':DISCORD_CLIENT_ID,'DISCORD_CLIENT_SECRET':DISCORD_CLIENT_SECRET,'DISCORD_REDIRECT_URI':DISCORD_REDIRECT_URI}
    missing = [k for k,v in required.items() if not v]
    if missing: raise HTTPException(503, 'Missing environment variables: '+', '.join(missing))
    if db is None: raise HTTPException(503, 'MongoDB is not connected')

@app.get('/auth/login')
async def auth_login():
    configured()
    state = signer.dumps({'nonce':secrets.token_urlsafe(24)})
    q = urlencode({'client_id':DISCORD_CLIENT_ID,'redirect_uri':DISCORD_REDIRECT_URI,'response_type':'code','scope':'identify guilds','state':state})
    return RedirectResponse('https://discord.com/oauth2/authorize?'+q)

@app.get('/auth/callback')
async def auth_callback(code: str=None, state: str=None):
    configured()
    if not code or not state: raise HTTPException(400,'Missing OAuth code or state.')
    try: signer.loads(state,max_age=600)
    except (BadSignature,SignatureExpired): raise HTTPException(400,'Invalid or expired OAuth state.')
    async with httpx.AsyncClient(timeout=20) as c:
        tr = await c.post('https://discord.com/api/oauth2/token',data={'client_id':DISCORD_CLIENT_ID,'client_secret':DISCORD_CLIENT_SECRET,'grant_type':'authorization_code','code':code,'redirect_uri':DISCORD_REDIRECT_URI},headers={'Content-Type':'application/x-www-form-urlencoded'})
        if tr.status_code != 200: raise HTTPException(400,'Discord token exchange failed.')
        td = tr.json()
        ur = await c.get('https://discord.com/api/users/@me',headers={'Authorization':'Bearer '+td['access_token']})
        if ur.status_code != 200: raise HTTPException(400,'Could not retrieve Discord profile.')
        user = ur.json()
    one_time = secrets.token_urlsafe(32)
    await db.login_codes.insert_one({'_id':one_time,'user':{'id':user['id'],'username':user.get('username'),'global_name':user.get('global_name'),'avatar':user.get('avatar')},'access_token':td['access_token'],'expires_at':datetime.now(timezone.utc)+timedelta(minutes=2)})
    return RedirectResponse(FRONTEND_URL+'?login_code='+one_time)

@app.post('/auth/exchange')
async def exchange(request: Request):
    configured(); body = await request.json(); code = body.get('code')
    if not code: raise HTTPException(400,'Missing login code.')
    record = await db.login_codes.find_one_and_delete({'_id':code})
    if not record: raise HTTPException(400,'Login code is invalid or expired.')
    expiry = record['expires_at']
    if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc): raise HTTPException(400,'Login code is invalid or expired.')
    token = secrets.token_urlsafe(48)
    await db.sessions.insert_one({'_id':token,'user':record['user'],'access_token':record['access_token'],'expires_at':datetime.now(timezone.utc)+timedelta(days=7)})
    return {'token':token,'user':record['user']}

async def session_for(request):
    token = request.headers.get('Authorization','').removeprefix('Bearer ').strip()
    if not token or db is None: raise HTTPException(401,'Not authenticated.')
    s = await db.sessions.find_one({'_id':token,'expires_at':{'$gt':datetime.now(timezone.utc)}})
    if not s: raise HTTPException(401,'Session expired. Sign in again.')
    return s

async def owned_guilds(s):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get('https://discord.com/api/users/@me/guilds',headers={'Authorization':'Bearer '+s['access_token']})
    if r.status_code != 200: raise HTTPException(502,'Discord server lookup failed.')
    return [g for g in r.json() if g.get('owner') is True]

async def require_owner(s,guild_id):
    guild = next((g for g in await owned_guilds(s) if g['id']==str(guild_id)),None)
    if guild is None: raise HTTPException(403,'You must be the actual owner of this Discord server.')
    return guild

async def config_doc(guild_id):
    # ConfigManager deliberately does not persist protected guild_id in config.
    # The host/child heartbeat links a server to the child bot_id instead.
    instance = await db.dashboard_instances.find_one({
        'guild_id': str(guild_id), 'kind': 'instance'
    })
    if not instance or not instance.get('bot_id'):
        raise HTTPException(404, 'No running Modmail instance is registered for this server yet.')
    bot_id = str(instance['bot_id'])
    candidates = [bot_id]
    try: candidates.append(int(bot_id))
    except ValueError: pass
    doc = await db.config.find_one({'bot_id': {'$in': candidates}})
    if doc is None:
        raise HTTPException(404, 'No Modmail configuration found for this bot yet.')
    return doc

@app.get('/api/me')
async def me(request: Request):
    return {'user':(await session_for(request))['user']}

@app.get('/api/guilds')
async def guilds(request: Request):
    return {'guilds':await owned_guilds(await session_for(request))}

@app.get('/api/instances')
async def instances(request: Request):
    s = await session_for(request); owned = await owned_guilds(s); ids = [g['id'] for g in owned]
    docs = await db.dashboard_instances.find({'kind':'instance','guild_id':{'$in':ids}}).to_list(length=200)
    names = {g['id']:g['name'] for g in owned}
    return {'instances':[{'guild_id':str(d.get('guild_id')),'guild_name':d.get('guild_name') or names.get(str(d.get('guild_id'))),'bot_id':d.get('bot_id'),'bot_name':d.get('bot_name'),'avatar_url':d.get('avatar_url'),'status':d.get('status','unknown'),'last_seen':d.get('last_seen')} for d in docs]}

@app.get('/api/config/{guild_id}')
async def get_config(guild_id: str, request: Request):
    s=await session_for(request); await require_owner(s,guild_id); doc=await config_doc(guild_id)
    return {k:doc[k] for k in EDITABLE_CONFIG_KEYS if k in doc}

@app.put('/api/config/{guild_id}')
async def put_config(guild_id: str, request: Request):
    s=await session_for(request); await require_owner(s,guild_id); body=await request.json()
    if not isinstance(body,dict) or not body: raise HTTPException(400,'Provide at least one setting.')
    bad=set(body)-EDITABLE_CONFIG_KEYS
    if bad: raise HTTPException(400,'Unsupported settings: '+', '.join(sorted(bad)))
    if any(isinstance(v,str) and len(v)>5000 for v in body.values()): raise HTTPException(400,'A setting is too long.')
    doc=await config_doc(guild_id)
    await db.config.update_one({'_id':doc['_id']},{'$set':body})
    return {'ok':True,'message':'Saved. Running child bots refresh configuration every 30 seconds after the RRHosting patch is installed.'}

@app.get('/api/snippets/{guild_id}')
async def get_snippets(guild_id: str, request: Request):
    s=await session_for(request); await require_owner(s,guild_id); doc=await config_doc(guild_id)
    snippets=doc.get('snippets') or {}
    return {'snippets':snippets if isinstance(snippets,dict) else {}}

@app.put('/api/snippets/{guild_id}')
async def put_snippet(guild_id: str, request: Request):
    s=await session_for(request); await require_owner(s,guild_id); body=await request.json()
    name=str(body.get('name','')).strip(); content=body.get('content')
    if not name or len(name)>50 or '.' in name or '$' in name or name.startswith('$'): raise HTTPException(400,'Snippet name must be 1–50 characters and cannot contain . or $.')
    if not isinstance(content,str) or not content.strip() or len(content)>4000: raise HTTPException(400,'Snippet content must be 1–4000 characters.')
    doc=await config_doc(guild_id)
    await db.config.update_one({'_id':doc['_id']},{'$set':{f'snippets.{name}':content}})
    return {'ok':True}

@app.delete('/api/snippets/{guild_id}/{name}')
async def delete_snippet(guild_id: str, name: str, request: Request):
    s=await session_for(request); await require_owner(s,guild_id); doc=await config_doc(guild_id)
    await db.config.update_one({'_id':doc['_id']},{'$unset':{f'snippets.{name}':''}})
    return {'ok':True}

@app.post('/auth/logout')
async def logout(request: Request):
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip()
    if token and db is not None: await db.sessions.delete_one({'_id':token})
    return {'ok':True}
