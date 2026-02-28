'use strict';
/* GeoChat V4 */

const EMOJIS = [
  '👍','👎','❤️','😂','😮','😢','🔥','👏','🌍','📍',
  '🎉','😡','🤔','👀','💯','🙏','⭐','✍️','🕊️','😎',
  '🤝','💬','🌱','⚡','🗺️'
];

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  locationId:      null,
  freshLocationId: null,   // empty marker only visible to creator
  markers:         {},     // id → L.Marker
  heatLayer:       null,
  heatOn:          false,
  radiusKm:        0,
  sse:             null,
  topMsg:          null,
  reportingMsgId:  null,
  emojiPickerEl:   null,
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
const $   = id => document.getElementById(id);
const panel        = $('panel');
const panelPlace   = $('panelPlace');
const panelCoords  = $('panelCoords');
const panelClose   = $('panelClose');
const msgList      = $('msgList');
const msgInput     = $('msgInput');
const postBtn      = $('postBtn');
const charCount    = $('charCount');
const mapHint      = $('mapHint');
const onlinePill   = $('onlinePill');
const onlineCount  = $('onlineCount');
const notifBtn     = $('notifBtn');
const notifPanel   = $('notifPanel');
const notifBadge   = $('notifBadge');
const notifList    = $('notifList');
const heatToggle   = $('heatmapToggle');
const radiusSlider = $('radiusSlider');
const radiusVal    = $('radiusVal');
const searchInput  = $('searchInput');
const searchRes    = $('searchResults');
const topPane      = $('topPane');
const reportOverlay= $('reportOverlay');
const badgeToast   = $('badgeToast');
const badgeToastMsg= $('badgeToastMsg');

const uid = window.CURRENT_USER ? window.CURRENT_USER.id : null;

// ── Map setup ─────────────────────────────────────────────────────────────────
const map = L.map('map', { center: [20, 0], zoom: 2 });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

const clusters = L.markerClusterGroup({
  maxClusterRadius: 55, showCoverageOnHover: false, zoomToBoundsOnClick: true,
  iconCreateFunction(c) {
    const n = c.getChildCount(), s = n < 10 ? 28 : n < 50 ? 34 : 40;
    return L.divIcon({
      html: `<div class="cluster-inner" style="width:${s}px;height:${s}px">${n}</div>`,
      className: 'custom-cluster', iconSize: [s, s],
    });
  },
});
map.addLayer(clusters);

// ── Marker icons ──────────────────────────────────────────────────────────────
const MSG_ZOOM_THRESHOLD = 12; // show top message snippet at/above this zoom

function markerIcon(loc) {
  const hasMsgs    = (loc.message_count || 0) > 0;
  const zoom       = map.getZoom();
  const showSnippet = hasMsgs && zoom >= MSG_ZOOM_THRESHOLD && loc.top_content;

  // Snippet bubble (shown at high zoom instead of count)
  const snippet = showSnippet
    ? `<div class="v4-snippet">${esc(String(loc.top_content).slice(0, 58))}${loc.top_content.length > 58 ? '…' : ''}</div>`
    : '';

  // Count badge (shown at low zoom when no snippet)
  const cnt = hasMsgs && !showSnippet
    ? `<div class="v4-count">${loc.message_count > 99 ? '99+' : loc.message_count}</div>`
    : '';

  return L.divIcon({
    html: `<div class="v4-marker">${snippet}${cnt}<div class="v4-pin${hasMsgs ? ' has-msgs' : ''}"></div><div class="v4-stem"></div></div>`,
    className: '', iconSize: [32, 36], iconAnchor: [7, 36],
  });
}

function addOrUpdateMarker(loc) {
  if (S.markers[loc.id]) {
    S.markers[loc.id]._locData = { ...S.markers[loc.id]._locData, ...loc };
    S.markers[loc.id].setIcon(markerIcon(S.markers[loc.id]._locData));
    return S.markers[loc.id];
  }
  const m = L.marker([loc.latitude, loc.longitude], { icon: markerIcon(loc) });
  m._locData = loc;
  m.on('click', () => {
    map.setView([loc.latitude, loc.longitude], Math.max(map.getZoom(), 14), { animate: true });
    openLocation(loc.latitude, loc.longitude, loc.place_name, loc.id);
  });
  S.markers[loc.id] = m;
  clusters.addLayer(m);
  return m;
}

