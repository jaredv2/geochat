"""
GeoChat — location-based real-time discussion
=============================================
Required environment variables:
  DISCORD_CLIENT_ID      Discord OAuth app client ID
  DISCORD_CLIENT_SECRET  Discord OAuth app client secret
  DISCORD_REDIRECT_URI   e.g. https://yourdomain.com/callback
  SECRET_KEY             Flask session secret (auto-generated if omitted in dev)

Optional:
  ADMIN_DISCORD_ID       Your Discord user ID for admin panel access
  LIBRETRANSLATE_URL     LibreTranslate instance URL for message translation
  DATABASE_PATH          SQLite file path (default: ./database.db)
  PORT                   HTTP port (default: 8000)

Deploy:
  See README.md for Render + UptimeRobot free deployment guide.
"""

import os, sqlite3, secrets, html, json, queue, threading, unicodedata, re
from flask import (Flask, render_template, request, redirect,
                   session, jsonify, g, Response, stream_with_context, abort)
import requests as http
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse
# ... rest of imports
app = Flask(__name__)

# ── Secret key ────────────────────────────────────────────────────────────────
_env_key = os.environ.get('SECRET_KEY', '')
if _env_key:
    app.secret_key = _env_key
else:
    # Dev fallback: persist to file so sessions survive restarts
    _kf = os.path.join(os.path.dirname(__file__), '.secret_key')
    if os.path.exists(_kf):
        with open(_kf) as f: app.secret_key = f.read().strip()
    else:
        _k = secrets.token_hex(32)
        try: open(_kf, 'w').write(_k)
        except OSError: pass
        app.secret_key = _k
    import sys
    print("⚠️  SECRET_KEY not set — using ephemeral key (sessions lost on restart)", file=sys.stderr)

# ── Production detection ──────────────────────────────────────────────────────
IS_PRODUCTION = os.environ.get('RENDER', '') == 'true' or os.environ.get('PRODUCTION', '') == '1'

app.config.update(
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,   # HTTPS only in prod
    PREFERRED_URL_SCHEME='https' if IS_PRODUCTION else 'http',
)

PORT                  = int(os.environ.get('PORT', 8000))
DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', f'http://localhost:{PORT}/callback')
DISCORD_API           = 'https://discord.com/api/v10'
ADMIN_DISCORD_ID      = os.environ.get('ADMIN_DISCORD_ID', '')
LIBRETRANSLATE_URL    = os.environ.get('LIBRETRANSLATE_URL', '')
DATABASE              = os.environ.get('DATABASE_PATH',
                            os.path.join(os.path.dirname(__file__), 'database.db'))
ONLINE_WINDOW_SECS    = 120  # user considered online if seen within 2 min

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

RATE_LIMITS = {
    'post_message': (5, 60),
    'vote':         (30, 60),
    'react':        (20, 60),
    'report':       (3, 300),
}

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
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            dead = []
            for q in self.listeners.get(lid, []):
                try: q.put_nowait(payload)
                except queue.Full: dead.append(q)
            for q in dead:
                self.listeners[lid].remove(q)

broker = SSEBroker()

# ── Background cleanup thread ─────────────────────────────────────────────────
def _cleanup_loop():
    """Every 2 minutes: delete locations with no messages (orphan markers)."""
    import time, logging
    log = logging.getLogger('geochat.cleanup')
    while True:
        time.sleep(120)
        try:
            with app.app_context():
                db = get_db()
                # Find locations with message_count=0 older than 5 minutes
                orphans = db.execute("""
                    SELECT id FROM locations
                    WHERE message_count = 0
                    AND created_at < datetime('now', '-5 minutes')
                """).fetchall()
                for row in orphans:
                    lid = row['id']
                    # Double-check no messages exist (count could be stale)
                    real = db.execute(
                        "SELECT COUNT(*) FROM messages WHERE location_id=?", (lid,)
                    ).fetchone()[0]
                    if real == 0:
                        db.execute("DELETE FROM online_presence WHERE location_id=?", (lid,))
                        db.execute("DELETE FROM locations WHERE id=?", (lid,))
                if orphans:
                    db.commit()
                    log.info("Cleanup: removed %d orphan location(s)", len(orphans))
                # Also clean stale presence records
                db.execute("""DELETE FROM online_presence
                              WHERE last_seen < datetime('now', '-10 minutes')""")
                db.commit()
                close_db()
        except Exception as e:
            logging.getLogger('geochat.cleanup').error("Cleanup error: %s", e)

