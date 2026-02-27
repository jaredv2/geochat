'use strict';
/* GeoChat V3 */

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  locationId: null,
  freshLocationId: null,
  markers: {},
  heatmapLayer: null,
  heatmapOn: false,
  replyingTo: null,
  sse: null,
  radiusKm: 0,
  reportingMsgId: null,
  topMsg: null,
};

const EMOJIS = ['👍','👎','❤️','😂','😮','😢','🔥','👏','🌍','📍'];

// ── DOM ───────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const panel          = $('panel');
const panelPlace     = $('panelPlace');
const panelCoords    = $('panelCoords');
const panelClose     = $('panelClose');
const messagesList   = $('messagesList');
const msgInput       = $('msgInput');
const postBtn        = $('postBtn');
const charCount      = $('charCount');
const mapHint        = $('mapHint');
const liveChip       = $('livechip');
const liveIndicator  = $('liveIndicator');
const notifBtn       = $('notifBtn');
const notifPanel     = $('notifPanel');
const notifBadge     = $('notifBadge');
const notifList      = $('notifList');
const heatmapToggle  = $('heatmapToggle');
const searchInput    = $('searchInput');
const searchResults  = $('searchResults');
const topWrap        = $('topWrap');
const radiusSlider   = $('radiusSlider');
const radiusVal      = $('radiusVal');
const badgeToast     = $('badgeToast');
const badgeToastInner= $('badgeToastInner');
const reportOverlay  = $('reportOverlay');

// ── Map ───────────────────────────────────────────────────────────────────────
const map = L.map('map', { center: [20, 0], zoom: 2 });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

const clusterGroup = L.markerClusterGroup({
  maxClusterRadius: 55, showCoverageOnHover: false, zoomToBoundsOnClick: true,
  iconCreateFunction(cluster) {
    const n = cluster.getChildCount();
    const sz = n < 10 ? 30 : n < 50 ? 36 : 42;
    return L.divIcon({
      html: `<div class="cluster-inner" style="width:${sz}px;height:${sz}px">${n}</div>`,
      className: 'custom-cluster', iconSize: [sz, sz],
    });
  }
});
map.addLayer(clusterGroup);

function makeMarkerIcon(loc) {
  const hasMsgs = (loc.message_count || 0) > 0;
  const avatarHtml = loc.last_user_avatar
    ? `<img src="${esc(loc.last_user_avatar)}" class="v3-uavatar" onerror="this.style.display='none'">`
    : '';
  return L.divIcon({
    html: `<div class="v3-marker">
             ${avatarHtml}
             ${hasMsgs ? `<div class="v3-count">${loc.message_count > 99 ? '99+' : loc.message_count}</div>` : ''}
             <div class="v3-pin ${hasMsgs ? 'active-loc' : ''}"></div>
             <div class="v3-stem ${hasMsgs ? 'active-loc' : ''}"></div>
           </div>`,
    className: '', iconSize: [32, 32], iconAnchor: [5, 24],
  });
}

function addOrUpdateMarker(loc) {
  if (S.markers[loc.id]) {
    S.markers[loc.id].setIcon(makeMarkerIcon(loc));
    return S.markers[loc.id];
  }
  const m = L.marker([loc.latitude, loc.longitude], { icon: makeMarkerIcon(loc) });
  m.on('click', () => openLocation(loc.latitude, loc.longitude, loc.place_name, loc.id));
  S.markers[loc.id] = m;
  clusterGroup.addLayer(m);
  return m;
}

// ── Load nearby markers ───────────────────────────────────────────────────────
async function loadNearby() {
  const c = map.getCenter(), b = map.getBounds();
  const dlat = (b.getNorth() - b.getSouth()) / 2 + 0.5;
  const dlng = (b.getEast()  - b.getWest())  / 2 + 0.5;
  const params = new URLSearchParams({ lat: c.lat, lng: c.lng, dlat, dlng });
  if (S.radiusKm > 0) params.set('radius', S.radiusKm);
  try {
    const res = await fetch(`/api/locations/nearby?${params}`);
    const locs = await res.json();
    locs.forEach(addOrUpdateMarker);
  } catch(e) {}
}
map.on('moveend', loadNearby);
map.on('zoomend', loadNearby);
loadNearby();

