import os, re, secrets, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import isodate
from bson import ObjectId
from bson.errors import InvalidId
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

ONLINE_WINDOW = timedelta(seconds=120)   # heartbeat is every 30s
MAX_PENDING_CHANGES = 50
CHANGE_TTL = timedelta(days=7)

mongo = None
db = None
signer = URLSafeTimedSerializer(SESSION_SECRET or 'development-only-change-me')


# ---------------------------------------------------------------------------
# Settings schema. This is the single source of truth the dashboard renders
# from. The key list MUST match DASHBOARD_EDITABLE_KEYS in the bot's bot.py.
# ---------------------------------------------------------------------------
def F(key, type_, label, help_='', **kw):
    return {'key': key, 'type': type_, 'label': label, 'help': help_, **kw}


PAGES = {
    'general': {
        'title': 'General settings',
        'sub': 'Core Modmail behaviour.',
        'groups': [
            {'title': 'Core behaviour', 'fields': [
                F('prefix', 'string', 'Command prefix', 'Characters staff type before a command.', max=10, pattern=r'\S+', placeholder='?'),
                F('mention', 'string', 'Mention on new thread', 'Pinged when a thread opens, e.g. @here or <@&ROLE_ID>.', max=200, placeholder='@here'),
                F('show_timestamp', 'bool', 'Show timestamps', 'Add timestamps to embeds.'),
                F('user_typing', 'bool', 'Relay user typing', 'Show a typing indicator in the thread when the user types in DMs.'),
                F('mod_typing', 'bool', 'Relay staff typing', 'Show a typing indicator in the user\'s DMs when staff type in the thread.'),
            ]},
            {'title': 'Replies', 'fields': [
                F('reply_without_command', 'bool', 'Reply without a command', 'Anything typed in a thread channel is sent to the user.'),
                F('anon_reply_without_command', 'bool', 'Anonymous replies without a command', 'Replies sent this way hide the staff member\'s name.'),
                F('plain_reply_without_command', 'bool', 'Plain replies without a command', 'Replies sent this way use plain text instead of an embed.'),
                F('anonymous_snippets', 'bool', 'Anonymous snippets', 'Snippets are sent anonymously.'),
                F('plain_snippets', 'bool', 'Plain snippets', 'Snippets are sent as plain text.'),
            ]},
            {'title': 'Alerts', 'fields': [
                F('alert_on_mention', 'bool', 'Alert when the bot is mentioned', 'Post an alert in the mention channel.'),
                F('silent_alert_on_mention', 'bool', 'Silent mention alerts', 'Do not ping anyone when posting the alert.'),
                F('mention_channel_id', 'id', 'Mention alert channel ID', 'Defaults to the log channel.'),
            ]},
            {'title': 'When a user leaves', 'fields': [
                F('close_on_leave', 'bool', 'Close thread when user leaves', ''),
                F('close_on_leave_reason', 'text', 'Close reason', 'Logged as the close message.', max=1000),
            ]},
        ],
    },
    'tickets': {
        'title': 'Ticket settings',
        'sub': 'Where threads are created and what they show.',
        'groups': [
            {'title': 'Channels & categories', 'fields': [
                F('main_category_id', 'id', 'Main category ID', 'Category where new thread channels are created.'),
                F('fallback_category_id', 'id', 'Fallback category ID', 'Used if the main category is full.'),
                F('log_channel_id', 'id', 'Log channel ID', 'Where thread logs are posted.'),
                F('update_channel_id', 'id', 'Update channel ID', 'Where bot update notices are posted.'),
            ]},
            {'title': 'Thread behaviour', 'fields': [
                F('recipient_thread_close', 'bool', 'Let users close their own thread', 'Users can react to close.'),
                F('require_close_reason', 'bool', 'Require a close reason', ''),
                F('thread_contact_silently', 'bool', 'Contact silently', 'Do not notify the user when staff open a thread with them.'),
                F('thread_show_roles', 'bool', 'Show roles in thread info', ''),
                F('thread_show_account_age', 'bool', 'Show account age in thread info', ''),
                F('thread_show_join_age', 'bool', 'Show server join age in thread info', ''),
            ]},
            {'title': 'Minimum message length', 'fields': [
                F('thread_min_characters', 'int', 'Minimum characters', '0 disables the check.', min=0, max=2000),
                F('thread_min_characters_title', 'string', 'Too-short title', '', max=256),
                F('thread_min_characters_response', 'text', 'Too-short response', '', max=2000),
                F('thread_min_characters_footer', 'string', 'Too-short footer', '', max=2048),
            ]},
        ],
    },
    'messages': {
        'title': 'Messages & embeds',
        'sub': 'Customise what users and staff see.',
        'groups': [
            {'title': 'Thread creation', 'fields': [
                F('thread_creation_title', 'string', 'Title', '', max=256),
                F('thread_creation_response', 'text', 'Response', 'Sent to the user when a thread opens.', max=2000),
                F('thread_creation_footer', 'string', 'Footer', '', max=2048),
                F('thread_creation_send_dm_embed', 'bool', 'Send the creation embed in DMs', ''),
            ]},
            {'title': 'Thread close', 'fields': [
                F('thread_close_title', 'string', 'Title', '', max=256),
                F('thread_close_response', 'text', 'Response', 'Sent to the user when staff close the thread.', max=2000),
                F('thread_self_close_response', 'text', 'Self-close response', 'Sent when the user closes their own thread.', max=2000),
                F('thread_close_footer', 'string', 'Footer', '', max=2048),
            ]},
            {'title': 'Thread menu', 'fields': [
                F('thread_creation_menu_enabled', 'bool', 'Enable the thread menu', 'Users pick an option before the thread is created.'),
                F('thread_creation_menu_embed_text', 'text', 'Menu text', '', max=2000),
                F('thread_creation_menu_dropdown_placeholder', 'string', 'Dropdown placeholder', '', max=100),
            ]},
            {'title': 'Colours', 'fields': [
                F('main_color', 'color', 'Main colour', ''),
                F('mod_color', 'color', 'Staff reply colour', ''),
                F('recipient_color', 'color', 'User message colour', ''),
                F('error_color', 'color', 'Error colour', ''),
            ]},
        ],
    },
    'automation': {
        'title': 'Automation',
        'sub': 'Auto-close and abuse protection.',
        'groups': [
            {'title': 'Auto-close', 'fields': [
                F('thread_auto_close', 'hours', 'Close inactive threads after (hours)', '0 disables auto-close.', min=0, max=8760),
                F('thread_auto_close_response', 'text', 'Auto-close message', '', max=2000),
            ]},
            {'title': 'Abuse protection', 'fields': [
                F('thread_cooldown', 'hours', 'Cooldown between threads (hours)', '0 disables the cooldown.', min=0, max=8760),
                F('account_age', 'hours', 'Minimum account age (hours)', '0 disables the check.', min=0, max=87600),
                F('guild_age', 'hours', 'Minimum time in server (hours)', '0 disables the check.', min=0, max=87600),
            ]},
        ],
    },
    'staff': {'title': 'Staff & permissions', 'sub': 'Who can use which commands.', 'custom': 'permissions'},
}
FIELDS = {f['key']: f for p in PAGES.values() for g in p.get('groups', []) for f in g['fields']}
EDITABLE_CONFIG_KEYS = set(FIELDS)

