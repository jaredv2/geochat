"""
GeoChat V3
==========
Setup:
  export DISCORD_CLIENT_ID=your_id
  export DISCORD_CLIENT_SECRET=your_secret
  export PORT=8000
  export DISCORD_REDIRECT_URI=http://localhost:8000/callback
  export ADMIN_DISCORD_ID=your_discord_user_id   # grants admin on login
  export LIBRETRANSLATE_URL=http://localhost:5001  # optional, for translation

Run:
  pip install flask requests
  python app.py
"""

import os, sqlite3, secrets, html, time, json, queue, threading
from flask import (Flask, render_template, request, redirect,
                   session, jsonify, g, Response, stream_with_context)
import requests as http
from dotenv import load_dotenv

app = Flask(__name__)

load_dotenv()

# ── Secret key ────────────────────────────────────────────────────────────────
_kf = os.path.join(os.path.dirname(__file__), '.secret_key')
if os.environ.get('SECRET_KEY'):        app.secret_key = os.environ['SECRET_KEY']
elif os.path.exists(_kf):
    with open(_kf) as f:                app.secret_key = f.read().strip()
else:
    k = secrets.token_hex(32); open(_kf,'w').write(k); app.secret_key = k

app.config.update(SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=False)

PORT                 = int(os.environ.get('PORT', 8000))
DISCORD_CLIENT_ID    = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET= os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI', f'http://localhost:{PORT}/callback')
DISCORD_API          = 'https://discord.com/api/v10'
ADMIN_DISCORD_ID     = os.environ.get('ADMIN_DISCORD_ID', '')
LIBRETRANSLATE_URL   = os.environ.get('LIBRETRANSLATE_URL', 'http://localhost:5001')
DATABASE             = os.path.join(os.path.dirname(__file__), 'database.db')

BADGE_DEFS = {
    'first_post':  {'label': 'First Post',   'icon': '✍️',  'desc': 'Posted your first message'},
    'explorer':    {'label': 'Explorer',      'icon': '🗺️',  'desc': 'Posted in 5 different locations'},
    'popular':     {'label': 'Popular',       'icon': '⭐',  'desc': 'Received 10 upvotes total'},
    'veteran':     {'label': 'Veteran',       'icon': '🏅',  'desc': 'Posted 25 messages'},
    'globe':       {'label': 'Globe Trotter', 'icon': '🌍',  'desc': 'Posted in 20 locations'},
    'loved':       {'label': 'Loved',         'icon': '❤️',  'desc': 'Received 50 upvotes total'},
}

RATE_LIMITS = {'post_message': (5,60), 'vote': (30,60), 'react': (20,60), 'report': (3,300)}