// ── Radius filter ─────────────────────────────────────────────────────────────
if (radiusSlider) {
  radiusSlider.addEventListener('input', () => {
    S.radiusKm = parseInt(radiusSlider.value);
    radiusVal.textContent = S.radiusKm > 0 ? `${S.radiusKm}km` : 'off';
    // Clear and reload markers
    clusterGroup.clearLayers();
    Object.keys(S.markers).forEach(k => { if (parseInt(k) !== S.locationId) delete S.markers[k]; });
    loadNearby();
  });
}

// ── Heatmap ───────────────────────────────────────────────────────────────────
if (heatmapToggle) {
  heatmapToggle.addEventListener('click', async () => {
    S.heatmapOn = !S.heatmapOn;
    heatmapToggle.classList.toggle('active', S.heatmapOn);
    if (S.heatmapOn) {
      if (!S.heatmapLayer) {
        const res = await fetch('/api/locations/heatmap');
        const data = await res.json();
        const pts = data.map(d => [d.latitude, d.longitude, Math.min(d.message_count / 8, 1)]);
        S.heatmapLayer = L.heatLayer(pts, {
          radius: 30, blur: 20, maxZoom: 14,
          gradient: { 0.2: '#001f3f', 0.5: '#00d4ff', 0.8: '#00ff8c', 1: '#ffb800' }
        });
      }
      S.heatmapLayer.addTo(map);
    } else if (S.heatmapLayer) {
      map.removeLayer(S.heatmapLayer);
    }
  });
}

// ── Map click ─────────────────────────────────────────────────────────────────
map.on('click', async e => {
  const { lat, lng } = e.latlng;
  mapHint.classList.add('hidden');
  openPanel('locating…', lat, lng);
  setMessages('<div class="loading-mono">loading…</div>');

  const placeName = await reverseGeocode(lat, lng);
  panelPlace.textContent = placeName;

  try {
    const res = await fetch('/api/location', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latitude: lat, longitude: lng, place_name: placeName }),
    });
    const loc = await res.json();
    S.locationId = loc.id;
    S.freshLocationId = (loc.message_count || 0) === 0 ? loc.id : null;
    addOrUpdateMarker({ ...loc, latitude: lat, longitude: lng });
    await loadMessages();
    connectSSE(loc.id);
  } catch(e) {
    setMessages('<div class="empty-mono">could not load</div>');
  }
});

async function openLocation(lat, lng, place, lid) {
  mapHint.classList.add('hidden');
  openPanel(place || 'loading…', lat, lng);
  S.locationId = lid;
  S.freshLocationId = null;
  setMessages('<div class="loading-mono">loading…</div>');
  if (!place) panelPlace.textContent = await reverseGeocode(lat, lng);
  await loadMessages();
  connectSSE(lid);
}

// ── Geocode ───────────────────────────────────────────────────────────────────
async function reverseGeocode(lat, lng) {
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&addressdetails=1&namedetails=1&zoom=18`;
    const d = await (await fetch(url, { headers: { 'Accept-Language': 'en' } })).json();
    if (!d || d.error) return `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
    const a = d.address || {}, nd = d.namedetails || {};
    const s = a.tourism||a.leisure||a.amenity||a.building||a.shop||a.historic||a.natural||a.man_made||nd.name||
              a.road||a.pedestrian||a.path||a.footway||a.neighbourhood||a.suburb||a.quarter||
              a.city_district||a.district||a.borough||a.town||a.village||a.hamlet||a.city||a.county||a.state;
    if (!s) return d.display_name.split(',').slice(0,2).join(',').trim();
    const city = a.city||a.town||a.village||'';
    return (city && city.toLowerCase()!==s.toLowerCase()) ? `${s}, ${city}` : s;
  } catch(e) { return `${lat.toFixed(4)}, ${lng.toFixed(4)}`; }
}

// ── Panel ─────────────────────────────────────────────────────────────────────
function openPanel(place, lat, lng) {
  panelPlace.textContent = place;
  panelCoords.textContent = `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`;
  panel.classList.add('open');
  switchTab('chat');
}