# OWNER is intentionally excluded: it grants bot-owner powers (e.g. eval).
PERMISSION_LEVELS = ('ADMINISTRATOR', 'MODERATOR', 'SUPPORTER')


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    global mongo, db
    if MONGODB_URI:
        mongo = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        db = mongo[MONGODB_DB_NAME]
        await mongo.admin.command('ping')
        await db.login_codes.create_index('expires_at', expireAfterSeconds=0)
        await db.sessions.create_index('expires_at', expireAfterSeconds=0)
        await db.dashboard_instances.create_index([('guild_id', 1), ('kind', 1)])
        await db.dashboard_changes.create_index('expires_at', expireAfterSeconds=0)
        await db.dashboard_changes.create_index([('guild_id', 1), ('status', 1), ('created_at', 1)])
        await db.dashboard_instance_commands.create_index([('guild_id', 1), ('status', 1), ('created_at', 1)])
        await db.dashboard_instance_commands.create_index('expires_at', expireAfterSeconds=0)
    yield
    if mongo:
        mongo.close()


app = FastAPI(title='SmartModmail API', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list({'https://sigmapaulalt.github.io', ALLOWED_ORIGIN}),
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allow_headers=['*'],
)


@app.get('/health')
async def health():
    return {'ok': True, 'service': 'smartmodmail-api', 'mongodb_configured': bool(MONGODB_URI)}