// Refresh icons when zoom crosses snippet threshold
map.on('zoomend', () => {
  Object.values(S.markers).forEach(m => {
    if (m._locData) m.setIcon(markerIcon(m._locData));
  });
});


// ── Load nearby markers ───────────────────────────────────────────────────────
async function loadNearby() {
  const c = map.getCenter(), b = map.getBounds();
  const dlat = (b.getNorth() - b.getSouth()) / 2 + 0.5;
  const dlng = (b.getEast()  - b.getWest())  / 2 + 0.5;
  const params = new URLSearchParams({ lat: c.lat, lng: c.lng, dlat, dlng });
  if (S.radiusKm > 0) params.set('radius', S.radiusKm);
  if (uid) params.set('uid', uid);   // server hides other users' empty markers
  try {
    const locs = await fetchJSON(`/api/locations/nearby?${params}`);
    locs.forEach(addOrUpdateMarker);
  } catch(e) {}
}
map.on('moveend', loadNearby);
map.on('zoomend', loadNearby);
loadNearby();

// ── Radius filter ─────────────────────────────────────────────────────────────
function haversineKm(lat1, lng1, lat2, lng2) {
  const R = 6371, dLat = (lat2-lat1)*Math.PI/180, dLng = (lng2-lng1)*Math.PI/180;
  const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLng/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function applyRadiusVisibility() {
  const c = map.getCenter();
  Object.entries(S.markers).forEach(([id, m]) => {
    const pos = m.getLatLng();
    const dist = haversineKm(c.lat, c.lng, pos.lat, pos.lng);
    const inRange = S.radiusKm === 0 || dist <= S.radiusKm;
    // Add or remove from cluster group based on range
    if (inRange && !clusters.hasLayer(m)) {
      clusters.addLayer(m);
    } else if (!inRange && clusters.hasLayer(m)) {
      clusters.removeLayer(m);
    }
  });
}

if (radiusSlider) {
  radiusSlider.addEventListener('input', () => {
    S.radiusKm = parseInt(radiusSlider.value);
    if (radiusVal) radiusVal.textContent = S.radiusKm > 0 ? `${S.radiusKm}km` : '—';
    applyRadiusVisibility();
    loadNearby(); // also fetch any newly in-range markers
  });
}

// Re-apply visibility when map moves (center changes)
map.on('moveend', applyRadiusVisibility);

// ── Heatmap ───────────────────────────────────────────────────────────────────
if (heatToggle) {
  heatToggle.addEventListener('click', async () => {
    S.heatOn = !S.heatOn;
    heatToggle.classList.toggle('active', S.heatOn);
    if (S.heatOn) {
      try {
        // Always re-fetch fresh data
        const data = await fetchJSON('/api/locations/heatmap');
        if (!data || !data.length) { toast('No location data for heatmap yet'); S.heatOn = false; heatToggle.classList.remove('active'); return; }
        // Remove old layer if exists
        if (S.heatLayer) { try { map.removeLayer(S.heatLayer); } catch(e){} S.heatLayer = null; }
        const pts = data.map(d => [d.latitude, d.longitude, Math.min(d.message_count / 5, 1.0)]);
        if (typeof L.heatLayer !== 'function') {
          toast('Heatmap library not loaded — check your connection'); S.heatOn = false; heatToggle.classList.remove('active'); return;
        }
        S.heatLayer = L.heatLayer(pts, {
          radius: 35, blur: 22, maxZoom: 17, max: 1.0,
          gradient: { 0.0:'#f5f0e6', 0.3:'#c8bba8', 0.55:'#b8860b', 0.8:'#c0392b', 1.0:'#1a1612' }
        });
        S.heatLayer.addTo(map);
      } catch(e) {
        toast('Heatmap failed to load');
        S.heatOn = false; heatToggle.classList.remove('active');
      }
    } else {
      if (S.heatLayer) { try { map.removeLayer(S.heatLayer); } catch(e){} S.heatLayer = null; }
    }
  });
}

// ── My location ───────────────────────────────────────────────────────────────
const myLocBtn = $('myLocBtn');
if (myLocBtn) {
  myLocBtn.addEventListener('click', () => {
    if (!navigator.geolocation) { toast('Geolocation not supported'); return; }
    navigator.geolocation.getCurrentPosition(
      pos => map.setView([pos.coords.latitude, pos.coords.longitude], 14, { animate: true }),
      ()  => toast('Could not get your location')
    );
  });
}

// ── Map click ─────────────────────────────────────────────────────────────────
map.on('click', async e => {
  const { lat, lng } = e.latlng;
  if (mapHint) mapHint.classList.add('hidden');
  openPanel('Locating…', lat, lng);
  setMsgs('<div class="loading-state">Loading…</div>');

  const place = await reverseGeocode(lat, lng);
  panelPlace.textContent = place;

  try {
    const loc = await fetchJSON('/api/location', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latitude: lat, longitude: lng, place_name: place }),
    });
    S.locationId      = loc.id;
    // Only mark fresh if truly empty AND we are the creator
    S.freshLocationId = (loc.message_count || 0) === 0 ? loc.id : null;
    addOrUpdateMarker({ ...loc, latitude: lat, longitude: lng });
    await loadMsgs();
    connectSSE(loc.id);
  } catch(e) {
    setMsgs('<div class="empty-state">Could not load.</div>');
  }
});