_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        db_url = os.environ.get('DATABASE_URL')
        
        if db_url:
            # PostgreSQL Connection
            result = urlparse(db_url)
            username = result.username
            password = result.password
            database = result.path[1:]
            hostname = result.hostname
            port = result.port
            
            conn = psycopg2.connect(
                database=database,
                user=username,
                password=password,
                host=hostname,
                port=port
            )
            # Use DictCursor to access columns by name (like sqlite3.Row)
            g.db_type = 'postgres'
            g.db = conn
        else:
            # SQLite Fallback (Local Dev)
            g.db_type = 'sqlite'
            g.db = sqlite3.connect(DATABASE)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    db = get_db()
    
    # 1. Convert ? to %s for Postgres
    if getattr(g, 'db_type', 'sqlite') == 'postgres':
        query = query.replace('?', '%s')
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    else:
        cursor = db.cursor()

    # 2. Execute
    cursor.execute(query, args)
    
    # 3. Commit if it's a modification (INSERT/UPDATE/DELETE)
    if query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
        db.commit()
        # Return last row id for inserts if needed
        if 'INSERT' in query.upper() and getattr(g, 'db_type', 'sqlite') == 'postgres':
             try:
                 # Postgres doesn't support cursor.lastrowid directly for all drivers
                 # Ideally, use "RETURNING id" in your SQL queries for Postgres
                 pass 
             except: pass
        return cursor

    # 4. Return results
    rv = cursor.fetchall()
    cursor.close()
    return (rv[0] if rv else None) if one else rv