panelClose.addEventListener('click', async () => {
  panel.classList.remove('open');
  disconnectSSE();
  if (liveIndicator) liveIndicator.style.display = 'none';
  const freshId = S.freshLocationId;
  if (freshId) {
    if (S.markers[freshId]) { clusterGroup.removeLayer(S.markers[freshId]); delete S.markers[freshId]; }
    fetch(`/api/location/${freshId}`, { method: 'DELETE' }).catch(() => {});
    S.freshLocationId = null;
  }
  S.locationId = null;
  closeEmojiPicker();
});

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${name}`));
  if (name === 'top') renderTopMsg();
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(lid) {
  disconnectSSE();
  S.sse = new EventSource(`/api/stream/${lid}`);
  if (liveChip) liveChip.style.display = 'flex';
  if (liveIndicator) liveIndicator.style.display = 'flex';

  S.sse.addEventListener('new_message', e => {
    const msg = JSON.parse(e.data);
    // Only add if not ours (we already added it) and matches location
    if (window.CURRENT_USER && msg.user_id === window.CURRENT_USER.id) return;
    const card = document.createElement('div');
    card.innerHTML = renderMsg(msg);
    card.firstChild.classList.add('live-new');
    messagesList.insertAdjacentHTML('afterbegin', card.innerHTML);
    S.freshLocationId = null;
    updateTopIfNeeded(msg);
    updateMarkerCount();
  });

  S.sse.addEventListener('vote_update', e => {
    const { id, score } = JSON.parse(e.data);
    const el = $(`score-${id}`);
    if (el) { el.textContent = score; el.className = `vote-score ${score>0?'pos':score<0?'neg':''}`; }
  });

  S.sse.addEventListener('reaction_update', e => {
    const { id, reactions } = JSON.parse(e.data);
    const el = $(`reactions-${id}`);
    if (el) el.innerHTML = buildReactionsHtml(id, reactions);
  });

  S.sse.addEventListener('edit_message', e => {
    const { id, content } = JSON.parse(e.data);
    const el = $(`msgContent-${id}`);
    if (el) el.textContent = content;
  });

  S.sse.addEventListener('delete_message', e => {
    const { id } = JSON.parse(e.data);
    const el = $(`msg-${id}`);
    if (el) el.remove();
  });

  S.sse.addEventListener('badge_earned', e => {
    const { username, badges } = JSON.parse(e.data);
    badges.forEach(b => showBadgeToast(`${username} earned ${b.icon} ${b.label}!`));
  });

  S.sse.onerror = () => {
    if (liveChip) liveChip.style.display = 'none';
  };
}

function disconnectSSE() {
  if (S.sse) { S.sse.close(); S.sse = null; }
  if (liveChip) liveChip.style.display = 'none';
}

// ── Messages ──────────────────────────────────────────────────────────────────
function setMessages(html) { messagesList.innerHTML = html; }

async function loadMessages() {
  if (!S.locationId) return;
  try {
    const msgs = await (await fetch(`/api/messages/${S.locationId}`)).json();
    renderMessages(msgs);
  } catch(e) { setMessages('<div class="empty-mono">could not load</div>'); }
}

function renderMessages(msgs) {
  if (!msgs.length) {
    setMessages('<div class="empty-mono">// no messages yet</div>');
    renderTopMsg(null); return;
  }
  setMessages(msgs.map(m => renderMsg(m)).join(''));
  S.topMsg = msgs[0];
  renderTopMsg(msgs[0]);
}

function renderMsg(m, isReply = false) {
  const mine = window.CURRENT_USER && window.CURRENT_USER.id === m.user_id;
  const sc   = m.score > 0 ? 'pos' : m.score < 0 ? 'neg' : '';
  const reactHtml = buildReactionsHtml(m.id, m.reactions || []);

  const repliesHtml = (!isReply && m.replies && m.replies.length)
    ? `<div class="replies-wrap">${m.replies.map(r => renderMsg(r, true)).join('')}</div>` : '';

  const replyForm = !isReply ? `
    <div class="reply-form" id="replyForm-${m.id}">
      <textarea class="reply-input" id="replyInput-${m.id}" placeholder="reply…" maxlength="500" rows="2"></textarea>
      <div class="reply-btns">
        <button class="btn-xs btn-xs-g" onclick="cancelReply(${m.id})">cancel</button>
        <button class="btn-xs btn-xs-p" onclick="submitReply(${m.id})">send</button>
      </div>
    </div>` : '';

  return `
    <div class="msg-card ${isReply ? 'reply-card' : ''}" id="msg-${m.id}">
      <div class="msg-top">
        <img src="${esc(m.avatar_url||'')}" class="msg-avatar" alt=""
             onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'"
             onclick="window.location='/profile/${m.user_id}'">
        <div class="msg-body">
          <div class="msg-meta">
            <span class="msg-username" onclick="window.location='/profile/${m.user_id}'">${esc(m.username)}</span>
            <span class="msg-time">${relTime(m.created_at)}</span>
            ${m.edited ? '<span class="edited-tag">edited</span>' : ''}
          </div>
          <div class="msg-content" id="msgContent-${m.id}">${esc(m.content)}</div>
          <div class="msg-edit-form" id="editForm-${m.id}">
            <textarea class="msg-edit-input" id="editInput-${m.id}" maxlength="500">${esc(m.content)}</textarea>
            <div class="msg-edit-btns">
              <button class="btn-xs btn-xs-g" onclick="cancelEdit(${m.id})">cancel</button>
              <button class="btn-xs btn-xs-p" onclick="submitEdit(${m.id})">save</button>
            </div>
          </div>
          <div class="reactions-row" id="reactions-${m.id}">${reactHtml}</div>
        </div>
      </div>
      <div class="msg-actions">
        <div class="vote-row">
          <button class="vote-btn up ${m.user_vote===1?'active':''}" onclick="vote(${m.id},1)">▲</button>
          <span class="vote-score ${sc}" id="score-${m.id}">${m.score}</span>
          <button class="vote-btn down ${m.user_vote===-1?'active':''}" onclick="vote(${m.id},-1)">▼</button>
        </div>
        ${!isReply ? `<button class="action-btn" onclick="toggleReply(${m.id})">↩ reply</button>` : ''}
        <button class="action-btn" onclick="translate(${m.id},'${esc(m.content).replace(/'/g,'\\x27')}')">⟳ translate</button>
        <button class="action-btn danger" onclick="openReport(${m.id})">⚑ report</button>
        ${mine ? `
          <button class="action-btn" onclick="startEdit(${m.id})">edit</button>
          <button class="action-btn danger" onclick="deleteMsg(${m.id})">del</button>` : ''}
      </div>
      ${replyForm}
      ${repliesHtml}
    </div>`;
}

function buildReactionsHtml(msgId, reactions) {
  const btns = (reactions || []).map(r =>
    `<button class="reaction-btn ${r.reacted ? 'mine' : ''}" onclick="react(${msgId},'${r.emoji}')">
       ${r.emoji}<span class="reaction-count">${r.count}</span>
     </button>`
  ).join('');
  return btns + `<button class="add-reaction-btn" onclick="openEmojiPicker(event,${msgId})">+</button>`;
}

function updateTopIfNeeded(msg) {
  if (!S.topMsg || msg.score >= S.topMsg.score) {
    S.topMsg = msg;
  }
}

async function updateMarkerCount() {
  if (!S.locationId) return;
  try {
    const loc = await (await fetch(`/api/location/${S.locationId}`)).json();
    if (S.markers[S.locationId]) S.markers[S.locationId].setIcon(makeMarkerIcon(loc));
  } catch(e) {}
}

// ── Top message ───────────────────────────────────────────────────────────────
function renderTopMsg(m) {
  if (!topWrap) return;
  if (m === undefined) m = S.topMsg;
  if (!m) { topWrap.innerHTML = '<div class="top-empty">// no messages yet</div>'; return; }
  topWrap.innerHTML = `
    <div class="top-card">
      <div class="top-label">// top message</div>
      <div class="top-user">
        <img src="${esc(m.avatar_url||'')}" class="top-avatar" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
        <span class="top-username">${esc(m.username)}</span>
        <span class="top-score">▲ ${m.score}</span>
      </div>
      <div class="top-content">${esc(m.content)}</div>
      <div style="font-family:'Space Mono',monospace;font-size:0.62rem;color:var(--muted);margin-top:8px">${relTime(m.created_at)}</div>
    </div>`;
}

// ── Emoji reactions ───────────────────────────────────────────────────────────
let _emojiPickerEl = null;
function openEmojiPicker(event, msgId) {
  closeEmojiPicker();
  const picker = document.createElement('div');
  picker.className = 'emoji-picker';
  picker.style.position = 'fixed';
  EMOJIS.forEach(emoji => {
    const btn = document.createElement('button');
    btn.className = 'emoji-pick-btn'; btn.textContent = emoji;
    btn.onclick = () => { react(msgId, emoji); closeEmojiPicker(); };
    picker.appendChild(btn);
  });
  const rect = event.target.getBoundingClientRect();
  picker.style.top  = (rect.bottom + 4) + 'px';
  picker.style.left = Math.min(rect.left, window.innerWidth - 220) + 'px';
  document.body.appendChild(picker);
  _emojiPickerEl = picker;
  setTimeout(() => document.addEventListener('click', closeEmojiPicker), 0);
}
function closeEmojiPicker() {
  if (_emojiPickerEl) { _emojiPickerEl.remove(); _emojiPickerEl = null; }
  document.removeEventListener('click', closeEmojiPicker);
}

async function react(msgId, emoji) {
  if (!window.CURRENT_USER) { window.location.href = '/login'; return; }
  try {
    const res = await fetch('/api/react', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: msgId, emoji }),
    });
    if (res.status === 429) { showToast('slow down!'); return; }
    const d = await res.json();
    const el = $(`reactions-${msgId}`);
    if (el) el.innerHTML = buildReactionsHtml(msgId, d.reactions);
  } catch(e) {}
}

// ── Voting ────────────────────────────────────────────────────────────────────
async function vote(msgId, value) {
  if (!window.CURRENT_USER) { window.location.href = '/login'; return; }
  try {
    const res = await fetch('/api/vote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: msgId, value }),
    });
    if (res.status === 429) { showToast('rate limit hit'); return; }
    const d = await res.json();
    const scoreEl = $(`score-${msgId}`);
    if (scoreEl) {
      scoreEl.textContent = d.score;
      scoreEl.className = `vote-score ${d.score>0?'pos':d.score<0?'neg':''}`;
    }
    const card = $(`msg-${msgId}`);
    if (card) {
      card.querySelector('.vote-btn.up').classList.toggle('active', d.user_vote === 1);
      card.querySelector('.vote-btn.down').classList.toggle('active', d.user_vote === -1);
    }
  } catch(e) { showToast('vote failed'); }
}

// ── Translate ─────────────────────────────────────────────────────────────────
async function translate(msgId, originalText) {
  const contentEl = $(`msgContent-${msgId}`);
  if (!contentEl) return;
  if (contentEl.dataset.translated) {
    contentEl.textContent = originalText;
    delete contentEl.dataset.translated;
    const badge = contentEl.nextElementSibling;
    if (badge && badge.classList.contains('translated-badge')) badge.remove();
    return;
  }
  const lang = navigator.language.split('-')[0] || 'en';
  try {
    const res = await fetch('/api/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: originalText, target: lang }),
    });
    const d = await res.json();
    if (d.ok && d.translated) {
      contentEl.textContent = d.translated;
      contentEl.dataset.translated = '1';
      const badge = document.createElement('span');
      badge.className = 'translated-badge'; badge.textContent = `[translated]`;
      contentEl.insertAdjacentElement('afterend', badge);
    } else {
      showToast('translation unavailable — try running LibreTranslate locally');
    }
  } catch(e) { showToast('translation failed'); }
}

// ── Replies ───────────────────────────────────────────────────────────────────
function toggleReply(id) {
  const form = $(`replyForm-${id}`);
  if (!form) return;
  document.querySelectorAll('.reply-form.open').forEach(f => f.classList.remove('open'));
  form.classList.add('open');
  $(`replyInput-${id}`)?.focus();
}
function cancelReply(id) { $(`replyForm-${id}`)?.classList.remove('open'); }
async function submitReply(parentId) {
  if (!window.CURRENT_USER) { window.location.href = '/login'; return; }
  const input = $(`replyInput-${parentId}`);
  const content = input?.value.trim();
  if (!content) return;
  try {
    const res = await fetch('/api/message', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location_id: S.locationId, content, parent_id: parentId }),
    });
    if (res.status === 429) { showToast('rate limit: wait a moment'); return; }
    if (!res.ok) { showToast((await res.json()).error); return; }
    cancelReply(parentId);
    await loadMessages();
  } catch(e) { showToast('reply failed'); }
}

// ── Edit / Delete ─────────────────────────────────────────────────────────────
function startEdit(id) {
  $(`msgContent-${id}`)?.classList.add('editing');
  const f = $(`editForm-${id}`);
  if (f) { f.classList.add('visible'); $(`editInput-${id}`)?.focus(); }
}
function cancelEdit(id) {
  $(`msgContent-${id}`)?.classList.remove('editing');
  $(`editForm-${id}`)?.classList.remove('visible');
}
async function submitEdit(id) {
  const content = $(`editInput-${id}`)?.value.trim();
  if (!content) return;
  const res = await fetch(`/api/message/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) { showToast((await res.json()).error); return; }
  cancelEdit(id);
  await loadMessages();
}
async function deleteMsg(id) {
  if (!confirm('delete this message?')) return;
  const res = await fetch(`/api/message/${id}`, { method: 'DELETE' });
  if (!res.ok) { showToast((await res.json()).error); return; }
  await loadMessages();
  await updateMarkerCount();
}