async function openLocation(lat, lng, place, lid) {
  if (mapHint) mapHint.classList.add('hidden');
  openPanel(place || 'Loading…', lat, lng);
  S.locationId      = lid;
  S.freshLocationId = null;
  setMsgs('<div class="loading-state">Loading…</div>');
  if (!place) panelPlace.textContent = await reverseGeocode(lat, lng);
  await loadMsgs();
  connectSSE(lid);
  // Load online count
  try {
    const d = await fetchJSON(`/api/location/${lid}/online`);
    showOnline(d.count);
  } catch(e) {}
}

// ── Reverse geocode ───────────────────────────────────────────────────────────
async function reverseGeocode(lat, lng) {
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&addressdetails=1&namedetails=1&zoom=18&accept-language=en`;
    const d   = await (await fetch(url, { headers: { 'User-Agent': 'GeoChat/4.0' } })).json();
    if (!d || d.error) return `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
    const a = d.address || {}, nd = d.namedetails || {};
    const s = a.tourism||a.leisure||a.amenity||a.building||a.shop||a.historic||a.natural||
              a.man_made||nd.name||a.road||a.pedestrian||a.path||a.footway||
              a.neighbourhood||a.suburb||a.quarter||a.city_district||a.district||
              a.borough||a.town||a.village||a.hamlet||a.city||a.county||a.state;
    if (!s) return d.display_name.split(',').slice(0, 2).join(',').trim();
    const city = a.city || a.town || a.village || '';
    let name = (city && city.toLowerCase() !== s.toLowerCase()) ? `${s}, ${city}` : s;
    // Auto-translate non-latin place names to English
    if (needsTranslation(name)) {
      try {
        const tr = await fetchJSON('/api/place/translate', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ name }),
        });
        if (tr.translated && tr.name) name = tr.name;
      } catch(e) {}
    }
    return name;
  } catch(e) { return `${lat.toFixed(4)}, ${lng.toFixed(4)}`; }
}

function needsTranslation(text) {
  // Check if string has non-ASCII characters that are likely non-Latin script
  for (const ch of text) {
    const code = ch.codePointAt(0);
    // Arabic, Hebrew, CJK, Cyrillic, Thai, Devanagari, etc.
    if (code > 127 && ch !== ',' && ch !== '.' && ch !== '-' && ch !== ' ') return true;
  }
  return false;
}

// ── Panel open / close ────────────────────────────────────────────────────────
function openPanel(place, lat, lng) {
  panelPlace.textContent = place;
  panelCoords.textContent = `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`;
  panel.classList.add('open');
  switchTab('chat');
  if (onlinePill) onlinePill.style.display = 'none';
}

