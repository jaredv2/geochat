"""
GeoChat — location-based real-time discussion
=============================================
Required environment variables:
  DATABASE_URL           PostgreSQL connection string (Render provides this)
  DISCORD_CLIENT_ID      Discord OAuth app client ID
  DISCORD_CLIENT_SECRET  Discord OAuth app client secret
  DISCORD_REDIRECT_URI   e.g. https://yourdomain.com/callback
  SECRET_KEY             Flask session secret (auto-generated if omitted in dev)

Optional:
  ADMIN_DISCORD_ID       Your Discord user ID for admin panel access
  LIBRETRANSLATE_URL     LibreTranslate instance URL for message translation
  PORT                   HTTP port (default: 8000)

Deploy: See README.md for Render + UptimeRobot free deployment guide.
"""

import os, secrets, html, json, queue, threading, unicodedata, math, logging, sys
from datetime import datetime

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from flask import (Flask, render_template, request, redirect,
                   session, jsonify, g, Response, stream_with_context, abort)
import requests as _requests

# ── HTTP session (no retry — Discord rate-limit backoff makes callbacks slow) ─
_http = _requests.Session()
_http.headers.update({
    'User-Agent': 'GeoChat/1.0 (https://geochat.onrender.com)',
    'Accept': 'application/json',
})

app = Flask(__name__)

# Trust Render's HTTPS proxy — fixes url_for() and request.scheme behind reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s',
                    stream=sys.stdout)
log = logging.getLogger('geochat')

# ── Config ────────────────────────────────────────────────────────────────────
IS_PRODUCTION         = os.environ.get('RENDER', '') == 'true' or os.environ.get('PRODUCTION', '') == '1'
PORT                  = int(os.environ.get('PORT', 8000))
DATABASE_URL          = os.environ.get('DATABASE_URL', '')
DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', f'http://localhost:{PORT}/callback')
# On Render, the proxy terminates TLS — ensure redirect URI is always https://
if IS_PRODUCTION and DISCORD_REDIRECT_URI.startswith('http://'):
    DISCORD_REDIRECT_URI = 'https://' + DISCORD_REDIRECT_URI[7:]
DISCORD_API           = 'https://discord.com/api/v10'
ADMIN_DISCORD_ID      = os.environ.get('ADMIN_DISCORD_ID', '')
LIBRETRANSLATE_URL    = os.environ.get('LIBRETRANSLATE_URL', '')
DISCORD_PROXY_URL     = os.environ.get('DISCORD_PROXY_URL', '')   # Cloudflare Worker proxy
DISCORD_PROXY_SECRET  = os.environ.get('DISCORD_PROXY_SECRET', '') # shared secret
ONLINE_WINDOW_SECS    = 120

# Render supplies postgres:// — psycopg2 needs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

# ── Secret key ────────────────────────────────────────────────────────────────
_env_key = os.environ.get('SECRET_KEY', '')
if _env_key:
    app.secret_key = _env_key
else:
    _kf = os.path.join(os.path.dirname(__file__), '.secret_key')
    if os.path.exists(_kf):
        with open(_kf) as f: app.secret_key = f.read().strip()
    else:
        _k = secrets.token_hex(32)
        try: open(_kf, 'w').write(_k)
        except OSError: pass
        app.secret_key = _k
    print("WARNING: SECRET_KEY not set — sessions will reset on restart", file=sys.stderr)