// ── Post ──────────────────────────────────────────────────────────────────────
if (msgInput) {
  msgInput.addEventListener('input', () => {
    const l = msgInput.value.length;
    if (charCount) { charCount.textContent = l; charCount.className = l > 450 ? 'warn' : ''; }
    msgInput.style.height = 'auto';
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
  });
  msgInput.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doPost(); });
}
if (postBtn) postBtn.addEventListener('click', doPost);

async function doPost() {
  const content = msgInput?.value.trim();
  if (!content || !S.locationId) return;
  if (postBtn) { postBtn.disabled = true; postBtn.textContent = '…'; }
  try {
    const res = await fetch('/api/message', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location_id: S.locationId, content }),
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (res.status === 429) { showToast('rate limit: 5 per minute'); return; }
    if (!res.ok) { showToast((await res.json()).error); return; }
    const data = await res.json();
    if (msgInput) { msgInput.value = ''; msgInput.style.height = 'auto'; }
    if (charCount) charCount.textContent = '0';
    S.freshLocationId = null;
    // Show badge toasts
    if (data.new_badges && data.new_badges.length) {
      data.new_badges.forEach(b => showBadgeToast(`🏅 Badge unlocked: ${b}`));
    }
    await loadMessages();
    await updateMarkerCount();
  } catch(e) { showToast('post failed'); }
  finally { if (postBtn) { postBtn.disabled = false; postBtn.textContent = 'send ↵'; } }
}