panelClose.addEventListener('click', async () => {
  panel.classList.remove('open');
  disconnectSSE();
  closeEmojiPicker();
  const freshId = S.freshLocationId;
  if (freshId) {
    if (S.markers[freshId]) { clusters.removeLayer(S.markers[freshId]); delete S.markers[freshId]; }
    fetch(`/api/location/${freshId}`, { method: 'DELETE' }).catch(() => {});
    S.freshLocationId = null;
  }
  S.locationId = null;
  if (onlinePill) onlinePill.style.display = 'none';
});

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  if (name === 'top') renderTop();
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(lid) {
  disconnectSSE();
  S.sse = new EventSource(`/api/stream/${lid}`);

  S.sse.addEventListener('new_message', e => {
    const msg = JSON.parse(e.data);
    if (uid && msg.user_id === uid) return; // already shown locally
    const div = document.createElement('div');
    div.innerHTML = renderMsg(msg);
    msgList.insertAdjacentHTML('afterbegin', div.innerHTML);
    S.freshLocationId = null;
    if (!S.topMsg || msg.score >= (S.topMsg.score || 0)) S.topMsg = msg;
    refreshMarkerCount();
  });

  S.sse.addEventListener('vote_update', e => {
    const { id, score } = JSON.parse(e.data);
    const el = $(`score-${id}`);
    if (el) { el.textContent = score; el.className = `vote-score ${score>0?'pos':score<0?'neg':''}`; }
  });

  S.sse.addEventListener('reaction_update', e => {
    const { id, reactions } = JSON.parse(e.data);
    const el = $(`reactions-${id}`);
    if (el) el.innerHTML = reactionsHtml(id, reactions);
  });

  S.sse.addEventListener('edit_message', e => {
    const { id, content } = JSON.parse(e.data);
    const el = $(`msgText-${id}`);
    if (el) el.textContent = content;
  });

  S.sse.addEventListener('delete_message', e => {
    const { id } = JSON.parse(e.data);
    $(`msg-${id}`)?.remove();
  });

  S.sse.addEventListener('location_deleted', e => {
    const { location_id } = JSON.parse(e.data);
    if (S.markers[location_id]) {
      clusters.removeLayer(S.markers[location_id]);
      delete S.markers[location_id];
    }
    panel.classList.remove('open');
    disconnectSSE();
    S.locationId = null;
    toast('This location was removed — all messages deleted');
  });

  S.sse.addEventListener('online_count', e => {
    const { count } = JSON.parse(e.data);
    showOnline(count);
  });

  S.sse.addEventListener('badge_earned', e => {
    const { username, badges } = JSON.parse(e.data);
    badges.forEach(b => showBadge(`${username} earned ${b.icon} ${b.label}!`));
  });
}

function disconnectSSE() {
  if (S.sse) { S.sse.close(); S.sse = null; }
}

function showOnline(count) {
  if (!onlinePill || !onlineCount) return;
  onlineCount.textContent = count;
  onlinePill.style.display = count > 0 ? 'flex' : 'none';
}

// ── Messages ──────────────────────────────────────────────────────────────────
function setMsgs(h) { if (msgList) msgList.innerHTML = h; }

async function loadMsgs() {
  if (!S.locationId) return;
  try {
    const msgs = await fetchJSON(`/api/messages/${S.locationId}`);
    if (!msgs.length) {
      setMsgs('<div class="empty-state">No messages yet — be the first.</div>');
      S.topMsg = null; renderTop(); return;
    }
    setMsgs(msgs.map(m => renderMsg(m)).join(''));
    S.topMsg = msgs[0]; renderTop(msgs[0]);
  } catch(e) { setMsgs('<div class="empty-state">Could not load.</div>'); }
}