app.config.update(
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

# ── Connection pool ───────────────────────────────────────────────────────────
_pool: ThreadedConnectionPool | None = None

def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool

def get_db():
    if 'db' not in g:
        g.db = get_pool().getconn()
        g.db.autocommit = False
    return g.db

def cur(db=None):
    """Return a RealDictCursor."""
    return (db or get_db()).cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def q1(sql, params=()):
    """Execute, return first row as dict or None."""
    c = cur(); c.execute(sql, params); return c.fetchone()

def qall(sql, params=()):
    """Execute, return all rows as list of dicts."""
    c = cur(); c.execute(sql, params); return c.fetchall() or []

def qval(sql, params=()):
    """Execute, return first column of first row."""
    c = cur(); c.execute(sql, params)
    row = c.fetchone()
    if row is None: return None
    return list(row.values())[0]

def execute(sql, params=()):
    """Execute DML, return cursor."""
    c = cur(); c.execute(sql, params); return c

def commit():
    get_db().commit()

def rollback():
    try: get_db().rollback()
    except Exception: pass

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        try: get_pool().putconn(db)
        except Exception: pass

# ── Schema init ───────────────────────────────────────────────────────────────
def init_db():
    with app.app_context():
        db = get_db()
        schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
        with open(schema_path) as f:
            sql = f.read()
        c = db.cursor()
        for stmt in sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                try:
                    c.execute(stmt)
                except Exception as e:
                    db.rollback()
                    log.warning("Schema (non-fatal): %s", e)
        db.commit()
        log.info("Database schema ready")

# ── SSE broker ────────────────────────────────────────────────────────────────
class SSEBroker:
    def __init__(self):
        self.listeners: dict[int, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, lid: int) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with self._lock:
            self.listeners.setdefault(lid, []).append(q)
        return q

    def unsubscribe(self, lid: int, q: queue.Queue):
        with self._lock:
            lst = self.listeners.get(lid, [])
            if q in lst: lst.remove(q)

    def publish(self, lid: int, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        with self._lock:
            dead = []
            for q in self.listeners.get(lid, []):
                try: q.put_nowait(payload)
                except queue.Full: dead.append(q)
            for q in dead:
                self.listeners[lid].remove(q)

broker = SSEBroker()

# ── Background cleanup: delete orphan markers every 2 min ────────────────────
def _cleanup_loop():
    import time
    _log = logging.getLogger('geochat.cleanup')
    while True:
        time.sleep(120)
        try:
            with app.app_context():
                rows = qall("""SELECT id FROM locations
                               WHERE message_count = 0
                               AND created_at < NOW() - INTERVAL '5 minutes'""")
                for row in rows:
                    lid = row['id']
                    real = qval("SELECT COUNT(*) FROM messages WHERE location_id=%s", (lid,))
                    if real == 0:
                        execute("DELETE FROM online_presence WHERE location_id=%s", (lid,))
                        execute("DELETE FROM locations WHERE id=%s", (lid,))
                if rows:
                    commit()
                    _log.info("Removed %d orphan location(s)", len(rows))
                execute("DELETE FROM online_presence WHERE last_seen < NOW() - INTERVAL '10 minutes'")
                execute("DELETE FROM oauth_states WHERE created_at < NOW() - INTERVAL '10 minutes'")
                commit()
        except Exception as e:
            _log.error("Cleanup error: %s", e)
            try: rollback()
            except Exception: pass

threading.Thread(target=_cleanup_loop, daemon=True).start()

# ── Rate limiting ─────────────────────────────────────────────────────────────
RATE_LIMITS = {
    'post_message': (5, 60),
    'vote':         (30, 60),
    'react':        (20, 60),
    'report':       (3, 300),
}

def check_rate_limit(user_id, action):
    limit, window = RATE_LIMITS.get(action, (10, 60))
    execute("""DELETE FROM rate_limits
               WHERE action=%s AND created_at < NOW() - (%s || ' seconds')::INTERVAL""",
            (action, str(window)))
    count = qval("SELECT COUNT(*) FROM rate_limits WHERE user_id=%s AND action=%s", (user_id, action))
    if (count or 0) >= limit: return False
    execute("INSERT INTO rate_limits (user_id,action) VALUES (%s,%s)", (user_id, action))
    commit()
    return True

# ── Badges ────────────────────────────────────────────────────────────────────
BADGE_DEFS = {
    'first_post':   {'label': 'First Word',    'icon': '✍',  'desc': 'Posted your first message'},
    'explorer':     {'label': 'Explorer',       'icon': '🗺',  'desc': 'Posted in 5 different locations'},
    'popular':      {'label': 'Popular',        'icon': '⭐',  'desc': 'Received 10 upvotes'},
    'veteran':      {'label': 'Veteran',        'icon': '🏅',  'desc': '25 messages posted'},
    'globe':        {'label': 'Globe Trotter',  'icon': '🌍',  'desc': 'Posted in 20 locations'},
    'loved':        {'label': 'Beloved',        'icon': '❤',  'desc': '50 upvotes total'},
    'centurion':    {'label': 'Centurion',      'icon': '💯',  'desc': '100 messages posted'},
    'debater':      {'label': 'Debater',        'icon': '⚔',  'desc': 'Left 20 replies'},
    'reactor':      {'label': 'Reactor',        'icon': '⚡',  'desc': 'Sent 30 reactions'},
    'cartographer': {'label': 'Cartographer',   'icon': '📐',  'desc': 'Posted in 50 locations'},
    'influencer':   {'label': 'Influencer',     'icon': '📣',  'desc': '200 upvotes total'},
    'night_owl':    {'label': 'Night Owl',      'icon': '🦉',  'desc': 'Posted between midnight and 4 AM'},
    'early_bird':   {'label': 'Early Bird',     'icon': '🌅',  'desc': 'Posted between 5 AM and 7 AM'},
}

# ── User helpers ──────────────────────────────────────────────────────────────
def current_user():
    if 'user_id' not in session: return None
    row = q1("SELECT id,username,avatar_url,is_admin,is_banned FROM users WHERE id=%s",
             (session['user_id'],))
    if not row: session.clear(); return None
    if row['is_banned']: session.clear(); return None
    return dict(row)

def check_banned():
    if 'user_id' not in session: return
    row = q1("SELECT is_banned,ban_reason FROM users WHERE id=%s", (session['user_id'],))
    if row and row['is_banned']:
        reason = row['ban_reason'] or 'No reason given.'
        session.clear()
        abort(Response(render_template('banned.html', reason=reason), status=403))

def get_online_count(lid):
    return qval("""SELECT COUNT(DISTINCT user_id) FROM online_presence
                   WHERE location_id=%s AND last_seen > NOW() - (%s || ' seconds')::INTERVAL""",
                (lid, str(ONLINE_WINDOW_SECS))) or 0

def touch_presence(uid, lid):
    if not uid: return
    try:
        execute("""INSERT INTO online_presence (user_id,location_id,last_seen)
                   VALUES (%s,%s,NOW())
                   ON CONFLICT(user_id,location_id) DO UPDATE SET last_seen=NOW()""", (uid, lid))
        commit()
    except Exception: rollback()

def get_reactions(mid, uid=None):
    rows = qall("SELECT emoji, COUNT(*) as cnt FROM reactions WHERE message_id=%s GROUP BY emoji ORDER BY cnt DESC", (mid,))
    mine = set()
    if uid:
        mine = {r['emoji'] for r in qall("SELECT emoji FROM reactions WHERE message_id=%s AND user_id=%s", (mid, uid))}
    return [{'emoji': r['emoji'], 'count': r['cnt'], 'reacted': r['emoji'] in mine} for r in rows]

def fmt_msg(row, uid=None, replies=None):
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, 'isoformat'): d[k] = v.isoformat()
    d['replies']   = replies or []
    d['reactions'] = get_reactions(d['id'], uid)
    d['user_vote'] = 0
    if uid:
        v = q1("SELECT value FROM votes WHERE message_id=%s AND user_id=%s", (d['id'], uid))
        if v: d['user_vote'] = v['value']
    return d

def award_badges(user_id):
    u = q1("SELECT post_count FROM users WHERE id=%s", (user_id,))
    if not u: return []
    earned = {r['badge'] for r in qall("SELECT badge FROM badges WHERE user_id=%s", (user_id,))}
    new_badges = []

    def give(badge):
        if badge not in earned:
            execute("INSERT INTO badges (user_id,badge) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, badge))
            new_badges.append(badge)

    pc = u['post_count']
    if pc >= 1:   give('first_post')
    if pc >= 25:  give('veteran')
    if pc >= 100: give('centurion')
    locs = qval("SELECT COUNT(DISTINCT location_id) FROM messages WHERE user_id=%s AND parent_id IS NULL", (user_id,)) or 0
    if locs >= 5:  give('explorer')
    if locs >= 20: give('globe')
    if locs >= 50: give('cartographer')
    replies = qval("SELECT COUNT(*) FROM messages WHERE user_id=%s AND parent_id IS NOT NULL", (user_id,)) or 0
    if replies >= 20: give('debater')
    reactions = qval("SELECT COUNT(*) FROM reactions WHERE user_id=%s", (user_id,)) or 0
    if reactions >= 30: give('reactor')
    score = qval("SELECT COALESCE(SUM(score),0) FROM messages WHERE user_id=%s", (user_id,)) or 0
    if score >= 10:  give('popular')
    if score >= 50:  give('loved')
    if score >= 200: give('influencer')
    hour = datetime.utcnow().hour
    if 0 <= hour < 4:  give('night_owl')
    if 5 <= hour < 7:  give('early_bird')
    if new_badges: commit()
    return new_badges

# ── Translation helpers ───────────────────────────────────────────────────────
def transliterate_to_ascii(text):
    try:
        n = unicodedata.normalize('NFKD', text)
        a = n.encode('ascii', 'ignore').decode('ascii').strip()
        if a and len(a) >= max(1, len(text) // 3): return a
    except Exception: pass
    return text

def has_non_latin(text):
    for ch in text:
        if ch.isalpha():
            try:
                name = unicodedata.name(ch, '')
                if not any(s in name for s in ('LATIN', 'DIGIT', 'SPACE')): return True
            except Exception: pass
    return False

def translate_place_to_english(text):
    if not LIBRETRANSLATE_URL: return transliterate_to_ascii(text)
    try:
        res = _http.post(f"{LIBRETRANSLATE_URL}/translate",
                         json={'q': text, 'source': 'auto', 'target': 'en', 'format': 'text'},
                         timeout=3)
        if res.ok:
            t = res.json().get('translatedText', '').strip()
            if t and t != text: return t
    except Exception: pass
    return transliterate_to_ascii(text)

# ── Auth error page ───────────────────────────────────────────────────────────
def _auth_error(title, detail=''):
    page = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Login Error — GeoChat</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"IBM Plex Sans",sans-serif;background:#f5f0e6;color:#1a1612;
display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}}
.box{{max-width:460px;width:100%;border:3px solid #1a1612;background:#faf7f2;
box-shadow:6px 6px 0 rgba(26,22,18,.25);padding:32px}}
h1{{font-family:"IBM Plex Mono",monospace;font-size:1rem;font-weight:600;
text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px;color:#c0392b}}
p{{font-size:.9rem;line-height:1.7;color:#4a4035;margin-bottom:20px}}
.hint{{font-family:"IBM Plex Mono",monospace;font-size:.72rem;color:#8a7d6e;
background:#ede7d5;border-left:3px solid #c0392b;padding:10px 12px;margin-bottom:24px}}
a.btn{{display:inline-block;background:#1a1612;color:#f5f0e6;padding:11px 22px;
font-family:"IBM Plex Mono",monospace;font-size:.8rem;font-weight:600;
text-decoration:none;letter-spacing:.06em}}
a.btn:hover{{background:#c0392b}}</style></head>
<body><div class="box"><h1>Login Error</h1><p>{title}</p>
{'<div class="hint">'+detail+'</div>' if detail else ''}
<a class="btn" href="/login">← Try Again</a></div></body></html>'''
    return Response(page, status=400, mimetype='text/html')

# ── OAuth ─────────────────────────────────────────────────────────────────────
@app.route('/login')
def login():
    state = secrets.token_urlsafe(24)
    execute("DELETE FROM oauth_states WHERE created_at < NOW() - INTERVAL '10 minutes'")
    execute("INSERT INTO oauth_states (state) VALUES (%s)", (state,))
    commit()
    from urllib.parse import urlencode
    p = urlencode({'client_id': DISCORD_CLIENT_ID, 'redirect_uri': DISCORD_REDIRECT_URI,
                   'response_type': 'code', 'scope': 'identify', 'state': state})
    return redirect(f"https://discord.com/oauth2/authorize?{p}")

@app.route('/callback')
def callback():
    code, state = request.args.get('code'), request.args.get('state')
    if not code or not state:
        return _auth_error("Login link expired or invalid.", "Try logging in again.")

    # Atomic check-and-delete: if state doesn't exist RETURNING returns nothing
    deleted = execute(
        "DELETE FROM oauth_states WHERE state=%s RETURNING id", (state,)
    ).fetchone()
    commit()
    if not deleted:
        return _auth_error("Invalid or expired login state.",
                           "This can happen if you opened multiple login tabs or waited too long. Please try again.")

    # Token exchange — route via Cloudflare Worker proxy if configured
    # (avoids Render shared-IP rate limits on Discord's Cloudflare)
    token_url = f"{DISCORD_PROXY_URL}/token" if DISCORD_PROXY_URL else f"{DISCORD_API}/oauth2/token"
    token_headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if DISCORD_PROXY_URL and DISCORD_PROXY_SECRET:
        token_headers['X-Proxy-Secret'] = DISCORD_PROXY_SECRET
    try:
        tr = _http.post(token_url, data={
            'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': DISCORD_REDIRECT_URI,
        }, headers=token_headers, timeout=8)
    except Exception as e:
        log.error("Discord token exchange network error: %s", e)
        return _auth_error("Could not reach Discord.", "Please try again.")

    # Log full Discord response for debugging
    log.info("Discord token response: status=%s body=%s", tr.status_code,
             tr.text[:300] if not tr.ok else 'OK')

    if tr.status_code == 429:
        retry_after = tr.headers.get('Retry-After', '30')
        return _auth_error("Discord is rate-limiting this server.",
                           f"Please wait {retry_after} seconds and try again. "
                           f"This is a shared-IP issue with free hosting.")
    if not tr.ok:
        try:
            err = tr.json().get('error_description') or tr.json().get('error') or tr.text[:200]
        except Exception:
            err = tr.text[:200]
        return _auth_error("Discord login failed.", f"{tr.status_code}: {err}")

    access_token = tr.json().get('access_token')
    if not access_token:
        return _auth_error("No access token returned.", "Please try logging in again.")

    # Also proxy the /users/@me call through Deno to avoid Render IP blocks
    me_url = f"{DISCORD_PROXY_URL}/me" if DISCORD_PROXY_URL else f"{DISCORD_API}/users/@me"
    me_headers = {'Authorization': f"Bearer {access_token}"}
    if DISCORD_PROXY_URL and DISCORD_PROXY_SECRET:
        me_headers['X-Proxy-Secret'] = DISCORD_PROXY_SECRET
    try:
        ur = _http.get(me_url, headers=me_headers, timeout=6)
    except Exception:
        return _auth_error("Could not fetch your Discord profile.", "Please try again.")
    if not ur.ok:
        return _auth_error("Discord profile fetch failed.", f"Status: {ur.status_code}")

    u        = ur.json()
    did      = u['id']
    username = u['username']
    avatar   = (f"https://cdn.discordapp.com/avatars/{did}/{u['avatar']}.png"
                if u.get('avatar') else
                f"https://cdn.discordapp.com/embed/avatars/{int(did) % 5}.png")
    is_admin = 1 if (ADMIN_DISCORD_ID and did == ADMIN_DISCORD_ID) else 0

    execute("""INSERT INTO users (discord_id,username,avatar_url,is_admin)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT(discord_id) DO UPDATE SET
                 username=EXCLUDED.username, avatar_url=EXCLUDED.avatar_url,
                 is_admin=GREATEST(users.is_admin, EXCLUDED.is_admin)""",
            (did, username, avatar, is_admin))
    commit()

    row = q1("SELECT id,is_admin,is_banned,ban_reason FROM users WHERE discord_id=%s", (did,))
    if row and row['is_banned']:
        return Response(render_template('banned.html',
                                        reason=row['ban_reason'] or 'No reason given.'), status=403)
    session.update(user_id=row['id'], username=username, avatar_url=avatar,
                   is_admin=row['is_admin'])
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    check_banned()
    return render_template('index.html', user=current_user())

@app.route('/profile')
@app.route('/profile/<int:uid>')
def profile(uid=None):
    if uid is None:
        u = current_user()
        if not u: return redirect('/login')
        uid = u['id']
    pu = q1("SELECT id,username,avatar_url,post_count,created_at FROM users WHERE id=%s", (uid,))
    if not pu: return "Not found", 404
    ub = qall("SELECT badge,earned_at FROM badges WHERE user_id=%s ORDER BY earned_at", (uid,))
    return render_template('profile.html', profile_user=dict(pu), current_user=current_user(),
                           badges=[dict(b) for b in ub], badge_defs=BADGE_DEFS)

@app.route('/leaderboard')
def leaderboard():
    top_locs  = qall("SELECT id,place_name,message_count,latitude,longitude FROM locations ORDER BY message_count DESC LIMIT 20")
    top_users = qall("""
        SELECT u.id,u.username,u.avatar_url,u.post_count,
               COALESCE(SUM(m.score),0) as total_score,
               COUNT(DISTINCT m.location_id) as loc_count
        FROM users u LEFT JOIN messages m ON m.user_id=u.id
        GROUP BY u.id ORDER BY total_score DESC LIMIT 20""")
    stats = {
        'total_users':     qval("SELECT COUNT(*) FROM users") or 0,
        'total_messages':  qval("SELECT COUNT(*) FROM messages WHERE hidden=0") or 0,
        'total_locations': qval("SELECT COUNT(*) FROM locations WHERE message_count>0") or 0,
        'online_users':    qval("""SELECT COUNT(DISTINCT user_id) FROM online_presence
                                   WHERE last_seen > NOW() - (%s || ' seconds')::INTERVAL""",
                                (str(ONLINE_WINDOW_SECS),)) or 0,
    }
    return render_template('leaderboard.html',
                           top_locs=[dict(r) for r in top_locs],
                           top_users=[dict(r) for r in top_users],
                           stats=stats, current_user=current_user())

@app.route('/credits')
def credits():
    return render_template('credits.html', current_user=current_user())

@app.route('/admin')
def admin():
    u = current_user()
    if not u or not u['is_admin']: return "Forbidden", 403
    reports = qall("""
        SELECT r.id,r.reason,r.status,r.created_at,
               m.id as msg_id,m.content,m.hidden,
               rep.username as reporter,rep.avatar_url as reporter_avatar,
               au.id as author_id, au.username as author, l.place_name
        FROM reports r
        JOIN messages m   ON r.message_id=m.id
        JOIN users rep    ON r.reporter_id=rep.id
        JOIN users au     ON m.user_id=au.id
        JOIN locations l  ON m.location_id=l.id
        WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 50""")
    return render_template('admin.html', reports=[dict(r) for r in reports], current_user=u)

# ── SSE ───────────────────────────────────────────────────────────────────────
@app.route('/api/stream/<int:lid>')
def stream(lid):
    u   = current_user()
    uid = u['id'] if u else None
    if uid: touch_presence(uid, lid)
    q   = broker.subscribe(lid)
    def generate():
        yield "data: connected\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=20)
                except queue.Empty:
                    if uid: touch_presence(uid, lid)
                    count = get_online_count(lid)
                    yield f"event: online_count\ndata: {json.dumps({'count': count})}\n\n"
        except GeneratorExit:
            broker.unsubscribe(lid, q)
    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── API: stats ────────────────────────────────────────────────────────────────
@app.route('/api/stats')
def global_stats():
    return jsonify({
        'total_users':     qval("SELECT COUNT(*) FROM users") or 0,
        'total_messages':  qval("SELECT COUNT(*) FROM messages WHERE hidden=0") or 0,
        'total_locations': qval("SELECT COUNT(*) FROM locations WHERE message_count>0") or 0,
        'online_users':    qval("""SELECT COUNT(DISTINCT user_id) FROM online_presence
                                   WHERE last_seen > NOW() - (%s || ' seconds')::INTERVAL""",
                                (str(ONLINE_WINDOW_SECS),)) or 0,
    })

# ── API: locations ────────────────────────────────────────────────────────────
@app.route('/api/locations/nearby')
def nearby():
    try:
        lat  = float(request.args['lat']); lng = float(request.args['lng'])
        dlat = float(request.args.get('dlat', 1.0))
        dlng = float(request.args.get('dlng', 1.0))
        radius_km    = float(request.args.get('radius', 0))
        requester_id = request.args.get('uid', type=int)
    except (KeyError, ValueError): return jsonify({'error': 'invalid params'}), 400

    rows = qall("""
        SELECT id,latitude,longitude,place_name,message_count,last_user_avatar,last_user_id,top_content
        FROM locations
        WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
        ORDER BY message_count DESC LIMIT 300
    """, (lat-dlat, lat+dlat, lng-dlng, lng+dlng))

    results = []
    for r in rows:
        if r['message_count'] == 0 and r['last_user_id'] != requester_id:
            continue
        if radius_km > 0:
            dlat2 = math.radians(r['latitude'] - lat)
            dlng2 = math.radians(r['longitude'] - lng)
            a = (math.sin(dlat2/2)**2 +
                 math.cos(math.radians(lat)) * math.cos(math.radians(r['latitude'])) * math.sin(dlng2/2)**2)
            if 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)) > radius_km: continue
        results.append(dict(r))
    return jsonify(results)

@app.route('/api/locations/heatmap')
def heatmap():
    rows = qall("SELECT latitude,longitude,message_count FROM locations WHERE message_count>0 ORDER BY message_count DESC LIMIT 500")
    return jsonify([dict(r) for r in rows])

@app.route('/api/location', methods=['POST'])
def create_location():
    u    = current_user()
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data['latitude']); lng = float(data['longitude'])
    except (KeyError, ValueError, TypeError): return jsonify({'error': 'Invalid coords'}), 400
    place = html.escape(str(data.get('place_name', 'Unknown'))[:200])

    ex = q1("""SELECT id,place_name,message_count,last_user_avatar,top_content FROM locations
               WHERE ABS(latitude-%s) < 0.0001 AND ABS(longitude-%s) < 0.0001 LIMIT 1""",
            (lat, lng))
    if ex: return jsonify(dict(ex))

    uid_safe = avatar_safe = None
    if u:
        row = q1("SELECT id,avatar_url FROM users WHERE id=%s", (u['id'],))
        if row: uid_safe = row['id']; avatar_safe = row['avatar_url']

    c      = execute("INSERT INTO locations (latitude,longitude,place_name,last_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
                     (lat, lng, place, uid_safe))
    new_id = c.fetchone()['id']
    commit()
    return jsonify({'id': new_id, 'place_name': place, 'message_count': 0,
                    'last_user_avatar': avatar_safe, 'top_content': None}), 201

@app.route('/api/location/<int:lid>')
def get_location(lid):
    row = q1("SELECT * FROM locations WHERE id=%s", (lid,))
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))

@app.route('/api/location/<int:lid>', methods=['DELETE'])
def delete_empty_location(lid):
    u   = current_user()
    row = q1("SELECT message_count,last_user_id FROM locations WHERE id=%s", (lid,))
    if not row: return jsonify({'ok': True})
    if row['message_count'] > 0: return jsonify({'error': 'Has messages'}), 400
    if u and row['last_user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    execute("DELETE FROM locations WHERE id=%s AND message_count=0", (lid,))
    commit()
    return jsonify({'ok': True})

@app.route('/api/location/<int:lid>/online')
def location_online(lid):
    return jsonify({'count': get_online_count(lid)})

@app.route('/api/search')
def search():
    q = request.args.get('q','').strip()
    if len(q) < 2: return jsonify([])
    rows = qall("SELECT id,latitude,longitude,place_name,message_count FROM locations WHERE place_name ILIKE %s ORDER BY message_count DESC LIMIT 8",
                (f'%{q}%',))
    return jsonify([dict(r) for r in rows])

@app.route('/api/search/world')
def search_world():
    q = request.args.get('q','').strip()
    if len(q) < 2: return jsonify([])
    try:
        res = _http.get('https://nominatim.openstreetmap.org/search', params={
            'q': q, 'format': 'json', 'limit': 6, 'addressdetails': 1, 'accept-language': 'en',
        }, timeout=5)
        if not res.ok: return jsonify([])
        results = []
        for item in res.json():
            addr    = item.get('address', {})
            name    = (addr.get('tourism') or addr.get('amenity') or addr.get('building') or
                       addr.get('road') or addr.get('neighbourhood') or addr.get('suburb') or
                       addr.get('town') or addr.get('village') or addr.get('city') or
                       addr.get('county') or addr.get('state') or item.get('name',''))
            country = addr.get('country','')
            label   = f"{name}, {country}" if name and country and name != country else (name or item.get('display_name',''))
            if has_non_latin(label): label = translate_place_to_english(label)
            results.append({'label': label[:120], 'lat': float(item['lat']),
                            'lng': float(item['lon']), 'type': item.get('type','')})
        return jsonify(results)
    except Exception: return jsonify([])

@app.route('/api/place/translate', methods=['POST'])
def place_translate():
    data = request.get_json(silent=True) or {}
    name = data.get('name','').strip()[:200]
    if not name: return jsonify({'name': name})
    if not has_non_latin(name): return jsonify({'name': name, 'translated': False})
    english = translate_place_to_english(name)
    return jsonify({'name': english, 'translated': english != name})

# ── API: admin ────────────────────────────────────────────────────────────────
@app.route('/api/admin/ban/<int:target_uid>', methods=['POST'])
def ban_user(target_uid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json(silent=True) or {}
    reason = html.escape(data.get('reason','No reason given.')[:200])
    row    = q1("SELECT is_admin FROM users WHERE id=%s", (target_uid,))
    if not row: return jsonify({'error': 'Not found'}), 404
    if row['is_admin']: return jsonify({'error': 'Cannot ban an admin'}), 400
    execute("UPDATE users SET is_banned=1, ban_reason=%s WHERE id=%s", (reason, target_uid))
    commit()
    return jsonify({'ok': True})

@app.route('/api/admin/unban/<int:target_uid>', methods=['POST'])
def unban_user(target_uid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE id=%s", (target_uid,))
    commit()
    return jsonify({'ok': True})

@app.route('/api/admin/users')
def admin_users():
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    q = request.args.get('q','').strip()
    if q:
        rows = qall("SELECT id,username,avatar_url,post_count,is_banned,ban_reason,created_at FROM users WHERE username ILIKE %s AND is_admin=0 ORDER BY created_at DESC LIMIT 30",
                    (f'%{q}%',))
    else:
        rows = qall("SELECT id,username,avatar_url,post_count,is_banned,ban_reason,created_at FROM users WHERE is_admin=0 ORDER BY created_at DESC LIMIT 50")
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/report/<int:rid>', methods=['POST'])
def resolve_report(rid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json(silent=True) or {}
    action = data.get('action')
    r      = q1("SELECT message_id FROM reports WHERE id=%s", (rid,))
    if not r: return jsonify({'error': 'Not found'}), 404
    if action == 'hide':
        execute("UPDATE messages SET hidden=1 WHERE id=%s", (r['message_id'],))
    execute("UPDATE reports SET status='resolved',resolved_by=%s WHERE id=%s", (u['id'], rid))
    commit()
    return jsonify({'ok': True})

# ── API: messages ─────────────────────────────────────────────────────────────
@app.route('/api/messages/<int:lid>')
def get_messages(lid):
    u   = current_user(); uid = u['id'] if u else None
    if uid: touch_presence(uid, lid)
    if not q1("SELECT id FROM locations WHERE id=%s", (lid,)):
        return jsonify({'error': 'Not found'}), 404
    top = qall("""
        SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
               u.id as user_id,u.username,u.avatar_url
        FROM messages m JOIN users u ON m.user_id=u.id
        WHERE m.location_id=%s AND m.parent_id IS NULL AND m.hidden=0
        ORDER BY m.score DESC, m.created_at DESC LIMIT 100""", (lid,))
    result = []
    for msg in top:
        replies = qall("""
            SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
                   u.id as user_id,u.username,u.avatar_url
            FROM messages m JOIN users u ON m.user_id=u.id
            WHERE m.parent_id=%s AND m.hidden=0 ORDER BY m.created_at ASC LIMIT 50""",
            (msg['id'],))
        result.append(fmt_msg(msg, uid, [fmt_msg(r, uid) for r in replies]))
    return jsonify(result)

@app.route('/api/messages/user/<int:uid>')
def user_messages(uid):
    rows = qall("""
        SELECT m.id,m.content,m.score,m.edited,m.created_at,
               l.place_name,l.id as location_id,l.latitude,l.longitude
        FROM messages m JOIN locations l ON m.location_id=l.id
        WHERE m.user_id=%s AND m.parent_id IS NULL AND m.hidden=0
        ORDER BY m.created_at DESC LIMIT 50""", (uid,))
    return jsonify([dict(r) for r in rows])

@app.route('/api/message', methods=['POST'])
def post_message():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'post_message'):
        return jsonify({'error': 'Rate limit: 5 per minute'}), 429
    data      = request.get_json(silent=True) or {}
    lid       = data.get('location_id')
    content   = data.get('content', '').strip()
    parent_id = data.get('parent_id')
    if not lid or not content: return jsonify({'error': 'location_id and content required'}), 400
    if len(content) > 500: return jsonify({'error': 'Max 500 chars'}), 400
    content = html.escape(content)
    if not q1("SELECT id FROM locations WHERE id=%s", (lid,)):
        return jsonify({'error': 'Location not found'}), 404
    parent = None
    if parent_id:
        parent = q1("SELECT id,user_id,location_id FROM messages WHERE id=%s", (parent_id,))
        if not parent or parent['location_id'] != lid:
            return jsonify({'error': 'Invalid parent'}), 400

    c   = execute("INSERT INTO messages (location_id,user_id,content,parent_id) VALUES (%s,%s,%s,%s) RETURNING id",
                  (lid, u['id'], content, parent_id))
    mid = c.fetchone()['id']
    execute("UPDATE locations SET message_count=message_count+1,last_user_id=%s,last_user_avatar=%s WHERE id=%s",
            (u['id'], u['avatar_url'], lid))
    if not parent_id:
        execute("UPDATE locations SET top_content=%s WHERE id=%s", (content[:80], lid))
    execute("UPDATE users SET post_count=post_count+1 WHERE id=%s", (u['id'],))
    if parent and parent['user_id'] != u['id']:
        execute("INSERT INTO notifications (user_id,message_id,reply_id) VALUES (%s,%s,%s)",
                (parent['user_id'], parent_id, mid))
    commit()

    new_badges = award_badges(u['id'])
    row = q1("""SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
                       u.id as user_id,u.username,u.avatar_url
                FROM messages m JOIN users u ON m.user_id=u.id WHERE m.id=%s""", (mid,))
    msg_data = fmt_msg(row, u['id'])
    broker.publish(lid, 'new_message', msg_data)
    if new_badges:
        broker.publish(lid, 'badge_earned',
                       {'user_id': u['id'], 'username': u['username'],
                        'badges': [{'badge': b, **BADGE_DEFS[b]} for b in new_badges if b in BADGE_DEFS]})
    return jsonify({'message': msg_data, 'new_badges': new_badges}), 201

@app.route('/api/message/<int:mid>', methods=['PUT'])
def edit_message(mid):
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    data    = request.get_json(silent=True) or {}
    content = data.get('content', '').strip()
    if not content or len(content) > 500: return jsonify({'error': 'Invalid content'}), 400
    msg = q1("SELECT user_id,location_id FROM messages WHERE id=%s", (mid,))
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    execute("UPDATE messages SET content=%s,edited=1 WHERE id=%s", (html.escape(content), mid))
    commit()
    broker.publish(msg['location_id'], 'edit_message', {'id': mid, 'content': html.escape(content)})
    return jsonify({'ok': True})

@app.route('/api/message/<int:mid>', methods=['DELETE'])
def delete_message(mid):
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    msg = q1("SELECT user_id,location_id,parent_id FROM messages WHERE id=%s", (mid,))
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    lid = msg['location_id']

    child_ids = [r['id'] for r in qall("SELECT id FROM messages WHERE parent_id=%s", (mid,))]
    for cid in child_ids:
        execute("DELETE FROM votes WHERE message_id=%s", (cid,))
        execute("DELETE FROM reactions WHERE message_id=%s", (cid,))
        execute("DELETE FROM notifications WHERE message_id=%s OR reply_id=%s", (cid, cid))
        execute("DELETE FROM messages WHERE id=%s", (cid,))

    execute("DELETE FROM votes WHERE message_id=%s", (mid,))
    execute("DELETE FROM reactions WHERE message_id=%s", (mid,))
    execute("DELETE FROM notifications WHERE message_id=%s OR reply_id=%s", (mid, mid))
    execute("DELETE FROM messages WHERE id=%s", (mid,))

    location_deleted = False
    if not msg['parent_id']:
        execute("UPDATE locations SET message_count=GREATEST(0,message_count-1) WHERE id=%s", (lid,))
        remaining = qval("SELECT COUNT(*) FROM messages WHERE location_id=%s AND parent_id IS NULL AND hidden=0", (lid,))
        if (remaining or 0) == 0:
            execute("DELETE FROM online_presence WHERE location_id=%s", (lid,))
            execute("DELETE FROM locations WHERE id=%s", (lid,))
            location_deleted = True

    if not msg['parent_id'] and not location_deleted:
        best = q1("SELECT content FROM messages WHERE location_id=%s AND parent_id IS NULL AND hidden=0 ORDER BY score DESC, created_at DESC LIMIT 1", (lid,))
        execute("UPDATE locations SET top_content=%s WHERE id=%s",
                (best['content'][:80] if best else None, lid))
    commit()
    broker.publish(lid, 'delete_message', {'id': mid})
    if location_deleted:
        broker.publish(lid, 'location_deleted', {'location_id': lid})
    return jsonify({'ok': True, 'location_deleted': location_deleted, 'location_id': lid})

# ── API: votes ────────────────────────────────────────────────────────────────
@app.route('/api/vote', methods=['POST'])
def vote():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'vote'): return jsonify({'error': 'Rate limit'}), 429
    data  = request.get_json(silent=True) or {}
    mid   = data.get('message_id'); value = data.get('value')
    if mid is None or value not in (1, -1): return jsonify({'error': 'Invalid'}), 400
    msg = q1("SELECT id,location_id FROM messages WHERE id=%s", (mid,))
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex  = q1("SELECT value FROM votes WHERE message_id=%s AND user_id=%s", (mid, u['id']))
    if ex:
        if ex['value'] == value:
            execute("DELETE FROM votes WHERE message_id=%s AND user_id=%s", (mid, u['id']))
            execute("UPDATE messages SET score=score-%s WHERE id=%s", (value, mid))
            new_vote = 0
        else:
            execute("UPDATE votes SET value=%s WHERE message_id=%s AND user_id=%s", (value, mid, u['id']))
            execute("UPDATE messages SET score=score+%s WHERE id=%s", (value*2, mid))
            new_vote = value
    else:
        execute("INSERT INTO votes (message_id,user_id,value) VALUES (%s,%s,%s)", (mid, u['id'], value))
        execute("UPDATE messages SET score=score+%s WHERE id=%s", (value, mid))
        new_vote = value
    commit()
    score = qval("SELECT score FROM messages WHERE id=%s", (mid,))
    broker.publish(msg['location_id'], 'vote_update', {'id': mid, 'score': score})
    return jsonify({'score': score, 'user_vote': new_vote})

# ── API: reactions ────────────────────────────────────────────────────────────
ALLOWED_EMOJIS = {
    '👍','👎','❤️','😂','😮','😢','🔥','👏','🌍','📍',
    '🎉','😡','🤔','👀','💯','🙏','⭐','🗺️','✍️','🕊️',
    '😎','🤝','💬','🌱','⚡'
}

@app.route('/api/react', methods=['POST'])
def react():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'react'): return jsonify({'error': 'Rate limit'}), 429
    data  = request.get_json(silent=True) or {}
    mid   = data.get('message_id'); emoji = data.get('emoji')
    if not mid or emoji not in ALLOWED_EMOJIS: return jsonify({'error': 'Invalid emoji'}), 400
    msg = q1("SELECT id,location_id FROM messages WHERE id=%s", (mid,))
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex  = q1("SELECT id FROM reactions WHERE message_id=%s AND user_id=%s AND emoji=%s", (mid, u['id'], emoji))
    if ex:
        execute("DELETE FROM reactions WHERE message_id=%s AND user_id=%s AND emoji=%s", (mid, u['id'], emoji))
    else:
        execute("INSERT INTO reactions (message_id,user_id,emoji) VALUES (%s,%s,%s)", (mid, u['id'], emoji))
    commit()
    reactions = get_reactions(mid, u['id'])
    broker.publish(msg['location_id'], 'reaction_update', {'id': mid, 'reactions': reactions})
    return jsonify({'reactions': reactions})

# ── API: reports ──────────────────────────────────────────────────────────────
@app.route('/api/report', methods=['POST'])
def report():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'report'): return jsonify({'error': 'Rate limit'}), 429
    data   = request.get_json(silent=True) or {}
    mid    = data.get('message_id')
    reason = html.escape(data.get('reason', '').strip()[:200])
    if not mid or not reason: return jsonify({'error': 'message_id and reason required'}), 400
    if not q1("SELECT id FROM messages WHERE id=%s", (mid,)):
        return jsonify({'error': 'Not found'}), 404
    if q1("SELECT id FROM reports WHERE message_id=%s AND reporter_id=%s AND status='pending'",
          (mid, u['id'])):
        return jsonify({'error': 'Already reported'}), 400
    execute("INSERT INTO reports (message_id,reporter_id,reason) VALUES (%s,%s,%s)",
            (mid, u['id'], reason))
    commit()
    return jsonify({'ok': True})

# ── API: notifications ────────────────────────────────────────────────────────
@app.route('/api/notifications')
def get_notifications():
    u = current_user()
    if not u: return jsonify([])
    rows = qall("""
        SELECT n.id,n.read,n.created_at,
               r.content as reply_content,ru.username as reply_username,ru.avatar_url as reply_avatar,
               m.content as original_content,l.place_name,l.id as location_id
        FROM notifications n
        JOIN messages r  ON n.reply_id=r.id
        JOIN users ru    ON r.user_id=ru.id
        JOIN messages m  ON n.message_id=m.id
        JOIN locations l ON m.location_id=l.id
        WHERE n.user_id=%s ORDER BY n.created_at DESC LIMIT 30""", (u['id'],))
    return jsonify([dict(r) for r in rows])

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    execute("UPDATE notifications SET read=1 WHERE user_id=%s", (u['id'],))
    commit()
    return jsonify({'ok': True})

@app.route('/api/notifications/unread-count')
def unread_count():
    u = current_user()
    if not u: return jsonify({'count': 0})
    return jsonify({'count': qval("SELECT COUNT(*) FROM notifications WHERE user_id=%s AND read=0",
                                  (u['id'],)) or 0})

# ── API: translate ────────────────────────────────────────────────────────────
@app.route('/api/translate', methods=['POST'])
def translate():
    data   = request.get_json(silent=True) or {}
    text   = data.get('text', '').strip()[:500]
    target = data.get('target', 'en')
    if not text: return jsonify({'error': 'No text'}), 400
    if not LIBRETRANSLATE_URL: return jsonify({'error': 'Translation unavailable', 'ok': False}), 503
    try:
        res = _http.post(f"{LIBRETRANSLATE_URL}/translate",
                         json={'q': text, 'source': 'auto', 'target': target, 'format': 'text'},
                         timeout=5)
        if res.ok:
            return jsonify({'translated': res.json().get('translatedText', ''), 'ok': True})
    except Exception: pass
    return jsonify({'error': 'Translation unavailable', 'ok': False}), 503

# ── Health / ping ─────────────────────────────────────────────────────────────
@app.route('/health')
@app.route('/ping')
def health():
    try:
        qval("SELECT 1")
        return jsonify({'status': 'ok', 'db': 'postgresql'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

@app.route('/debug/config')
def debug_config():
    """Admin-only: verify live configuration (redirect URI, client ID, etc.)"""
    u = current_user()
    if not u or not u['is_admin']:
        return jsonify({'error': 'Admin only'}), 403
    return jsonify({
        'redirect_uri':       DISCORD_REDIRECT_URI,
        'client_id_set':      bool(DISCORD_CLIENT_ID),
        'client_secret_set':  bool(DISCORD_CLIENT_SECRET),
        'database_url_set':   bool(DATABASE_URL),
        'secret_key_set':     bool(os.environ.get('SECRET_KEY')),
        'is_production':      IS_PRODUCTION,
        'request_scheme':     request.scheme,
        'request_host':       request.host,
        'proxy_url':          DISCORD_PROXY_URL or '(not set — using direct)',
        'proxy_secret_set':   bool(DISCORD_PROXY_SECRET),
        'token_endpoint':     f"{DISCORD_PROXY_URL}/token" if DISCORD_PROXY_URL else f"{DISCORD_API}/oauth2/token",
    })

@app.route('/debug/proxy-test')
def debug_proxy_test():
    """Admin-only: ping the worker proxy with a dummy request to verify it's reachable."""
    u = current_user()
    if not u or not u['is_admin']:
        return jsonify({'error': 'Admin only'}), 403
    if not DISCORD_PROXY_URL:
        return jsonify({'error': 'DISCORD_PROXY_URL not set'}), 400
    token_url = f"{DISCORD_PROXY_URL}/token"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if DISCORD_PROXY_SECRET:
        headers['X-Proxy-Secret'] = DISCORD_PROXY_SECRET
    try:
        # Send intentionally bad data — we just want to see if the worker responds at all
        r = _http.post(token_url, data='grant_type=test', headers=headers, timeout=8)
        return jsonify({
            'worker_reachable': True,
            'worker_url': token_url,
            'discord_status': r.status_code,
            'discord_body': r.text[:300],
        })
    except Exception as e:
        return jsonify({'worker_reachable': False, 'error': str(e)})

# ── Boot ──────────────────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    if not DISCORD_CLIENT_ID: print("WARNING: DISCORD_CLIENT_ID not set", file=sys.stderr)
    if not DATABASE_URL:      print("WARNING: DATABASE_URL not set", file=sys.stderr)
    print(f"Redirect URI: {DISCORD_REDIRECT_URI}", file=sys.stderr)
    app.run(host='0.0.0.0', port=PORT, debug=not IS_PRODUCTION,
            threaded=True, use_reloader=not IS_PRODUCTION)