# ── SSE broker ────────────────────────────────────────────────────────────────
class SSEBroker:
    def __init__(self):
        self.listeners: dict[int, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, location_id: int) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with self._lock:
            self.listeners.setdefault(location_id, []).append(q)
        return q

    def unsubscribe(self, location_id: int, q: queue.Queue):
        with self._lock:
            lst = self.listeners.get(location_id, [])
            if q in lst: lst.remove(q)

    def publish(self, location_id: int, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            dead = []
            for q in self.listeners.get(location_id, []):
                try: q.put_nowait(payload)
                except queue.Full: dead.append(q)
            for q in dead:
                self.listeners[location_id].remove(q)

broker = SSEBroker()

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = get_db()
        with open(os.path.join(os.path.dirname(__file__), 'schema.sql')) as f:
            db.executescript(f.read())
        db.commit()
        # Migrate rate_limits FK if needed
        try:
            db.execute("INSERT INTO rate_limits (user_id, action) VALUES (0,'_test')")
            db.execute("DELETE FROM rate_limits WHERE user_id=0")
            db.commit()
        except Exception:
            db.execute("DROP TABLE IF EXISTS rate_limits")
            db.execute("""CREATE TABLE rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, action TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            db.commit()

# ── Helpers ───────────────────────────────────────────────────────────────────
def current_user():
    if 'user_id' not in session: return None
    return {'id': session['user_id'], 'username': session['username'],
            'avatar_url': session['avatar_url'], 'is_admin': session.get('is_admin', 0)}

def check_rate_limit(user_id, action):
    limit, window = RATE_LIMITS.get(action, (10, 60))
    db = get_db()
    db.execute("DELETE FROM rate_limits WHERE action=? AND created_at < datetime('now',? || ' seconds')",
               (action, f'-{window}'))
    count = db.execute("SELECT COUNT(*) FROM rate_limits WHERE user_id=? AND action=?",
                       (user_id, action)).fetchone()[0]
    if count >= limit: return False
    db.execute("INSERT INTO rate_limits (user_id, action) VALUES (?,?)", (user_id, action))
    db.commit()
    return True

def get_reactions(message_id, user_id=None):
    db = get_db()
    rows = db.execute("""SELECT emoji, COUNT(*) as count FROM reactions
                         WHERE message_id=? GROUP BY emoji ORDER BY count DESC""",
                      (message_id,)).fetchall()
    user_reacted = set()
    if user_id:
        ur = db.execute("SELECT emoji FROM reactions WHERE message_id=? AND user_id=?",
                        (message_id, user_id)).fetchall()
        user_reacted = {r['emoji'] for r in ur}
    return [{'emoji': r['emoji'], 'count': r['count'],
             'reacted': r['emoji'] in user_reacted} for r in rows]

def fmt_msg(row, uid=None, replies=None):
    d = dict(row)
    d['replies'] = replies or []
    d['reactions'] = get_reactions(d['id'], uid)
    d['user_vote'] = 0
    if uid:
        v = get_db().execute("SELECT value FROM votes WHERE message_id=? AND user_id=?",
                              (d['id'], uid)).fetchone()
        if v: d['user_vote'] = v['value']
    return d

def award_badges(user_id):
    """Check and award badges for a user."""
    db = get_db()
    u = db.execute("SELECT post_count FROM users WHERE id=?", (user_id,)).fetchone()
    if not u: return []
    earned = {r['badge'] for r in db.execute("SELECT badge FROM badges WHERE user_id=?", (user_id,)).fetchall()}
    new_badges = []

    def maybe_award(badge):
        if badge not in earned:
            db.execute("INSERT OR IGNORE INTO badges (user_id, badge) VALUES (?,?)", (user_id, badge))
            new_badges.append(badge)

    pc = u['post_count']
    if pc >= 1:  maybe_award('first_post')
    if pc >= 25: maybe_award('veteran')

    locs = db.execute("SELECT COUNT(DISTINCT location_id) FROM messages WHERE user_id=? AND parent_id IS NULL",
                      (user_id,)).fetchone()[0]
    if locs >= 5:  maybe_award('explorer')
    if locs >= 20: maybe_award('globe')

    total_votes = db.execute("SELECT COALESCE(SUM(score),0) FROM messages WHERE user_id=?",
                              (user_id,)).fetchone()[0]
    if total_votes >= 10: maybe_award('popular')
    if total_votes >= 50: maybe_award('loved')

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

    u = ur.json()
    did = u['id']
    username = u['username']
    avatar = (f"https://cdn.discordapp.com/avatars/{did}/{u['avatar']}.png"
              if u.get('avatar') else
              f"https://cdn.discordapp.com/embed/avatars/{int(did)%5}.png")
    is_admin = 1 if (ADMIN_DISCORD_ID and did == ADMIN_DISCORD_ID) else 0

    db.execute("""INSERT INTO users (discord_id, username, avatar_url, is_admin) VALUES (?,?,?,?)
                  ON CONFLICT(discord_id) DO UPDATE SET
                    username=excluded.username, avatar_url=excluded.avatar_url,
                    is_admin=MAX(is_admin, excluded.is_admin)""",
               (did, username, avatar, is_admin))
    db.commit()
    row = db.execute("SELECT id, is_admin FROM users WHERE discord_id=?", (did,)).fetchone()
    session.update(user_id=row['id'], username=username, avatar_url=avatar,
                   is_admin=row['is_admin'])
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', user=current_user())

@app.route('/profile')
@app.route('/profile/<int:uid>')
def profile(uid=None):
    if uid is None:
        u = current_user()
        if not u: return redirect('/login')
        uid = u['id']
    db = get_db()
    pu = db.execute("SELECT id,username,avatar_url,post_count,created_at FROM users WHERE id=?",
                    (uid,)).fetchone()
    if not pu: return "Not found", 404
    user_badges = db.execute("SELECT badge, earned_at FROM badges WHERE user_id=? ORDER BY earned_at",
                             (uid,)).fetchall()
    return render_template('profile.html', profile_user=dict(pu),
                           current_user=current_user(),
                           badges=[dict(b) for b in user_badges],
                           badge_defs=BADGE_DEFS)

@app.route('/leaderboard')
def leaderboard():
    db = get_db()
    top_locs = db.execute("""SELECT id, place_name, message_count, latitude, longitude
                              FROM locations ORDER BY message_count DESC LIMIT 20""").fetchall()
    top_users = db.execute("""SELECT u.id, u.username, u.avatar_url, u.post_count,
                                     COALESCE(SUM(m.score),0) as total_score,
                                     COUNT(DISTINCT m.location_id) as loc_count
                               FROM users u LEFT JOIN messages m ON m.user_id=u.id
                               GROUP BY u.id ORDER BY total_score DESC LIMIT 20""").fetchall()
    return render_template('leaderboard.html', top_locs=[dict(r) for r in top_locs],
                           top_users=[dict(r) for r in top_users],
                           current_user=current_user())

@app.route('/admin')
def admin():
    u = current_user()
    if not u or not u['is_admin']: return "Forbidden", 403
    db = get_db()
    reports = db.execute("""
        SELECT r.id, r.reason, r.status, r.created_at,
               m.id as msg_id, m.content, m.hidden,
               rep.username as reporter, rep.avatar_url as reporter_avatar,
               au.username as author, l.place_name
        FROM reports r
        JOIN messages m ON r.message_id = m.id
        JOIN users rep ON r.reporter_id = rep.id
        JOIN users au ON m.user_id = au.id
        JOIN locations l ON m.location_id = l.id
        WHERE r.status = 'pending'
        ORDER BY r.created_at DESC LIMIT 50
    """).fetchall()
    return render_template('admin.html', reports=[dict(r) for r in reports],
                           current_user=u)

# ── SSE ───────────────────────────────────────────────────────────────────────
@app.route('/api/stream/<int:lid>')
def stream(lid):
    q = broker.subscribe(lid)
    def generate():
        yield f"data: connected\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            broker.unsubscribe(lid, q)
    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})

# ── API: locations ────────────────────────────────────────────────────────────
@app.route('/api/locations/nearby')
def nearby():
    try:
        lat  = float(request.args['lat']);  lng  = float(request.args['lng'])
        dlat = float(request.args.get('dlat', 1.0))
        dlng = float(request.args.get('dlng', 1.0))
        radius_km = float(request.args.get('radius', 0))
    except (KeyError, ValueError): return jsonify({'error': 'invalid params'}), 400
    db = get_db()
    rows = db.execute("""
        SELECT id, latitude, longitude, place_name, message_count,
               last_user_avatar
        FROM locations
        WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
        ORDER BY message_count DESC LIMIT 300
    """, (lat-dlat, lat+dlat, lng-dlng, lng+dlng)).fetchall()
    results = []
    for r in rows:
        if radius_km > 0:
            import math
            dlat2 = math.radians(r['latitude'] - lat)
            dlng2 = math.radians(r['longitude'] - lng)
            a = math.sin(dlat2/2)**2 + math.cos(math.radians(lat)) * \
                math.cos(math.radians(r['latitude'])) * math.sin(dlng2/2)**2
            dist = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            if dist > radius_km: continue
        results.append(dict(r))
    return jsonify(results)

@app.route('/api/locations/heatmap')
def heatmap():
    rows = get_db().execute("""SELECT latitude, longitude, message_count FROM locations
                                WHERE message_count > 0 ORDER BY message_count DESC LIMIT 500""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/location', methods=['POST'])
def create_location():
    data = request.get_json(silent=True) or {}
    try: lat = float(data['latitude']); lng = float(data['longitude'])
    except (KeyError, ValueError, TypeError): return jsonify({'error': 'Invalid coords'}), 400
    place = html.escape(str(data.get('place_name','Unknown'))[:200])
    db = get_db()
    ex = db.execute("""SELECT id, place_name, message_count FROM locations
                       WHERE ABS(latitude-?) < 0.0001 AND ABS(longitude-?) < 0.0001 LIMIT 1""",
                    (lat, lng)).fetchone()
    if ex: return jsonify(dict(ex))
    cur = db.execute("INSERT INTO locations (latitude, longitude, place_name) VALUES (?,?,?)",
                     (lat, lng, place))
    db.commit()
    return jsonify({'id': cur.lastrowid, 'place_name': place, 'message_count': 0}), 201

@app.route('/api/location/<int:lid>')
def get_location(lid):
    row = get_db().execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))