function renderMsg(m, isReply = false) {
  const mine = uid && uid === m.user_id;
  const sc   = m.score > 0 ? 'pos' : m.score < 0 ? 'neg' : '';
  const reacts = reactionsHtml(m.id, m.reactions || []);

  const repliesHtml = (!isReply && m.replies?.length)
    ? `<div class="replies">${m.replies.map(r => renderMsg(r, true)).join('')}</div>` : '';

  const replyForm = !isReply ? `
    <div class="reply-form" id="rf-${m.id}">
      <textarea class="reply-ta" id="ri-${m.id}" placeholder="Reply…" maxlength="500" rows="2"></textarea>
      <div class="reply-btns">
        <button class="btn-xs bx-ghost" onclick="cancelReply(${m.id})">Cancel</button>
        <button class="btn-xs bx-primary" onclick="submitReply(${m.id})">Reply</button>
      </div>
    </div>` : '';

  return `
    <div class="${isReply ? 'reply-card' : 'msg-card'}" id="msg-${m.id}">
      <div class="msg-row">
        <img class="msg-av" src="${esc(m.avatar_url||'')}" alt=""
             onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'"
             onclick="nav('/profile/${m.user_id}')">
        <div class="msg-body">
          <div class="msg-meta">
            <span class="msg-name" onclick="nav('/profile/${m.user_id}')">${esc(m.username)}</span>
            <span class="msg-time">${relTime(m.created_at)}</span>
            ${m.edited ? '<span class="edited-tag">edited</span>' : ''}
          </div>
          <div class="msg-text" id="msgText-${m.id}">${esc(m.content)}</div>
          <div class="edit-form" id="ef-${m.id}">
            <textarea class="edit-ta" id="ei-${m.id}" maxlength="500">${esc(m.content)}</textarea>
            <div class="edit-btns">
              <button class="btn-xs bx-ghost" onclick="cancelEdit(${m.id})">Cancel</button>
              <button class="btn-xs bx-primary" onclick="submitEdit(${m.id})">Save</button>
            </div>
          </div>
          <div class="reactions" id="reactions-${m.id}">${reacts}</div>
        </div>
      </div>
      <div class="msg-actions">
        <div class="vote-row">
          <button class="vote-btn up ${m.user_vote===1?'active':''}" onclick="doVote(${m.id},1)">▲</button>
          <span class="vote-score ${sc}" id="score-${m.id}">${m.score}</span>
          <button class="vote-btn down ${m.user_vote===-1?'active':''}" onclick="doVote(${m.id},-1)">▼</button>
        </div>
        ${!isReply ? `<button class="act-btn" onclick="toggleReply(${m.id})">↩ Reply</button>` : ''}
        <button class="act-btn" onclick="doTranslate(${m.id})">⟳ Translate</button>
        <button class="act-btn danger" onclick="openReport(${m.id})">⚑ Report</button>
        ${mine ? `
          <button class="act-btn" onclick="startEdit(${m.id})">Edit</button>
          <button class="act-btn danger" onclick="deleteMsg(${m.id})">Delete</button>` : ''}
      </div>
      ${replyForm}
      ${repliesHtml}
    </div>`;
}

function reactionsHtml(msgId, reactions) {
  const btns = (reactions || []).map(r =>
    `<button class="react-btn ${r.reacted?'mine':''}" onclick="doReact(${msgId},'${r.emoji}')">
       ${r.emoji}<span class="react-count">${r.count}</span>
     </button>`).join('');
  return btns + `<button class="add-react" onclick="openEmoji(event,${msgId})">+</button>`;
}

// ── Top message ───────────────────────────────────────────────────────────────
function renderTop(m) {
  if (!topPane) return;
  if (m !== undefined) S.topMsg = m;
  const t = S.topMsg;
  if (!t) { topPane.innerHTML = '<div class="top-empty">No messages at this location yet.</div>'; return; }
  topPane.innerHTML = `
    <div class="top-card">
      <div class="top-user">
        <img class="top-av" src="${esc(t.avatar_url||'')}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
        <span class="top-name">${esc(t.username)}</span>
        <span class="top-score">▲ ${t.score}</span>
      </div>
      <div class="top-text">${esc(t.content)}</div>
      <div class="msg-time" style="margin-top:8px">${relTime(t.created_at)}</div>
    </div>`;
}

// ── Emoji picker ──────────────────────────────────────────────────────────────
function openEmoji(event, msgId) {
  closeEmojiPicker();
  const picker = document.createElement('div');
  picker.className = 'emoji-picker';
  EMOJIS.forEach(emoji => {
    const b = document.createElement('button');
    b.className = 'ep-btn'; b.textContent = emoji;
    b.onclick = () => { doReact(msgId, emoji); closeEmojiPicker(); };
    picker.appendChild(b);
  });
  const rect = event.target.getBoundingClientRect();
  picker.style.top  = `${Math.min(rect.bottom + 4, window.innerHeight - 160)}px`;
  picker.style.left = `${Math.min(rect.left, window.innerWidth - 250)}px`;
  document.body.appendChild(picker);
  S.emojiPickerEl = picker;
  setTimeout(() => document.addEventListener('click', closeEmojiPicker), 0);
}
function closeEmojiPicker() {
  S.emojiPickerEl?.remove(); S.emojiPickerEl = null;
  document.removeEventListener('click', closeEmojiPicker);
}

// ── Reactions ─────────────────────────────────────────────────────────────────
async function doReact(msgId, emoji) {
  if (!uid) { location.href = '/login'; return; }
  try {
    const d = await fetchJSON('/api/react', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ message_id: msgId, emoji }),
    });
    const el = $(`reactions-${msgId}`);
    if (el) el.innerHTML = reactionsHtml(msgId, d.reactions);
  } catch(e) { toast('React failed'); }
}