// ── Report ────────────────────────────────────────────────────────────────────
function openReport(msgId) {
  if (!window.CURRENT_USER) { window.location.href = '/login'; return; }
  S.reportingMsgId = msgId;
  if (reportOverlay) reportOverlay.style.display = 'flex';
  const r = $('reportReason'); if (r) r.value = '';
}
function closeReport() {
  if (reportOverlay) reportOverlay.style.display = 'none';
  S.reportingMsgId = null;
}
async function submitReport() {
  const reason = $('reportReason')?.value.trim();
  if (!reason) { showToast('please enter a reason'); return; }
  try {
    const res = await fetch('/api/report', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: S.reportingMsgId, reason }),
    });
    if (!res.ok) { showToast((await res.json()).error); return; }
    showToast('reported — admins will review');
    closeReport();
  } catch(e) { showToast('report failed'); }
}

// ── Notifications ─────────────────────────────────────────────────────────────
if (notifBtn) {
  notifBtn.addEventListener('click', e => {
    e.stopPropagation();
    notifPanel?.classList.toggle('open');
    if (notifPanel?.classList.contains('open')) loadNotifs();
  });
}
if ($('markAllRead')) {
  $('markAllRead').addEventListener('click', async () => {
    await fetch('/api/notifications/read', { method: 'POST' });
    if (notifBadge) notifBadge.style.display = 'none';
    loadNotifs();
  });
}
document.addEventListener('click', e => {
  if (!notifPanel?.contains(e.target) && e.target !== notifBtn) notifPanel?.classList.remove('open');
  if (!reportOverlay?.contains(e.target.closest?.('.modal'))) {}
});