def configured():
    required = {'MONGODB_URI': MONGODB_URI, 'SESSION_SECRET': SESSION_SECRET, 'DISCORD_CLIENT_ID': DISCORD_CLIENT_ID,
                'DISCORD_CLIENT_SECRET': DISCORD_CLIENT_SECRET, 'DISCORD_REDIRECT_URI': DISCORD_REDIRECT_URI}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise HTTPException(503, 'Missing environment variables: ' + ', '.join(missing))
    if db is None:
        raise HTTPException(503, 'MongoDB is not connected')


def utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def iso(dt):
    dt = utc(dt)
    return dt.isoformat() if dt else None


async def json_body(request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, 'Request body must be valid JSON.')
    if not isinstance(body, dict):
        raise HTTPException(400, 'Request body must be a JSON object.')
    return body


def check_gid(guild_id):
    if not re.fullmatch(r'\d{5,25}', str(guild_id)):
        raise HTTPException(400, 'Invalid server ID.')
    return str(guild_id)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get('/auth/login')
async def auth_login():
    configured()
    state = signer.dumps({'nonce': secrets.token_urlsafe(24)})
    q = urlencode({'client_id': DISCORD_CLIENT_ID, 'redirect_uri': DISCORD_REDIRECT_URI, 'response_type': 'code',
                   'scope': 'identify guilds', 'state': state})
    return RedirectResponse('https://discord.com/oauth2/authorize?' + q)