// ── Voting ────────────────────────────────────────────────────────────────────
async function doVote(msgId, value) {
  if (!uid) { location.href = '/login'; return; }
  try {
    const res = await fetch('/api/vote', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ message_id: msgId, value }),
    });
    if (res.status === 429) { toast('Rate limit — slow down'); return; }
    const d = await res.json();
    const el = $(`score-${msgId}`);
    if (el) { el.textContent = d.score; el.className = `vote-score ${d.score>0?'pos':d.score<0?'neg':''}`; }
    const card = $(`msg-${msgId}`);
    if (card) {
      card.querySelector('.vote-btn.up').classList.toggle('active', d.user_vote === 1);
      card.querySelector('.vote-btn.down').classList.toggle('active', d.user_vote === -1);
    }
  } catch(e) { toast('Vote failed'); }
}

// ── Translate ─────────────────────────────────────────────────────────────────
async function doTranslate(msgId) {
  const el = $(`msgText-${msgId}`);
  if (!el) return;
  if (el.dataset.translated) {
    el.textContent = el.dataset.orig;
    delete el.dataset.translated; delete el.dataset.orig;
    el.nextElementSibling?.classList.contains('trans-tag') && el.nextElementSibling.remove();
    return;
  }
  const orig = el.textContent;
  const lang = navigator.language.split('-')[0] || 'en';
  try {
    const d = await fetchJSON('/api/translate', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text: orig, target: lang }),
    });
    if (d.ok && d.translated) {
      el.dataset.orig = orig; el.dataset.translated = '1';
      el.textContent = d.translated;
      const tag = document.createElement('span');
      tag.className = 'trans-tag'; tag.textContent = '[translated]';
      el.insertAdjacentElement('afterend', tag);
    } else { toast('Translation unavailable — LibreTranslate not running'); }
  } catch(e) { toast('Translation failed'); }
}

// ── Replies ───────────────────────────────────────────────────────────────────
function toggleReply(id) {
  document.querySelectorAll('.reply-form.open').forEach(f => f.classList.remove('open'));
  $(`rf-${id}`)?.classList.add('open');
  $(`ri-${id}`)?.focus();
}
function cancelReply(id) { $(`rf-${id}`)?.classList.remove('open'); }
async function submitReply(parentId) {
  if (!uid) { location.href = '/login'; return; }
  const content = $(`ri-${parentId}`)?.value.trim();
  if (!content) return;
  try {
    const res = await fetch('/api/message', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ location_id: S.locationId, content, parent_id: parentId }),
    });
    if (res.status === 429) { toast('Rate limit — max 5 per minute'); return; }
    if (!res.ok) { toast((await res.json()).error); return; }
    cancelReply(parentId);
    await loadMsgs();
  } catch(e) { toast('Reply failed'); }
}

// ── Edit / Delete ─────────────────────────────────────────────────────────────
function startEdit(id) {
  $(`msgText-${id}`)?.classList.add('is-editing');
  $(`ef-${id}`)?.classList.add('open');
  $(`ei-${id}`)?.focus();
}
function cancelEdit(id) {
  $(`msgText-${id}`)?.classList.remove('is-editing');
  $(`ef-${id}`)?.classList.remove('open');
}
async function submitEdit(id) {
  const content = $(`ei-${id}`)?.value.trim();
  if (!content) return;
  const res = await fetch(`/api/message/${id}`, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ content }),
  });
  if (!res.ok) { toast((await res.json()).error); return; }
  cancelEdit(id);
  await loadMsgs();
}
async function deleteMsg(id) {
  if (!confirm('Delete this message?')) return;
  const res = await fetch(`/api/message/${id}`, { method: 'DELETE' });
  if (!res.ok) { toast((await res.json()).error); return; }
  const data = await res.json();
  if (data.location_deleted) {
    // Remove the marker entirely from map
    const lid = data.location_id;
    if (S.markers[lid]) {
      clusters.removeLayer(S.markers[lid]);
      delete S.markers[lid];
    }
    // Close the panel since this location no longer exists
    panel.classList.remove('open');
    disconnectSSE();
    S.locationId = null;
    S.freshLocationId = null;
    toast('Location removed — no messages remain');
  } else {
    await loadMsgs();
    await refreshMarkerCount();
  }
}

