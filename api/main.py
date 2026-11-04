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