def init_db():
    with app.app_context():
        db = get_db()
        with open(os.path.join(os.path.dirname(__file__), 'schema.sql')) as f:
            db.executescript(f.read())
        db.commit()
        # Migrate rate_limits if FK version
        try:
            db.execute("INSERT INTO rate_limits (user_id,action) VALUES (0,'_t')")
            db.execute("DELETE FROM rate_limits WHERE user_id=0")
            db.commit()
        except Exception:
            db.execute("DROP TABLE IF EXISTS rate_limits")
            db.execute("""CREATE TABLE rate_limits(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, action TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            db.commit()
        # Migrate ban columns if missing
        cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        if 'is_banned' not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            db.commit()
        if 'ban_reason' not in cols:
            db.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
            db.commit()
        # Migrate top_content on locations
        loc_cols = [r[1] for r in db.execute("PRAGMA table_info(locations)").fetchall()]
        if 'top_content' not in loc_cols:
            db.execute("ALTER TABLE locations ADD COLUMN top_content TEXT")
            db.commit()

# ── Ban check ─────────────────────────────────────────────────────────────────
def check_banned():
    """Call at start of any route that a banned user should not access."""
    if 'user_id' not in session: return
    row = get_db().execute("SELECT is_banned, ban_reason FROM users WHERE id=?",
                           (session['user_id'],)).fetchone()
    if row and row['is_banned']:
        reason = row['ban_reason'] or 'No reason given.'
        session.clear()
        abort(Response(render_template('banned.html', reason=reason), status=403))

# ── Helpers ───────────────────────────────────────────────────────────────────
def current_user():
    if 'user_id' not in session: return None
    # Guard against stale sessions (DB was wiped, user deleted, etc.)
    row = get_db().execute(
        "SELECT id, username, avatar_url, is_admin, is_banned FROM users WHERE id=?",
        (session['user_id'],)).fetchone()
    if not row:
        session.clear()
        return None
    if row['is_banned']:
        session.clear()
        return None
    return {'id': row['id'], 'username': row['username'],
            'avatar_url': row['avatar_url'], 'is_admin': row['is_admin']}

def check_rate_limit(user_id, action):
    limit, window = RATE_LIMITS.get(action, (10, 60))
    db = get_db()
    db.execute("DELETE FROM rate_limits WHERE action=? AND created_at < datetime('now',? || ' seconds')",
               (action, f'-{window}'))
    count = db.execute("SELECT COUNT(*) FROM rate_limits WHERE user_id=? AND action=?",
                       (user_id, action)).fetchone()[0]
    if count >= limit: return False
    db.execute("INSERT INTO rate_limits (user_id,action) VALUES (?,?)", (user_id, action))
    db.commit()
    return True

def get_reactions(mid, uid=None):
    db = get_db()
    rows = db.execute(
        "SELECT emoji, COUNT(*) as cnt FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY cnt DESC",
        (mid,)).fetchall()
    mine = set()
    if uid:
        mine = {r['emoji'] for r in db.execute(
            "SELECT emoji FROM reactions WHERE message_id=? AND user_id=?", (mid, uid)).fetchall()}
    return [{'emoji': r['emoji'], 'count': r['cnt'], 'reacted': r['emoji'] in mine} for r in rows]

def fmt_msg(row, uid=None, replies=None):
    d = dict(row)
    d['replies']   = replies or []
    d['reactions'] = get_reactions(d['id'], uid)
    d['user_vote'] = 0
    if uid:
        v = get_db().execute("SELECT value FROM votes WHERE message_id=? AND user_id=?",
                              (d['id'], uid)).fetchone()
        if v: d['user_vote'] = v['value']
    return d

def get_online_count(lid):
    return get_db().execute(
        "SELECT COUNT(DISTINCT user_id) FROM online_presence WHERE location_id=? AND last_seen > datetime('now',? || ' seconds')",
        (lid, f'-{ONLINE_WINDOW_SECS}')).fetchone()[0]

def touch_presence(uid, lid):
    if not uid: return
    db = get_db()
    try:
        db.execute("""INSERT INTO online_presence (user_id, location_id, last_seen)
                      VALUES (?,?, CURRENT_TIMESTAMP)
                      ON CONFLICT(user_id, location_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",
                   (uid, lid))
        db.commit()
    except Exception:
        db.rollback()

def transliterate_to_ascii(text):
    """Best-effort: decompose unicode, strip non-ASCII combining chars, keep readable result."""
    try:
        normalized = unicodedata.normalize('NFKD', text)
        ascii_text = normalized.encode('ascii', 'ignore').decode('ascii').strip()
        if ascii_text and len(ascii_text) >= max(1, len(text) // 3):
            return ascii_text
    except Exception:
        pass
    return text

def has_non_latin(text):
    """Return True if text contains non-Latin script characters."""
    for ch in text:
        if ch.isalpha():
            try:
                name = unicodedata.name(ch, '')
                if not any(s in name for s in ('LATIN', 'DIGIT', 'SPACE')):
                    return True
            except Exception:
                pass
    return False

def translate_place_to_english(text):
    """Try LibreTranslate to get an English version of a place name."""
    try:
        res = http.post(f"{LIBRETRANSLATE_URL}/translate",
                        json={'q': text, 'source': 'auto', 'target': 'en', 'format': 'text'},
                        timeout=3)
        if res.ok:
            t = res.json().get('translatedText', '').strip()
            if t and t != text:
                return t
    except Exception:
        pass
    return transliterate_to_ascii(text)

def award_badges(user_id):
    from datetime import datetime
    db = get_db()
    u  = db.execute("SELECT post_count FROM users WHERE id=?", (user_id,)).fetchone()
    if not u: return []
    earned = {r['badge'] for r in db.execute("SELECT badge FROM badges WHERE user_id=?", (user_id,)).fetchall()}
    new_badges = []

    def give(badge):
        if badge not in earned:
            db.execute("INSERT OR IGNORE INTO badges (user_id,badge) VALUES (?,?)", (user_id, badge))
            new_badges.append(badge)

    pc = u['post_count']
    if pc >= 1:   give('first_post')
    if pc >= 25:  give('veteran')
    if pc >= 100: give('centurion')

    locs = db.execute("SELECT COUNT(DISTINCT location_id) FROM messages WHERE user_id=? AND parent_id IS NULL",
                      (user_id,)).fetchone()[0]
    if locs >= 5:  give('explorer')
    if locs >= 20: give('globe')
    if locs >= 50: give('cartographer')

    replies = db.execute("SELECT COUNT(*) FROM messages WHERE user_id=? AND parent_id IS NOT NULL",
                         (user_id,)).fetchone()[0]
    if replies >= 20: give('debater')

    total_reactions = db.execute("SELECT COUNT(*) FROM reactions WHERE user_id=?", (user_id,)).fetchone()[0]
    if total_reactions >= 30: give('reactor')

    score = db.execute("SELECT COALESCE(SUM(score),0) FROM messages WHERE user_id=?",
                       (user_id,)).fetchone()[0]
    if score >= 10:  give('popular')
    if score >= 50:  give('loved')
    if score >= 200: give('influencer')

    hour = datetime.utcnow().hour
    if 0 <= hour < 4:  give('night_owl')
    if 5 <= hour < 7:  give('early_bird')

    if new_badges: db.commit()
    return new_badges

# ── OAuth ─────────────────────────────────────────────────────────────────────
@app.route('/login')
def login():
    state = secrets.token_urlsafe(24)
    db = get_db()
    db.execute("DELETE FROM oauth_states WHERE created_at < datetime('now','-10 minutes')")
    db.execute("INSERT INTO oauth_states (state) VALUES (?)", (state,))
    db.commit()
    from urllib.parse import urlencode
    p = urlencode({'client_id': DISCORD_CLIENT_ID, 'redirect_uri': DISCORD_REDIRECT_URI,
                   'response_type': 'code', 'scope': 'identify', 'state': state})
    return redirect(f"https://discord.com/oauth2/authorize?{p}")

@app.route('/callback')
def callback():
    code, state = request.args.get('code'), request.args.get('state')
    if not code or not state: return "Missing params. <a href='/login'>Retry</a>", 400
    db = get_db()
    if not db.execute("SELECT id FROM oauth_states WHERE state=?", (state,)).fetchone():
        return "Invalid state. <a href='/login'>Retry</a>", 400
    db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    db.commit()
    tr = http.post(f"{DISCORD_API}/oauth2/token", data={
        'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': DISCORD_REDIRECT_URI},
        headers={'Content-Type': 'application/x-www-form-urlencoded'})
    if not tr.ok: return f"Token error: {tr.text}", 400
    ur = http.get(f"{DISCORD_API}/users/@me",
                  headers={'Authorization': f"Bearer {tr.json()['access_token']}"})
    if not ur.ok: return "User fetch failed.", 400
    u   = ur.json()
    did = u['id']
    username = u['username']
    avatar   = (f"https://cdn.discordapp.com/avatars/{did}/{u['avatar']}.png"
                if u.get('avatar') else
                f"https://cdn.discordapp.com/embed/avatars/{int(did)%5}.png")
    is_admin = 1 if (ADMIN_DISCORD_ID and did == ADMIN_DISCORD_ID) else 0
    db.execute("""INSERT INTO users (discord_id,username,avatar_url,is_admin) VALUES (?,?,?,?)
                  ON CONFLICT(discord_id) DO UPDATE SET
                    username=excluded.username, avatar_url=excluded.avatar_url,
                    is_admin=MAX(is_admin, excluded.is_admin)""",
               (did, username, avatar, is_admin))
    db.commit()
    row = db.execute("SELECT id, is_admin, is_banned, ban_reason FROM users WHERE discord_id=?", (did,)).fetchone()
    if row and row['is_banned']:
        reason = row['ban_reason'] or 'No reason given.'
        return Response(render_template('banned.html', reason=reason), status=403)
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
    db  = get_db()
    pu  = db.execute("SELECT id,username,avatar_url,post_count,created_at FROM users WHERE id=?",
                     (uid,)).fetchone()
    if not pu: return "Not found", 404
    ub  = db.execute("SELECT badge,earned_at FROM badges WHERE user_id=? ORDER BY earned_at",
                     (uid,)).fetchall()
    return render_template('profile.html', profile_user=dict(pu), current_user=current_user(),
                           badges=[dict(b) for b in ub], badge_defs=BADGE_DEFS)

@app.route('/leaderboard')
def leaderboard():
    db  = get_db()
    top_locs  = db.execute(
        "SELECT id,place_name,message_count,latitude,longitude FROM locations ORDER BY message_count DESC LIMIT 20"
    ).fetchall()
    top_users = db.execute("""
        SELECT u.id,u.username,u.avatar_url,u.post_count,
               COALESCE(SUM(m.score),0) as total_score,
               COUNT(DISTINCT m.location_id) as loc_count
        FROM users u LEFT JOIN messages m ON m.user_id=u.id
        GROUP BY u.id ORDER BY total_score DESC LIMIT 20
    """).fetchall()
    stats = {
        'total_users':    db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'total_messages': db.execute("SELECT COUNT(*) FROM messages WHERE hidden=0").fetchone()[0],
        'total_locations':db.execute("SELECT COUNT(*) FROM locations WHERE message_count>0").fetchone()[0],
        'online_users':   db.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM online_presence WHERE last_seen > datetime('now','-{ONLINE_WINDOW_SECS} seconds')"
        ).fetchone()[0],
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
    db = get_db()
    reports = db.execute("""
        SELECT r.id,r.reason,r.status,r.created_at,
               m.id as msg_id,m.content,m.hidden,
               rep.username as reporter,rep.avatar_url as reporter_avatar,
               au.id as author_id, au.username as author, l.place_name
        FROM reports r
        JOIN messages m   ON r.message_id=m.id
        JOIN users rep    ON r.reporter_id=rep.id
        JOIN users au     ON m.user_id=au.id
        JOIN locations l  ON m.location_id=l.id
        WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 50
    """).fetchall()
    return render_template('admin.html', reports=[dict(r) for r in reports], current_user=u)

# ── SSE ───────────────────────────────────────────────────────────────────────
@app.route('/api/stream/<int:lid>')
def stream(lid):
    u   = current_user()
    uid = u['id'] if u else None
    if uid: touch_presence(uid, lid)
    q   = broker.subscribe(lid)
    def generate():
        yield f"data: connected\n\n"
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

# ── API: global stats ─────────────────────────────────────────────────────────
@app.route('/api/stats')
def global_stats():
    db = get_db()
    return jsonify({
        'total_users':    db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'total_messages': db.execute("SELECT COUNT(*) FROM messages WHERE hidden=0").fetchone()[0],
        'total_locations':db.execute("SELECT COUNT(*) FROM locations WHERE message_count>0").fetchone()[0],
        'online_users':   db.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM online_presence WHERE last_seen > datetime('now','-{ONLINE_WINDOW_SECS} seconds')"
        ).fetchone()[0],
    })

# ── API: locations ────────────────────────────────────────────────────────────
@app.route('/api/locations/nearby')
def nearby():
    try:
        lat   = float(request.args['lat']); lng   = float(request.args['lng'])
        dlat  = float(request.args.get('dlat', 1.0))
        dlng  = float(request.args.get('dlng', 1.0))
        radius_km = float(request.args.get('radius', 0))
        requester_id = request.args.get('uid', type=int)
    except (KeyError, ValueError): return jsonify({'error': 'invalid params'}), 400

    db   = get_db()
    rows = db.execute("""
        SELECT id,latitude,longitude,place_name,message_count,last_user_avatar,last_user_id,top_content
        FROM locations
        WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
        ORDER BY message_count DESC LIMIT 300
    """, (lat-dlat, lat+dlat, lng-dlng, lng+dlng)).fetchall()

    results = []
    for r in rows:
        # Skip empty locations that don't belong to this requester
        if r['message_count'] == 0 and r['last_user_id'] != requester_id:
            continue
        if radius_km > 0:
            import math
            dlat2 = math.radians(r['latitude'] - lat)
            dlng2 = math.radians(r['longitude'] - lng)
            a = (math.sin(dlat2/2)**2 +
                 math.cos(math.radians(lat)) * math.cos(math.radians(r['latitude'])) * math.sin(dlng2/2)**2)
            if 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)) > radius_km: continue
        results.append(dict(r))
    return jsonify(results)