async function loadNotifs() {
  const notifs = await (await fetch('/api/notifications')).json();
  if (!notifList) return;
  if (!notifs.length) { notifList.innerHTML = '<div class="empty-mono">no notifications</div>'; return; }
  notifList.innerHTML = notifs.map(n => `
    <div class="notif-item ${n.read ? '' : 'unread'}" onclick="jumpToLocation(${n.location_id})">
      <div><span class="notif-user">${esc(n.reply_username)}</span> replied to your message</div>
      <div class="notif-preview">"${esc((n.reply_content||'').slice(0,50))}…"</div>
      <div class="notif-place">◈ ${esc(n.place_name||'')} · ${relTime(n.created_at)}</div>
    </div>`).join('');
}

async function pollUnread() {
  if (!window.CURRENT_USER) return;
  try {
    const { count } = await (await fetch('/api/notifications/unread-count')).json();
    if (notifBadge) { notifBadge.textContent = count; notifBadge.style.display = count > 0 ? '' : 'none'; }
  } catch(e) {}
}
if (window.CURRENT_USER) { pollUnread(); setInterval(pollUnread, 30000); }

function jumpToLocation(lid) {
  notifPanel?.classList.remove('open');
  fetch(`/api/location/${lid}`).then(r => r.json()).then(loc => {
    map.setView([loc.latitude, loc.longitude], 15);
    openLocation(loc.latitude, loc.longitude, loc.place_name, lid);
  });
}