@app.get('/auth/callback')
async def auth_callback(code: str = None, state: str = None):
    configured()
    if not code or not state:
        raise HTTPException(400, 'Missing OAuth code or state.')
    try:
        signer.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, 'Invalid or expired OAuth state.')
    async with httpx.AsyncClient(timeout=20) as c:
        tr = await c.post('https://discord.com/api/oauth2/token',
                          data={'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
                                'grant_type': 'authorization_code', 'code': code, 'redirect_uri': DISCORD_REDIRECT_URI},
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if tr.status_code != 200:
            raise HTTPException(400, 'Discord token exchange failed.')
        td = tr.json()
        ur = await c.get('https://discord.com/api/users/@me', headers={'Authorization': 'Bearer ' + td['access_token']})
        if ur.status_code != 200:
            raise HTTPException(400, 'Could not retrieve Discord profile.')
        user = ur.json()
    one_time = secrets.token_urlsafe(32)
    await db.login_codes.insert_one({
        '_id': one_time,
        'user': {'id': user['id'], 'username': user.get('username'), 'global_name': user.get('global_name'), 'avatar': user.get('avatar')},
        'access_token': td['access_token'],
        'expires_at': datetime.now(timezone.utc) + timedelta(minutes=2),
    })
    return RedirectResponse(FRONTEND_URL + '?login_code=' + one_time)


@app.post('/auth/exchange')
async def exchange(request: Request):
    configured()
    body = await json_body(request)
    code = body.get('code')
    if not code:
        raise HTTPException(400, 'Missing login code.')
    record = await db.login_codes.find_one_and_delete({'_id': code})
    if not record:
        raise HTTPException(400, 'Login code is invalid or expired.')
    if utc(record['expires_at']) < datetime.now(timezone.utc):
        raise HTTPException(400, 'Login code is invalid or expired.')
    token = secrets.token_urlsafe(48)
    await db.sessions.insert_one({'_id': token, 'user': record['user'], 'access_token': record['access_token'],
                                  'expires_at': datetime.now(timezone.utc) + timedelta(days=7)})
    return {'token': token, 'user': record['user']}


async def session_for(request):
    token = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not token or db is None:
        raise HTTPException(401, 'Not authenticated.')
    s = await db.sessions.find_one({'_id': token, 'expires_at': {'$gt': datetime.now(timezone.utc)}})
    if not s:
        raise HTTPException(401, 'Session expired. Sign in again.')
    return s


_guild_cache = {}  # session token -> (timestamp, guilds)


async def discord_guilds(s):
    """Return all Discord servers the signed-in user belongs to (not just owned servers)."""
    cached = _guild_cache.get(s['_id'])
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get('https://discord.com/api/users/@me/guilds', headers={'Authorization': 'Bearer ' + s['access_token']})
    if r.status_code != 200:
        raise HTTPException(502, 'Discord server lookup failed.')
    guilds = r.json()
    if len(_guild_cache) > 500:
        _guild_cache.clear()
    _guild_cache[s['_id']] = (time.time(), guilds)
    return guilds


def configured_owner_ids(doc):
    """Mirror the bot's user-ID based OWNER checks without exposing config to clients."""
    ids = set()
    raw_owners = doc.get('owners')
    if isinstance(raw_owners, (list, tuple, set)):
        values = raw_owners
    elif raw_owners is None:
        values = []
    else:
        values = str(raw_owners).split(',')
    for value in values:
        value = str(value).strip()
        if value.isdigit():
            ids.add(value)
    # The bot's OWNER permission list is treated as Discord user IDs by bot_owner_ids.
    levels = doc.get('level_permissions') or {}
    raw_level_owners = levels.get('OWNER', []) if isinstance(levels, dict) else []
    if not isinstance(raw_level_owners, (list, tuple, set)):
        raw_level_owners = [raw_level_owners]
    for value in raw_level_owners:
        value = str(value).strip()
        if value.isdigit():
            ids.add(value)
    # Include owner_id when the bot config document explicitly stores it.
    owner_id = doc.get('owner_id')
    if owner_id is not None and str(owner_id).isdigit():
        ids.add(str(owner_id))
    return ids


def has_discord_administrator(guild):
    """Discord's guild-list permissions field is a decimal permission bitfield."""
    try:
        return bool(int(str(guild.get('permissions', '0'))) & (1 << 3))
    except (TypeError, ValueError):
        return False


async def require_access(s, guild_id):
    """Allow the Discord owner/admin or a configured Modmail OWNER user."""
    gid = str(guild_id)
    guild = next((g for g in await discord_guilds(s) if str(g.get('id')) == gid), None)
    if guild is None:
        raise HTTPException(403, 'You must be a member of this Discord server.')
    if guild.get('owner') is True:
        return {**guild, 'access_level': 'SERVER_OWNER', 'access_source': 'discord_owner'}
    if has_discord_administrator(guild):
        return {**guild, 'access_level': 'DISCORD_ADMINISTRATOR', 'access_source': 'discord_administrator'}
    _, doc = await config_doc(gid, required=False)
    user_id = str(s.get('user', {}).get('id', ''))
    if user_id and user_id in configured_owner_ids(doc):
        return {**guild, 'access_level': 'OWNER', 'access_source': 'modmail_owner'}
    raise HTTPException(403, 'You must own this Discord server, have Discord Administrator permission, or have Modmail OWNER permission.')


async def accessible_guilds(s):
    """List registered instances the user owns or can manage as a Modmail OWNER."""
    member_guilds = await discord_guilds(s)
    guild_ids = [str(g.get('id')) for g in member_guilds if g.get('id')]
    if not guild_ids:
        return []
    instances = await db.dashboard_instances.find({
        'kind': 'instance', 'guild_id': {'$in': guild_ids}
    }).to_list(length=500)
    by_guild = {str(inst.get('guild_id')): inst for inst in instances}
    user_id = str(s.get('user', {}).get('id', ''))
    result = []
    for guild in member_guilds:
        gid = str(guild.get('id', ''))
        inst = by_guild.get(gid)
        if not inst or not inst.get('bot_id'):
            continue
        if guild.get('owner') is True:
            result.append({**guild, 'access_level': 'SERVER_OWNER', 'access_source': 'discord_owner'})
            continue
        if has_discord_administrator(guild):
            result.append({**guild, 'access_level': 'DISCORD_ADMINISTRATOR', 'access_source': 'discord_administrator'})
            continue
        bot_id = str(inst['bot_id'])
        candidates = [bot_id]
        try:
            candidates.append(int(bot_id))
        except ValueError:
            pass
        doc = await db.config.find_one({'bot_id': {'$in': candidates}}) or {}
        if user_id and user_id in configured_owner_ids(doc):
            result.append({**guild, 'access_level': 'OWNER', 'access_source': 'modmail_owner'})
    return result


# ---------------------------------------------------------------------------
# Instance / config helpers
# ---------------------------------------------------------------------------
async def instance_for(guild_id):
    inst = await db.dashboard_instances.find_one({'guild_id': str(guild_id), 'kind': 'instance'})
    if not inst or not inst.get('bot_id'):
        raise HTTPException(404, 'No running Modmail instance is registered for this server yet.')
    return inst


def is_online(inst):
    seen = utc(inst.get('last_seen'))
    return bool(seen and datetime.now(timezone.utc) - seen < ONLINE_WINDOW and inst.get('status', 'online') == 'online')


async def config_doc(guild_id, required=True):
    inst = await instance_for(guild_id)
    bot_id = str(inst['bot_id'])
    candidates = [bot_id]
    try:
        candidates.append(int(bot_id))
    except ValueError:
        pass
    doc = await db.config.find_one({'bot_id': {'$in': candidates}})
    if doc is None:
        if required:
            raise HTTPException(404, 'No Modmail configuration found for this bot yet.')
        doc = {}
    return inst, doc


async def enqueue(s, guild_id, kind, payload):
    inst = await instance_for(guild_id)
    pending = await db.dashboard_changes.count_documents({'guild_id': str(guild_id), 'status': 'pending'})
    if pending >= MAX_PENDING_CHANGES:
        raise HTTPException(429, 'Too many pending changes. Is the bot online?')
    now = datetime.now(timezone.utc)
    res = await db.dashboard_changes.insert_one({
        'guild_id': str(guild_id), 'bot_id': str(inst['bot_id']), 'kind': kind, 'payload': payload,
        'status': 'pending', 'created_at': now, 'expires_at': now + CHANGE_TTL,
        'requested_by': {'id': s['user']['id'], 'username': s['user'].get('username')},
    })
    return str(res.inserted_id), is_online(inst)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def clean_setting(key, value):
    """Return ('set', stored_value) or ('reset', None). Raises HTTPException on bad input."""
    f = FIELDS[key]
    t = f['type']

    def bad(msg):
        raise HTTPException(400, f"{f['label']}: {msg}")

    if t == 'bool':
        if not isinstance(value, bool):
            bad('must be true or false.')
        return 'set', value

    if t in ('string', 'text', 'id', 'color'):
        if not isinstance(value, str):
            bad('must be text.')
        value = value.strip()
        if not value:
            return 'reset', None
        if t == 'id':
            if not re.fullmatch(r'\d{15,25}', value):
                bad('must be a Discord ID (15-25 digits).')
            return 'set', value
        if t == 'color':
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                bad('must be a hex colour like #5865f2.')
            return 'set', value.lower()
        if len(value) > f.get('max', 2000):
            bad(f"must be at most {f.get('max', 2000)} characters.")
        if f.get('pattern') and not re.fullmatch(f['pattern'], value):
            bad('has an invalid format.')
        return 'set', value

    if t in ('int', 'hours'):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            bad('must be a number.')
        if value != value or value in (float('inf'), float('-inf')):
            bad('must be a finite number.')
        if value < f.get('min', 0) or value > f.get('max', 1_000_000):
            bad(f"must be between {f.get('min', 0)} and {f.get('max', 1_000_000)}.")
        if t == 'int':
            if int(value) != value:
                bad('must be a whole number.')
            return 'set', int(value)
        if value == 0:
            return 'reset', None
        seconds = max(1, round(value * 3600))
        return 'set', isodate.duration_isoformat(timedelta(seconds=seconds))

    bad('unsupported setting type.')


def clean_snippet_name(name):
    name = str(name or '').strip().lower()
    if not name or len(name) > 50 or '.' in name or '$' in name:
        raise HTTPException(400, 'Snippet name must be 1-50 characters and cannot contain . or $.')
    return name


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get('/api/schema')
async def schema():
    return {'pages': PAGES, 'permission_levels': list(PERMISSION_LEVELS)}


@app.get('/api/me')
async def me(request: Request):
    return {'user': (await session_for(request))['user']}


@app.get('/api/guilds')
async def guilds(request: Request):
    return {'guilds': await accessible_guilds(await session_for(request))}


def instance_view(d, names=None):
    names = names or {}
    return {
        'guild_id': str(d.get('guild_id')),
        'guild_name': d.get('guild_name') or names.get(str(d.get('guild_id'))),
        'bot_id': d.get('bot_id'), 'bot_name': d.get('bot_name'), 'avatar_url': d.get('avatar_url'),
        'status': 'online' if is_online(d) else 'offline',
        'last_seen': iso(d.get('last_seen')),
        'version': d.get('version'), 'open_threads': d.get('open_threads'),
        'latency_ms': d.get('latency_ms'), 'uptime_seconds': d.get('uptime_seconds'),
        'presence_status': d.get('presence_status', 'online'), 'activity_type': d.get('activity_type', 'watching'),
        'activity_text': d.get('activity_text', ''),
    }


@app.get('/api/instances')
async def instances(request: Request):
    s = await session_for(request)
    owned = await accessible_guilds(s)
    ids = [g['id'] for g in owned]
    docs = await db.dashboard_instances.find({'kind': 'instance', 'guild_id': {'$in': ids}}).to_list(length=200)
    names = {g['id']: g['name'] for g in owned}
    return {'instances': [instance_view(d, names) for d in docs]}


@app.post('/api/instance-control/{guild_id}')
async def instance_control(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    body = await json_body(request)
    action = str(body.get('action', '')).lower()
    if action in ('start', 'stop', 'restart'):
        inst = await instance_for(gid)
        now = datetime.now(timezone.utc)
        res = await db.dashboard_instance_commands.insert_one({
            'guild_id': gid, 'bot_id': str(inst['bot_id']), 'action': action,
            'status': 'pending', 'created_at': now, 'expires_at': now + timedelta(minutes=5),
            'requested_by': {'id': s['user']['id'], 'username': s['user'].get('username')},
        })
        await db.dashboard_audit.insert_one({'guild_id': gid, 'action': action,
            'requested_by': s['user']['id'], 'created_at': now})
        return {'ok': True, 'command_id': str(res.inserted_id), 'status': 'pending'}
    if action == 'presence':
        status_name = str(body.get('status', 'online')).lower()
        activity_type = str(body.get('activity_type', 'watching')).lower()
        activity_text = body.get('activity_text', '')
        if status_name not in ('online', 'idle', 'dnd', 'invisible'):
            raise HTTPException(400, 'Status must be online, idle, dnd, or invisible.')
        if activity_type not in ('playing', 'watching', 'listening', 'competing'):
            raise HTTPException(400, 'Invalid activity type.')
        if not isinstance(activity_text, str) or len(activity_text) > 128:
            raise HTTPException(400, 'Activity text must be 0-128 characters.')
        change_id, online = await enqueue(s, gid, 'presence', {
            'status': status_name, 'activity_type': activity_type, 'activity_text': activity_text.strip()
        })
        return {'ok': True, 'change_id': change_id, 'bot_online': online}
    raise HTTPException(400, 'Action must be start, stop, restart, or presence.')


@app.get('/api/instance-control/{guild_id}/{command_id}')
async def instance_control_status(guild_id: str, command_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    try:
        oid = ObjectId(command_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, 'Invalid command ID.')
    item = await db.dashboard_instance_commands.find_one({'_id': oid, 'guild_id': gid})
    if not item:
        raise HTTPException(404, 'Command not found.')
    return {'status': item.get('status'), 'error': item.get('error')}


@app.get('/api/status/{guild_id}')
async def status(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    inst, doc = await config_doc(gid, required=False)
    staff = set()
    for level in PERMISSION_LEVELS:
        for x in (doc.get('level_permissions') or {}).get(level, []):
            staff.add(str(x))
    out = instance_view(inst)
    out['staff_count'] = len(staff)
    return out


@app.get('/api/config/{guild_id}')
async def get_config(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    inst, doc = await config_doc(gid, required=False)
    effective = inst.get('effective_config')
    return {
        'values': {k: v for k, v in (effective or {}).items() if k in FIELDS},
        'customised': [k for k in FIELDS if k in doc],
        'bot_online': is_online(inst),
        'bot_reports_config': effective is not None,
    }


@app.put('/api/config/{guild_id}')
async def put_config(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    body = await json_body(request)
    raw_set = body.get('set') or {}
    raw_reset = body.get('reset') or []
    if not isinstance(raw_set, dict) or not isinstance(raw_reset, list):
        raise HTTPException(400, 'Expected {"set": {...}, "reset": [...]}.')
    to_set, to_reset = {}, set()
    for key, value in raw_set.items():
        if key not in FIELDS:
            raise HTTPException(400, 'Unsupported setting: ' + str(key)[:60])
        action, stored = clean_setting(key, value)
        if action == 'reset':
            to_reset.add(key)
        else:
            to_set[key] = stored
    for key in raw_reset:
        if not isinstance(key, str) or key not in FIELDS:
            raise HTTPException(400, 'Unsupported setting: ' + str(key)[:60])
        to_reset.add(key)
    overlap = set(to_set) & to_reset
    for key in overlap:
        del to_set[key]
    if not to_set and not to_reset:
        raise HTTPException(400, 'Provide at least one change.')
    change_id, online = await enqueue(s, gid, 'config', {'set': to_set, 'reset': sorted(to_reset)})
    return {'ok': True, 'change_id': change_id, 'bot_online': online}


@app.get('/api/changes/{guild_id}/{change_id}')
async def get_change(guild_id: str, change_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    try:
        oid = ObjectId(change_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, 'Invalid change ID.')
    d = await db.dashboard_changes.find_one({'_id': oid, 'guild_id': gid})
    if not d:
        raise HTTPException(404, 'Change not found.')
    return {'status': d.get('status'), 'error': d.get('error'), 'applied_at': iso(d.get('applied_at'))}


@app.get('/api/snippets/{guild_id}')
async def get_snippets(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    _, doc = await config_doc(gid, required=False)
    snippets = doc.get('snippets') or {}
    return {'snippets': snippets if isinstance(snippets, dict) else {}}


@app.put('/api/snippets/{guild_id}')
async def put_snippet(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    body = await json_body(request)
    name = clean_snippet_name(body.get('name'))
    content = body.get('content')
    if not isinstance(content, str) or not content.strip() or len(content) > 4000:
        raise HTTPException(400, 'Snippet content must be 1-4000 characters.')
    change_id, online = await enqueue(s, gid, 'snippet_set', {'name': name, 'content': content})
    return {'ok': True, 'change_id': change_id, 'bot_online': online}


@app.delete('/api/snippets/{guild_id}/{name}')
async def delete_snippet(guild_id: str, name: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    name = str(name).strip()
    if not name or len(name) > 50 or '$' in name:
        raise HTTPException(400, 'Invalid snippet name.')
    change_id, online = await enqueue(s, gid, 'snippet_delete', {'name': name})
    return {'ok': True, 'change_id': change_id, 'bot_online': online}


@app.get('/api/permissions/{guild_id}')
async def get_permissions(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    _, doc = await config_doc(gid, required=False)
    perms = doc.get('level_permissions') or {}
    return {'levels': {lvl: [str(x) for x in perms.get(lvl, [])] for lvl in PERMISSION_LEVELS}}


@app.post('/api/permissions/{guild_id}')
async def change_permission(guild_id: str, request: Request):
    gid = check_gid(guild_id)
    s = await session_for(request)
    await require_access(s, gid)
    body = await json_body(request)
    level = str(body.get('level', '')).upper()
    action = body.get('action')
    target = str(body.get('target_id', '')).strip()
    if level not in PERMISSION_LEVELS:
        raise HTTPException(400, 'Level must be one of: ' + ', '.join(PERMISSION_LEVELS))
    if action not in ('add', 'remove'):
        raise HTTPException(400, 'Action must be add or remove.')
    if not re.fullmatch(r'\d{15,25}', target):
        raise HTTPException(400, 'Enter a valid role or user ID (15-25 digits).')
    change_id, online = await enqueue(s, gid, 'permission', {'level': level, 'action': action, 'target_id': target})
    return {'ok': True, 'change_id': change_id, 'bot_online': online}


@app.post('/auth/logout')
async def logout(request: Request):
    token = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if token and db is not None:
        await db.sessions.delete_one({'_id': token})
        _guild_cache.pop(token, None)
    return {'ok': True}