@app.route('/api/locations/heatmap')
def heatmap():
    rows = get_db().execute(
        "SELECT latitude,longitude,message_count FROM locations WHERE message_count>0 ORDER BY message_count DESC LIMIT 500"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/location', methods=['POST'])
def create_location():
    u    = current_user()
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data['latitude']); lng = float(data['longitude'])
    except (KeyError, ValueError, TypeError): return jsonify({'error': 'Invalid coords'}), 400
    place = html.escape(str(data.get('place_name', 'Unknown'))[:200])
    db    = get_db()
    ex    = db.execute("""SELECT id,place_name,message_count,last_user_avatar,top_content FROM locations
                          WHERE ABS(latitude-?) < 0.0001 AND ABS(longitude-?) < 0.0001 LIMIT 1""",
                       (lat, lng)).fetchone()
    if ex: return jsonify(dict(ex))
    # Verify user actually exists in DB (stale session guard)
    uid_safe = None
    avatar_safe = None
    if u:
        row = db.execute("SELECT id, avatar_url FROM users WHERE id=?", (u['id'],)).fetchone()
        if row:
            uid_safe    = row['id']
            avatar_safe = row['avatar_url']
    cur = db.execute("INSERT INTO locations (latitude,longitude,place_name,last_user_id) VALUES (?,?,?,?)",
                     (lat, lng, place, uid_safe))
    db.commit()
    return jsonify({'id': cur.lastrowid, 'place_name': place, 'message_count': 0,
                    'last_user_avatar': avatar_safe, 'top_content': None}), 201