// ── Post ──────────────────────────────────────────────────────────────────────
if (msgInput) {
  msgInput.addEventListener('input', () => {
    const l = msgInput.value.length;
    if (charCount) { charCount.textContent = l; charCount.parentElement.classList.toggle('warn', l > 450); }
    msgInput.style.height = 'auto';
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
  });
  msgInput.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doPost(); });
}
if (postBtn) postBtn.addEventListener('click', doPost);

async function doPost() {
  const content = msgInput?.value.trim();
  if (!content || !S.locationId) return;
  postBtn.disabled = true; postBtn.textContent = '…';
  try {
    const res = await fetch('/api/message', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ location_id: S.locationId, content }),
    });
    if (res.status === 401) { location.href = '/login'; return; }
    if (res.status === 429) { toast('Rate limit — max 5 per minute'); return; }
    if (!res.ok) { toast((await res.json()).error); return; }
    const data = await res.json();
    msgInput.value = ''; msgInput.style.height = 'auto';
    if (charCount) charCount.textContent = '0';
    S.freshLocationId = null;
    data.new_badges?.forEach(b => showBadge(`Badge unlocked: ${b}`));
    await loadMsgs();
    await refreshMarkerCount();
  } catch(e) { toast('Post failed'); }
  finally { postBtn.disabled = false; postBtn.textContent = 'SEND ↵'; }
}

async function refreshMarkerCount() {
  if (!S.locationId) return;
  try {
    const loc = await fetchJSON(`/api/location/${S.locationId}`);
    if (S.markers[S.locationId]) {
      S.markers[S.locationId]._locData = loc;
      S.markers[S.locationId].setIcon(markerIcon(loc));
    }
  } catch(e) {}
}

// ── Report ────────────────────────────────────────────────────────────────────
function openReport(msgId) {
  if (!uid) { location.href = '/login'; return; }
  S.reportingMsgId = msgId;
  reportOverlay.style.display = 'flex';
  $('reportReason').value = '';
}
function closeReport() { reportOverlay.style.display = 'none'; S.reportingMsgId = null; }
async function submitReport() {
  const reason = $('reportReason')?.value.trim();
  if (!reason) { toast('Please enter a reason'); return; }
  try {
    const res = await fetch('/api/report', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ message_id: S.reportingMsgId, reason }),
    });
    if (!res.ok) { toast((await res.json()).error); return; }
    toast('Reported — admins will review');
    closeReport();
  } catch(e) { toast('Report failed'); }
}

// ── Notifications ─────────────────────────────────────────────────────────────
if (notifBtn) {
  notifBtn.addEventListener('click', e => {
    e.stopPropagation();
    notifPanel?.classList.toggle('open');
    if (notifPanel?.classList.contains('open')) loadNotifs();
  });
}
$('markAllRead')?.addEventListener('click', async () => {
  await fetch('/api/notifications/read', { method: 'POST' });
  if (notifBadge) notifBadge.style.display = 'none';
  loadNotifs();
});
document.addEventListener('click', e => {
  if (!notifPanel?.contains(e.target) && e.target !== notifBtn) notifPanel?.classList.remove('open');
});

async function loadNotifs() {
  const notifs = await fetchJSON('/api/notifications').catch(() => []);
  if (!notifList) return;
  if (!notifs.length) { notifList.innerHTML = '<div class="empty-note">nothing yet</div>'; return; }
  notifList.innerHTML = notifs.map(n => `
    <div class="notif-item ${n.read?'':'unread'}" onclick="jumpTo(${n.location_id})">
      <div><span class="notif-user">${esc(n.reply_username)}</span> replied to your message</div>
      <div class="notif-preview">"${esc((n.reply_content||'').slice(0,55))}…"</div>
      <div class="notif-place">▸ ${esc(n.place_name||'')} · ${relTime(n.created_at)}</div>
    </div>`).join('');
}

async function pollUnread() {
  if (!uid) return;
  const { count } = await fetchJSON('/api/notifications/unread-count').catch(() => ({ count: 0 }));
  if (notifBadge) { notifBadge.textContent = count; notifBadge.style.display = count > 0 ? '' : 'none'; }
}
if (uid) { pollUnread(); setInterval(pollUnread, 30000); }

function jumpTo(lid) {
  notifPanel?.classList.remove('open');
  fetchJSON(`/api/location/${lid}`).then(loc => {
    map.setView([loc.latitude, loc.longitude], 15, { animate: true });
    openLocation(loc.latitude, loc.longitude, loc.place_name, lid);
  });
}