@app.route('/api/location/<int:lid>', methods=['DELETE'])
def delete_empty_location(lid):
    db = get_db()
    row = db.execute("SELECT message_count FROM locations WHERE id=?", (lid,)).fetchone()
    if not row: return jsonify({'ok': True})
    if row['message_count'] > 0: return jsonify({'error': 'Has messages'}), 400
    db.execute("DELETE FROM locations WHERE id=? AND message_count=0", (lid,))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/search')
def search():
    q = request.args.get('q','').strip()
    if len(q) < 2: return jsonify([])
    rows = get_db().execute("""SELECT id, latitude, longitude, place_name, message_count
                                FROM locations WHERE place_name LIKE ?
                                ORDER BY message_count DESC LIMIT 10""",
                             (f'%{q}%',)).fetchall()
    return jsonify([dict(r) for r in rows])

# ── API: messages ─────────────────────────────────────────────────────────────
@app.route('/api/messages/<int:lid>')
def get_messages(lid):
    u = current_user(); uid = u['id'] if u else None
    db = get_db()
    top = db.execute("""
        SELECT m.id, m.content, m.score, m.edited, m.hidden, m.created_at, m.parent_id,
               u.id as user_id, u.username, u.avatar_url
        FROM messages m JOIN users u ON m.user_id=u.id
        WHERE m.location_id=? AND m.parent_id IS NULL AND m.hidden=0
        ORDER BY m.score DESC, m.created_at DESC LIMIT 100
    """, (lid,)).fetchall()
    result = []
    for msg in top:
        replies = db.execute("""
            SELECT m.id, m.content, m.score, m.edited, m.hidden, m.created_at, m.parent_id,
                   u.id as user_id, u.username, u.avatar_url
            FROM messages m JOIN users u ON m.user_id=u.id
            WHERE m.parent_id=? AND m.hidden=0 ORDER BY m.created_at ASC LIMIT 50
        """, (msg['id'],)).fetchall()
        result.append(fmt_msg(msg, uid, [fmt_msg(r, uid) for r in replies]))
    return jsonify(result)