// ── Search ────────────────────────────────────────────────────────────────────
let _searchTimer;
if (searchInput) {
  searchInput.addEventListener('input', () => { clearTimeout(_searchTimer); _searchTimer = setTimeout(doSearch, 250); });
  searchInput.addEventListener('keydown', e => { if (e.key === 'Escape') closeSearch(); });
}
document.addEventListener('click', e => {
  if (!searchInput?.contains(e.target) && !searchResults?.contains(e.target)) closeSearch();
});
async function doSearch() {
  const q = searchInput.value.trim();
  if (q.length < 2) { closeSearch(); return; }
  const items = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
  if (!items.length) { closeSearch(); return; }
  searchResults.innerHTML = items.map(l =>
    `<div class="search-result-item" onclick="selectSearch(${l.id},${l.latitude},${l.longitude},'${esc(l.place_name||'')}')">
       <span class="search-result-name">${esc(l.place_name||'Unknown')}</span>
       <span class="search-result-count">${l.message_count}</span>
     </div>`).join('');
  searchResults.classList.add('open');
}
function closeSearch() { searchResults.classList.remove('open'); }
function selectSearch(lid, lat, lng, name) {
  closeSearch(); searchInput.value = name;
  map.setView([lat, lng], 15);
  openLocation(lat, lng, name, lid);
}

// ── URL param ?loc= ───────────────────────────────────────────────────────────
(async () => {
  const lid = new URLSearchParams(window.location.search).get('loc');
  if (lid) {
    const loc = await (await fetch(`/api/location/${lid}`)).json();
    if (loc.id) { map.setView([loc.latitude, loc.longitude], 14); openLocation(loc.latitude, loc.longitude, loc.place_name, parseInt(lid)); }
  }
})();

// ── Utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function relTime(ts) {
  const d = new Date(ts&&ts.endsWith('Z')?ts:ts+'Z'), diff = (Date.now()-d)/1000;
  if (isNaN(diff)) return '';
  if (diff<60) return 'just now'; if (diff<3600) return Math.floor(diff/60)+'m';
  if (diff<86400) return Math.floor(diff/3600)+'h'; return d.toLocaleDateString('en',{month:'short',day:'numeric'});
}
function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position:'fixed', bottom:'70px', left:'50%', transform:'translateX(-50%)',
    background:'var(--surface)', color:'var(--cyan)', padding:'7px 16px',
    borderRadius:'5px', fontSize:'0.72rem', zIndex:'9999',
    fontFamily:"'Space Mono',monospace", border:'1px solid var(--border)',
    boxShadow:'var(--shadow)', pointerEvents:'none',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2800);
}
function showBadgeToast(msg) {
  if (!badgeToast || !badgeToastInner) return;
  badgeToastInner.textContent = msg;
  badgeToast.style.display = 'block';
  setTimeout(() => { badgeToast.style.display = 'none'; }, 4000);
}

// Expose to inline handlers
Object.assign(window, {
  vote, react, openEmojiPicker, closeEmojiPicker, translate,
  toggleReply, cancelReply, submitReply,
  startEdit, cancelEdit, submitEdit, deleteMsg,
  openReport, closeReport, submitReport,
  jumpToLocation, selectSearch,
});
