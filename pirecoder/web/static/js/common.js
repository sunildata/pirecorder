/* Shared helpers: transport, formatting, and a connection that survives
   the phone going to sleep, losing Wi-Fi, or roaming between APs.

   The recorder keeps running regardless of this socket — everything here is
   presentation only. On reconnect we simply re-read server state. */

const ZP = (() => {
  let socket = null;
  let pollTimer = null;
  let online = false;
  const handlers = {};

  /* ── Formatting ──────────────────────────────────────────────────────── */

  function hms(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = String(Math.floor(s / 3600)).padStart(2, '0');
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(s % 60).padStart(2, '0');
    return `${h}:${m}:${sec}`;
  }

  function bytes(n) {
    if (!n) return '0 MB';
    const mb = n / 1048576;
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  }

  function when(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return sameDay ? time : `${d.toLocaleDateString()} ${time}`;
  }

  /* ── Toast ───────────────────────────────────────────────────────────── */

  let toastTimer = null;
  function toast(message, kind = '') {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, 3200);
  }

  /* ── API ─────────────────────────────────────────────────────────────── */

  async function api(path, options = {}) {
    const opts = {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    };
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(`/api${path}`, opts);

    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('Not authenticated');
    }

    const type = res.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      return res;
    }

    const data = await res.json();
    if (!res.ok) {
      const err = new Error(data.error || `Request failed (${res.status})`);
      err.payload = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ── Live connection ─────────────────────────────────────────────────── */

  function on(event, fn) {
    (handlers[event] = handlers[event] || []).push(fn);
  }

  function fire(event, payload) {
    (handlers[event] || []).forEach((fn) => {
      try { fn(payload); } catch (e) { /* a broken widget shouldn't stop the rest */ }
    });
  }

  function setOnline(state) {
    if (online === state) return;
    online = state;
    const badge = document.getElementById('offline-badge');
    if (badge) badge.classList.toggle('show', !state);
  }

  function connect() {
    if (typeof io === 'undefined') { startPolling(); return; }

    socket = io({
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 700,
      reconnectionDelayMax: 4000,
      reconnectionAttempts: Infinity,
      timeout: 8000,
    });

    socket.on('connect', () => { setOnline(true); stopPolling(); refresh(); });
    socket.on('disconnect', () => { setOnline(false); startPolling(); });
    socket.on('connect_error', () => { setOnline(false); startPolling(); });

    ['status', 'levels', 'system', 'recording_started', 'recording_stopped',
     'recording_paused', 'recording_resumed', 'marker_added', 'segment_split',
     'capture_restarted', 'capture_failed', 'storage_full']
      .forEach((evt) => socket.on(evt, (payload) => fire(evt, payload)));
  }

  /* Polling is the safety net for a flaky venue network. It is deliberately
     slow — the point is that the UI eventually tells the truth, not that it
     is smooth. */
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try {
        const data = await api('/status');
        setOnline(true);
        fire('status', data);
        if (data.levels) fire('levels', { levels: data.levels, status: data });
      } catch (e) { setOnline(false); }
    }, 2000);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  async function refresh() {
    try {
      const data = await api('/status');
      fire('status', data);
    } catch (e) { /* connect handler will retry */ }
  }

  /* A phone waking from sleep fires this; pull fresh state right away rather
     than waiting for the next socket frame. */
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
  window.addEventListener('online', () => refresh());

  /* ── Nav indicator ───────────────────────────────────────────────────── */

  on('status', (s) => {
    const dot = document.getElementById('nav-dot');
    if (!dot) return;
    dot.className = 'rec-dot' +
      (s.is_paused ? ' paused' : s.is_recording ? ' live' : '');
  });

  return { api, on, fire, connect, toast, hms, bytes, when, refresh,
           get isOnline() { return online; } };
})();

document.addEventListener('DOMContentLoaded', () => ZP.connect());