@app.route('/api/messages/user/<int:uid>')
def user_messages(uid):
    rows = get_db().execute("""
        SELECT m.id, m.content, m.score, m.edited, m.created_at,
               l.place_name, l.id as location_id, l.latitude, l.longitude
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
    data = request.get_json(silent=True) or {}
    lid     = data.get('location_id')
    content = data.get('content','').strip()
    parent_id = data.get('parent_id')
    if not lid or not content: return jsonify({'error': 'location_id and content required'}), 400
    if len(content) > 500: return jsonify({'error': 'Max 500 chars'}), 400
    content = html.escape(content)
    db = get_db()
    if not db.execute("SELECT id FROM locations WHERE id=?", (lid,)).fetchone():
        return jsonify({'error': 'Location not found'}), 404
    parent = None
    if parent_id:
        parent = db.execute("SELECT id, user_id, location_id FROM messages WHERE id=?",
                             (parent_id,)).fetchone()
        if not parent or parent['location_id'] != lid:
            return jsonify({'error': 'Invalid parent'}), 400
    cur = db.execute("INSERT INTO messages (location_id, user_id, content, parent_id) VALUES (?,?,?,?)",
                     (lid, u['id'], content, parent_id))
    mid = cur.lastrowid
    db.execute("UPDATE locations SET message_count=message_count+1, last_user_id=?, last_user_avatar=? WHERE id=?",
               (u['id'], u['avatar_url'], lid))
    db.execute("UPDATE users SET post_count=post_count+1 WHERE id=?", (u['id'],))
    if parent and parent['user_id'] != u['id']:
        db.execute("INSERT INTO notifications (user_id, message_id, reply_id) VALUES (?,?,?)",
                   (parent['user_id'], parent_id, mid))
    db.commit()

    new_badges = award_badges(u['id'])
    row = db.execute("""SELECT m.id, m.content, m.score, m.edited, m.hidden, m.created_at, m.parent_id,
                               u.id as user_id, u.username, u.avatar_url
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
    data = request.get_json(silent=True) or {}
    content = data.get('content','').strip()
    if not content or len(content) > 500: return jsonify({'error': 'Invalid content'}), 400
    db = get_db()
    msg = db.execute("SELECT user_id, location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    db.execute("UPDATE messages SET content=?, edited=1 WHERE id=?", (html.escape(content), mid))
    db.commit()
    broker.publish(msg['location_id'], 'edit_message', {'id': mid, 'content': html.escape(content)})
    return jsonify({'ok': True})

@app.route('/api/message/<int:mid>', methods=['DELETE'])
def delete_message(mid):
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    msg = db.execute("SELECT user_id, location_id, parent_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    if msg['user_id'] != u['id']: return jsonify({'error': 'Forbidden'}), 403
    db.execute("DELETE FROM votes WHERE message_id=?", (mid,))
    db.execute("DELETE FROM reactions WHERE message_id=?", (mid,))
    db.execute("DELETE FROM notifications WHERE message_id=? OR reply_id=?", (mid, mid))
    db.execute("DELETE FROM messages WHERE id=?", (mid,))
    if not msg['parent_id']:
        db.execute("UPDATE locations SET message_count=MAX(0,message_count-1) WHERE id=?",
                   (msg['location_id'],))
    db.commit()
    broker.publish(msg['location_id'], 'delete_message', {'id': mid})
    return jsonify({'ok': True})

# ── API: votes ────────────────────────────────────────────────────────────────
@app.route('/api/vote', methods=['POST'])
def vote():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'vote'): return jsonify({'error': 'Rate limit'}), 429
    data = request.get_json(silent=True) or {}
    mid = data.get('message_id'); value = data.get('value')
    if mid is None or value not in (1,-1): return jsonify({'error': 'Invalid'}), 400
    db = get_db()
    msg = db.execute("SELECT id, location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex = db.execute("SELECT value FROM votes WHERE message_id=? AND user_id=?",
                    (mid, u['id'])).fetchone()
    if ex:
        if ex['value'] == value:
            db.execute("DELETE FROM votes WHERE message_id=? AND user_id=?", (mid, u['id']))
            db.execute("UPDATE messages SET score=score-? WHERE id=?", (value, mid))
            new_vote = 0
        else:
            db.execute("UPDATE votes SET value=? WHERE message_id=? AND user_id=?", (value, mid, u['id']))
            db.execute("UPDATE messages SET score=score+? WHERE id=?", (value*2, mid))
            new_vote = value
    else:
        db.execute("INSERT INTO votes (message_id, user_id, value) VALUES (?,?,?)", (mid, u['id'], value))
        db.execute("UPDATE messages SET score=score+? WHERE id=?", (value, mid))
        new_vote = value
    db.commit()
    score = db.execute("SELECT score FROM messages WHERE id=?", (mid,)).fetchone()['score']
    broker.publish(msg['location_id'], 'vote_update', {'id': mid, 'score': score})
    return jsonify({'score': score, 'user_vote': new_vote})

# ── API: reactions ────────────────────────────────────────────────────────────
ALLOWED_EMOJIS = {'👍','👎','❤️','😂','😮','😢','🔥','👏','🌍','📍'}

@app.route('/api/react', methods=['POST'])
def react():
    u = current_user()
    if not u: return jsonify({'error': 'Unauthorized'}), 401
    if not check_rate_limit(u['id'], 'react'): return jsonify({'error': 'Rate limit'}), 429
    data = request.get_json(silent=True) or {}
    mid = data.get('message_id'); emoji = data.get('emoji')
    if not mid or emoji not in ALLOWED_EMOJIS: return jsonify({'error': 'Invalid'}), 400
    db = get_db()
    msg = db.execute("SELECT id, location_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg: return jsonify({'error': 'Not found'}), 404
    ex = db.execute("SELECT id FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                    (mid, u['id'], emoji)).fetchone()
    if ex:
        db.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                   (mid, u['id'], emoji))
    else:
        db.execute("INSERT INTO reactions (message_id, user_id, emoji) VALUES (?,?,?)",
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
    data = request.get_json(silent=True) or {}
    mid = data.get('message_id'); reason = html.escape(data.get('reason','').strip()[:200])
    if not mid or not reason: return jsonify({'error': 'message_id and reason required'}), 400
    db = get_db()
    if not db.execute("SELECT id FROM messages WHERE id=?", (mid,)).fetchone():
        return jsonify({'error': 'Message not found'}), 404
    already = db.execute("SELECT id FROM reports WHERE message_id=? AND reporter_id=? AND status='pending'",
                          (mid, u['id'])).fetchone()
    if already: return jsonify({'error': 'Already reported'}), 400
    db.execute("INSERT INTO reports (message_id, reporter_id, reason) VALUES (?,?,?)",
               (mid, u['id'], reason))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/admin/report/<int:rid>', methods=['POST'])
def resolve_report(rid):
    u = current_user()
    if not u or not u['is_admin']: return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    db = get_db()
    report_row = db.execute("SELECT message_id FROM reports WHERE id=?", (rid,)).fetchone()
    if not report_row: return jsonify({'error': 'Not found'}), 404
    if action == 'hide':
        db.execute("UPDATE messages SET hidden=1 WHERE id=?", (report_row['message_id'],))
    db.execute("UPDATE reports SET status=?, resolved_by=? WHERE id=?",
               ('resolved', u['id'], rid))
    db.commit()
    return jsonify({'ok': True})

# ── API: notifications ────────────────────────────────────────────────────────
@app.route('/api/notifications')
def get_notifications():
    u = current_user()
    if not u: return jsonify([])
    rows = get_db().execute("""
        SELECT n.id, n.read, n.created_at,
               r.content as reply_content, ru.username as reply_username, ru.avatar_url as reply_avatar,
               m.content as original_content, l.place_name, l.id as location_id
        FROM notifications n
        JOIN messages r ON n.reply_id=r.id
        JOIN users ru ON r.user_id=ru.id
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
    data = request.get_json(silent=True) or {}
    text = data.get('text','').strip()[:500]
    target = data.get('target', 'en')
    if not text: return jsonify({'error': 'No text'}), 400
    try:
        res = http.post(f"{LIBRETRANSLATE_URL}/translate", json={
            'q': text, 'source': 'auto', 'target': target, 'format': 'text'
        }, timeout=5)
        if res.ok:
            return jsonify({'translated': res.json().get('translatedText',''), 'ok': True})
    except Exception:
        pass
    return jsonify({'error': 'Translation unavailable', 'ok': False}), 503

# ── API: leaderboard ──────────────────────────────────────────────────────────
@app.route('/api/leaderboard')
def leaderboard_api():
    db = get_db()
    locs = db.execute("""SELECT id, place_name, message_count, latitude, longitude
                          FROM locations ORDER BY message_count DESC LIMIT 10""").fetchall()
    users = db.execute("""SELECT u.id, u.username, u.avatar_url, u.post_count,
                                  COALESCE(SUM(m.score),0) as total_score
                           FROM users u LEFT JOIN messages m ON m.user_id=u.id
                           GROUP BY u.id ORDER BY total_score DESC LIMIT 10""").fetchall()
    return jsonify({'locations': [dict(r) for r in locs], 'users': [dict(r) for r in users]})

if __name__ == '__main__':
    if not DISCORD_CLIENT_ID: print("⚠️  DISCORD_CLIENT_ID not set")
    print(f"ℹ️  Redirect URI: {DISCORD_REDIRECT_URI}")
    init_db()
    app.run(debug=True, port=PORT, threaded=True)