@app.route('/api/location/<int:lid>')
def get_location(lid):
    row = get_db().execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))

@app.route('/api/location/<int:lid>', methods=['DELETE'])
def delete_empty_location(lid):
    u  = current_user()
    db = get_db()
    row = db.execute("SELECT message_count,last_user_id FROM locations WHERE id=?", (lid,)).fetchone()
    if not row: return jsonify({'ok': True})
    if row['message_count'] > 0: return jsonify({'error': 'Has messages'}), 400
    # Only allow creator to delete their empty marker
    if u and row['last_user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    db.execute("DELETE FROM locations WHERE id=? AND message_count=0", (lid,))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/location/<int:lid>/online')
def location_online(lid):
    return jsonify({'count': get_online_count(lid)})

@app.route('/api/search')
def search():
    q = request.args.get('q','').strip()
    if len(q) < 2: return jsonify([])
    rows = get_db().execute(
        "SELECT id,latitude,longitude,place_name,message_count FROM locations WHERE place_name LIKE ? ORDER BY message_count DESC LIMIT 8",
        (f'%{q}%',)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/search/world')
def search_world():
    """Search the whole world via Nominatim. Returns geocoded place suggestions."""
    q = request.args.get('q','').strip()
    if len(q) < 2: return jsonify([])
    try:
        res = http.get('https://nominatim.openstreetmap.org/search', params={
            'q': q, 'format': 'json', 'limit': 6, 'addressdetails': 1,
            'accept-language': 'en',
        }, headers={'User-Agent': 'GeoChat/4.0'}, timeout=5)
        if not res.ok: return jsonify([])
        results = []
        for item in res.json():
            # Build clean English label
            addr = item.get('address', {})
            name = (addr.get('tourism') or addr.get('amenity') or addr.get('building') or
                    addr.get('road') or addr.get('neighbourhood') or addr.get('suburb') or
                    addr.get('town') or addr.get('village') or addr.get('city') or
                    addr.get('county') or addr.get('state') or item.get('name',''))
            country = addr.get('country','')
            label = f"{name}, {country}" if name and country and name != country else (name or item.get('display_name',''))
            # Transliterate if has non-latin
            if has_non_latin(label):
                label = translate_place_to_english(label)
            results.append({
                'label': label[:120],
                'lat': float(item['lat']),
                'lng': float(item['lon']),
                'type': item.get('type',''),
            })
        return jsonify(results)
    except Exception as e:
        return jsonify([])

@app.route('/api/place/translate', methods=['POST'])
def place_translate():
    """Translate/transliterate a place name to English."""
    data = request.get_json(silent=True) or {}
    name = data.get('name','').strip()[:200]
    if not name: return jsonify({'name': name})
    if not has_non_latin(name):
        return jsonify({'name': name, 'translated': False})
    english = translate_place_to_english(name)
    return jsonify({'name': english, 'translated': english != name})

# ── API: admin ban/unban ──────────────────────────────────────────────────────
@app.route('/api/admin/ban/<int:target_uid>', methods=['POST'])
def ban_user(target_uid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json(silent=True) or {}
    reason = html.escape(data.get('reason','No reason given.')[:200])
    db     = get_db()
    row    = db.execute("SELECT is_admin FROM users WHERE id=?", (target_uid,)).fetchone()
    if not row: return jsonify({'error': 'Not found'}), 404
    if row['is_admin']: return jsonify({'error': 'Cannot ban an admin'}), 400
    db.execute("UPDATE users SET is_banned=1, ban_reason=? WHERE id=?", (reason, target_uid))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/admin/unban/<int:target_uid>', methods=['POST'])
def unban_user(target_uid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    db = get_db()
    db.execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE id=?", (target_uid,))
    db.commit()
    return jsonify({'ok': True})

# ── API: messages ─────────────────────────────────────────────────────────────
@app.route('/api/admin/users')
def admin_users():
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    q   = request.args.get('q', '').strip()
    db  = get_db()
    if q:
        rows = db.execute("""
            SELECT id, username, avatar_url, post_count, is_banned, ban_reason, created_at
            FROM users WHERE username LIKE ? AND is_admin=0
            ORDER BY created_at DESC LIMIT 30
        """, (f'%{q}%',)).fetchall()
    else:
        rows = db.execute("""
            SELECT id, username, avatar_url, post_count, is_banned, ban_reason, created_at
            FROM users WHERE is_admin=0
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/messages/<int:lid>')
def get_messages(lid):
    u   = current_user(); uid = u['id'] if u else None
    db  = get_db()
    if uid: touch_presence(uid, lid)
    if not db.execute("SELECT id FROM locations WHERE id=?", (lid,)).fetchone():
        return jsonify({'error': 'Not found'}), 404
    top = db.execute("""
        SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
               u.id as user_id,u.username,u.avatar_url
        FROM messages m JOIN users u ON m.user_id=u.id
        WHERE m.location_id=? AND m.parent_id IS NULL AND m.hidden=0
        ORDER BY m.score DESC, m.created_at DESC LIMIT 100
    """, (lid,)).fetchall()
    result = []
    for msg in top:
        replies = db.execute("""
            SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
                   u.id as user_id,u.username,u.avatar_url
            FROM messages m JOIN users u ON m.user_id=u.id
            WHERE m.parent_id=? AND m.hidden=0 ORDER BY m.created_at ASC LIMIT 50
        """, (msg['id'],)).fetchall()
        result.append(fmt_msg(msg, uid, [fmt_msg(r, uid) for r in replies]))
    return jsonify(result)

@app.route('/api/messages/user/<int:uid>')
def user_messages(uid):
    rows = get_db().execute("""
        SELECT m.id,m.content,m.score,m.edited,m.created_at,
               l.place_name,l.id as location_id,l.latitude,l.longitude
        FROM messages m JOIN locations l ON m.location_id=l.id
        WHERE m.user_id=? AND m.parent_id IS NULL AND m.hidden=0
        ORDER BY m.created_at DESC LIMIT 50
    """, (uid,)).fetchall()
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
    db      = get_db()
    if not db.execute("SELECT id FROM locations WHERE id=?", (lid,)).fetchone():
        return jsonify({'error': 'Location not found'}), 404
    parent = None
    if parent_id:
        parent = db.execute("SELECT id,user_id,location_id FROM messages WHERE id=?",
                             (parent_id,)).fetchone()
        if not parent or parent['location_id'] != lid:
            return jsonify({'error': 'Invalid parent'}), 400
# Change this:
# cur = db.execute("INSERT ...")
# mid = cur.lastrowid

# To this:
    if getattr(g, 'db_type', 'sqlite') == 'postgres':
        cur = db.cursor()
        cur.execute("INSERT INTO messages (...) VALUES (...) RETURNING id", (args...))
        mid = cur.fetchone()[0]
        db.commit()
    else:
        cur = db.execute("INSERT INTO messages (...) VALUES (...)", (args...))
        mid = cur.lastrowid
        db.commit()
    db.execute("UPDATE locations SET message_count=message_count+1,last_user_id=?,last_user_avatar=? WHERE id=?",
               (u['id'], u['avatar_url'], lid))
    if not parent_id:
        db.execute("UPDATE locations SET top_content=? WHERE id=?", (content[:80], lid))
    db.execute("UPDATE users SET post_count=post_count+1 WHERE id=?", (u['id'],))
    if parent and parent['user_id'] != u['id']:
        db.execute("INSERT INTO notifications (user_id,message_id,reply_id) VALUES (?,?,?)",
                   (parent['user_id'], parent_id, mid))
    db.commit()
    new_badges = award_badges(u['id'])
    row = db.execute("""SELECT m.id,m.content,m.score,m.edited,m.hidden,m.created_at,m.parent_id,
                               u.id as user_id,u.username,u.avatar_url
                        FROM messages m JOIN users u ON m.user_id=u.id WHERE m.id=?""",
                     (mid,)).fetchone()
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
    db  = get_db()
    msg = db.execute("SELECT user_id,location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    db.execute("UPDATE messages SET content=?,edited=1 WHERE id=?", (html.escape(content), mid))
    db.commit()
    broker.publish(msg['location_id'], 'edit_message', {'id': mid, 'content': html.escape(content)})
    return jsonify({'ok': True})

@app.route('/api/message/<int:mid>', methods=['DELETE'])
def delete_message(mid):
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    db  = get_db()
    msg = db.execute("SELECT user_id,location_id,parent_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    lid = msg['location_id']

    # Also delete all child replies and their related data
    child_ids = [r['id'] for r in db.execute(
        "SELECT id FROM messages WHERE parent_id=?", (mid,)).fetchall()]
    for cid in child_ids:
        db.execute("DELETE FROM votes WHERE message_id=?", (cid,))
        db.execute("DELETE FROM reactions WHERE message_id=?", (cid,))
        db.execute("DELETE FROM notifications WHERE message_id=? OR reply_id=?", (cid, cid))
        db.execute("DELETE FROM messages WHERE id=?", (cid,))

    db.execute("DELETE FROM votes WHERE message_id=?", (mid,))
    db.execute("DELETE FROM reactions WHERE message_id=?", (mid,))
    db.execute("DELETE FROM notifications WHERE message_id=? OR reply_id=?", (mid, mid))
    db.execute("DELETE FROM messages WHERE id=?", (mid,))

    location_deleted = False
    if not msg['parent_id']:
        db.execute("UPDATE locations SET message_count=MAX(0,message_count-1) WHERE id=?", (lid,))
        remaining = db.execute(
            "SELECT COUNT(*) FROM messages WHERE location_id=? AND parent_id IS NULL AND hidden=0",
            (lid,)).fetchone()[0]
        if remaining == 0:
            # Wipe the location entirely
            db.execute("DELETE FROM online_presence WHERE location_id=?", (lid,))
            db.execute("DELETE FROM locations WHERE id=?", (lid,))
            location_deleted = True

    # Refresh top_content after deletion
    if not msg['parent_id'] and not location_deleted:
        best = db.execute(
            "SELECT content FROM messages WHERE location_id=? AND parent_id IS NULL AND hidden=0 ORDER BY score DESC, created_at DESC LIMIT 1",
            (lid,)).fetchone()
        db.execute("UPDATE locations SET top_content=? WHERE id=?",
                   (best['content'][:80] if best else None, lid))
    db.commit()
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
    db  = get_db()
    msg = db.execute("SELECT id,location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex  = db.execute("SELECT value FROM votes WHERE message_id=? AND user_id=?",
                     (mid, u['id'])).fetchone()
    if ex:
        if ex['value'] == value:
            db.execute("DELETE FROM votes WHERE message_id=? AND user_id=?", (mid, u['id']))
            db.execute("UPDATE messages SET score=score-? WHERE id=?", (value, mid))
            new_vote = 0
        else:
            db.execute("UPDATE votes SET value=? WHERE message_id=? AND user_id=?",
                       (value, mid, u['id']))
            db.execute("UPDATE messages SET score=score+? WHERE id=?", (value*2, mid))
            new_vote = value
    else:
        db.execute("INSERT INTO votes (message_id,user_id,value) VALUES (?,?,?)",
                   (mid, u['id'], value))
        db.execute("UPDATE messages SET score=score+? WHERE id=?", (value, mid))
        new_vote = value
    db.commit()
    score = db.execute("SELECT score FROM messages WHERE id=?", (mid,)).fetchone()['score']
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
    db  = get_db()
    msg = db.execute("SELECT id,location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex  = db.execute("SELECT id FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                     (mid, u['id'], emoji)).fetchone()
    if ex:
        db.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                   (mid, u['id'], emoji))
    else:
        db.execute("INSERT INTO reactions (message_id,user_id,emoji) VALUES (?,?,?)",
                   (mid, u['id'], emoji))
    db.commit()
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
    db = get_db()
    if not db.execute("SELECT id FROM messages WHERE id=?", (mid,)).fetchone():
        return jsonify({'error': 'Not found'}), 404
    if db.execute("SELECT id FROM reports WHERE message_id=? AND reporter_id=? AND status='pending'",
                  (mid, u['id'])).fetchone():
        return jsonify({'error': 'Already reported'}), 400
    db.execute("INSERT INTO reports (message_id,reporter_id,reason) VALUES (?,?,?)",
               (mid, u['id'], reason))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/admin/report/<int:rid>', methods=['POST'])
def resolve_report(rid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json(silent=True) or {}
    action = data.get('action')
    db     = get_db()
    r      = db.execute("SELECT message_id FROM reports WHERE id=?", (rid,)).fetchone()
    if not r: return jsonify({'error': 'Not found'}), 404
    if action == 'hide':
        db.execute("UPDATE messages SET hidden=1 WHERE id=?", (r['message_id'],))
    db.execute("UPDATE reports SET status='resolved',resolved_by=? WHERE id=?", (u['id'], rid))
    db.commit()
    return jsonify({'ok': True})

# ── API: notifications ────────────────────────────────────────────────────────
@app.route('/api/notifications')
def get_notifications():
    u = current_user()
    if not u: return jsonify([])
    rows = get_db().execute("""
        SELECT n.id,n.read,n.created_at,
               r.content as reply_content,ru.username as reply_username,ru.avatar_url as reply_avatar,
               m.content as original_content,l.place_name,l.id as location_id
        FROM notifications n
        JOIN messages r ON n.reply_id=r.id
        JOIN users ru   ON r.user_id=ru.id
        JOIN messages m ON n.message_id=m.id
        JOIN locations l ON m.location_id=l.id
        WHERE n.user_id=? ORDER BY n.created_at DESC LIMIT 30
    """, (u['id'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    get_db().execute("UPDATE notifications SET read=1 WHERE user_id=?", (u['id'],))
    get_db().commit()
    return jsonify({'ok': True})

@app.route('/api/notifications/unread-count')
def unread_count():
    u = current_user()
    if not u: return jsonify({'count': 0})
    c = get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0",
                          (u['id'],)).fetchone()[0]
    return jsonify({'count': c})

# ── API: translate ────────────────────────────────────────────────────────────
@app.route('/api/translate', methods=['POST'])
def translate():
    data   = request.get_json(silent=True) or {}
    text   = data.get('text', '').strip()[:500]
    target = data.get('target', 'en')
    if not text: return jsonify({'error': 'No text'}), 400
    try:
        res = http.post(f"{LIBRETRANSLATE_URL}/translate",
                        json={'q': text, 'source': 'auto', 'target': target, 'format': 'text'},
                        timeout=5)
        if res.ok:
            return jsonify({'translated': res.json().get('translatedText', ''), 'ok': True})
    except Exception:
        pass
    return jsonify({'error': 'Translation unavailable', 'ok': False}), 503

# ── Health / ping (UptimeRobot) ──────────────────────────────────────────────
@app.route('/health')
@app.route('/ping')
def health():
    """Lightweight endpoint for UptimeRobot to keep the instance warm."""
    try:
        db = get_db()
        db.execute("SELECT 1")
        return jsonify({'status': 'ok', 'db': 'ok'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

# ── Run ───────────────────────────────────────────────────────────────────────
# Always init DB (called by gunicorn workers too via module import)
import logging, sys
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    stream=sys.stdout,
)

init_db()

if __name__ == '__main__':
    if not DISCORD_CLIENT_ID:
        print("⚠️  DISCORD_CLIENT_ID not set", file=sys.stderr)
    print(f"ℹ️  Redirect URI: {DISCORD_REDIRECT_URI}", file=sys.stderr)
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=not IS_PRODUCTION,
        threaded=True,
        use_reloader=not IS_PRODUCTION,
    )