// ── Search (local GeoChat locations + world Nominatim) ────────────────────────
let _st;
searchInput?.addEventListener('input', () => { clearTimeout(_st); _st = setTimeout(doSearch, 300); });
searchInput?.addEventListener('keydown', e => { if (e.key === 'Escape') closeSearch(); });
document.addEventListener('click', e => {
  if (!searchInput?.contains(e.target) && !searchRes?.contains(e.target)) closeSearch();
});

async function doSearch() {
  const q = searchInput.value.trim();
  if (q.length < 2) { closeSearch(); return; }

  // Run local DB search and world Nominatim in parallel
  const [local, world] = await Promise.all([
    fetchJSON(`/api/search?q=${encodeURIComponent(q)}`).catch(() => []),
    fetchJSON(`/api/search/world?q=${encodeURIComponent(q)}`).catch(() => []),
  ]);

  if (!local.length && !world.length) { closeSearch(); return; }

  let html = '';

  if (local.length) {
    html += `<div class="sri-section">IN GEOCHAT</div>`;
    html += local.map(l =>
      `<div class="search-result-item sri-local" onclick="pickSearch(${l.id},${l.latitude},${l.longitude},'${esc(l.place_name||'')}')">
         <span class="sri-name">${esc(l.place_name||'Unknown')}</span>
         <span class="sri-count">${l.message_count} msg</span>
       </div>`).join('');
  }

  if (world.length) {
    html += `<div class="sri-section">WORLD</div>`;
    html += world.map(w =>
      `<div class="search-result-item sri-world" onclick="flyToWorld(${w.lat},${w.lng},'${esc(w.label||'')}')">
         <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="flex-shrink:0;opacity:.5"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
         <span class="sri-name">${esc(w.label||'Unknown')}</span>
       </div>`).join('');
  }

  searchRes.innerHTML = html;
  searchRes.classList.add('open');
}

function closeSearch() { searchRes?.classList.remove('open'); }

function pickSearch(lid, lat, lng, name) {
  closeSearch(); if(searchInput) searchInput.value = name;
  map.setView([lat, lng], 15, { animate: true });
  openLocation(lat, lng, name, lid);
}

function flyToWorld(lat, lng, label) {
  closeSearch(); if(searchInput) searchInput.value = label;
  map.setView([lat, lng], 14, { animate: true });
  // Don't auto-open a panel — just fly the map. User can click to start a chat.
  toast(`Flew to: ${label}`);
}

// ── ?loc= param ───────────────────────────────────────────────────────────────
(async () => {
  const lid = new URLSearchParams(location.search).get('loc');
  if (!lid) return;
  try {
    const loc = await fetchJSON(`/api/location/${lid}`);
    map.setView([loc.latitude, loc.longitude], 14, { animate: true });
    openLocation(loc.latitude, loc.longitude, loc.place_name, parseInt(lid));
  } catch(e) {}
})();

// ── Utils ─────────────────────────────────────────────────────────────────────
async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function relTime(ts) {
  const d = new Date(ts && ts.endsWith('Z') ? ts : ts + 'Z');
  const diff = (Date.now() - d) / 1000;
  if (isNaN(diff)) return '';
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
}
function nav(url) { location.href = url; }
function toast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position:'fixed', bottom:'68px', left:'50%', transform:'translateX(-50%)',
    background:'var(--ink)', color:'var(--parchment)', padding:'8px 16px',
    fontFamily:"'IBM Plex Mono',monospace", fontSize:'0.72rem', zIndex:'9999',
    borderLeft:'3px solid var(--red)', boxShadow:'var(--shadow-md)',
    pointerEvents:'none', whiteSpace:'nowrap',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2800);
}
function showBadge(msg) {
  if (!badgeToast || !badgeToastMsg) return;
  badgeToastMsg.textContent = msg;
  badgeToast.style.display = 'block';
  setTimeout(() => { badgeToast.style.display = 'none'; }, 4000);
}

// Expose to inline handlers
Object.assign(window, {
  doVote, doReact, openEmoji, doTranslate,
  toggleReply, cancelReply, submitReply,
  startEdit, cancelEdit, submitEdit, deleteMsg,
  openReport, closeReport, submitReport,
  jumpTo, pickSearch, flyToWorld, nav,
});
