/* Dashboard: transport, VU meters, waveform, gain control, telemetry.

   The timer runs off a local interval seeded by server duration rather than
   waiting for each frame. That way it keeps counting smoothly even if the
   socket stalls — and it re-syncs the moment a real update lands. */

(() => {
  const $ = (id) => document.getElementById(id);

  const el = {
    timer: $('timer'), sub: $('timer-sub'), alert: $('alert'),
    label: $('label'), markers: $('markers'), clip: $('clip'),
    record: $('btn-record'), pause: $('btn-pause'), stop: $('btn-stop'),
    marker: $('btn-marker'), notes: $('btn-notes'),
  };

  let state = { is_recording: false, is_paused: false, session: null };
  let localDuration = 0;
  let tick = null;

  /* ── Meters ──────────────────────────────────────────────────────────── */

  // −60 dBFS maps to 0% so the visible range matches what an engineer cares
  // about; anything quieter is silence for metering purposes.
  const FLOOR = -60;
  const toPct = (db) => Math.max(0, Math.min(100, ((db - FLOOR) / -FLOOR) * 100));
  const fmtDb = (db) => (db <= -90 ? '−∞' : `${db.toFixed(1)}`);

  function renderLevels(levels) {
    if (!levels) return;
    ['l', 'r'].forEach((ch, i) => {
      const peak = levels.peak_db?.[i] ?? -90;
      const rms  = levels.rms_db?.[i] ?? -90;
      const hold = levels.peak_hold_db?.[i] ?? -90;
      $(`m-${ch}`).style.width = `${toPct(rms)}%`;
      $(`h-${ch}`).style.left = `${toPct(peak)}%`;
      $(`db-${ch}`).textContent = fmtDb(peak);
      $(`h-${ch}`).style.opacity = hold > FLOOR ? '.85' : '0';
    });
    const clipped = (levels.clip || []).some(Boolean);
    el.clip.classList.toggle('on', clipped);
    if (levels.waveform && levels.waveform.length) {
      waveformPush(levels.waveform);
    }
    renderSignalStatus(levels);
  }

  function clearMeters() {
    // Never wipe live meter data while the monitor is running — the wipeout
    // was the main reason the waveform appeared flat in idle mode.
    if (monitorActive) return;
    ['l', 'r'].forEach((ch) => {
      $(`m-${ch}`).style.width = '0%';
      $(`h-${ch}`).style.opacity = '0';
      $(`db-${ch}`).textContent = '−∞';
    });
    waveformClear();
    renderSignalStatus(null);
  }

  /* ── Signal status badge ─────────────────────────────────────────────── */
  // Threshold above which we consider a signal "present" (−50 dBFS).
  const SIG_THRESHOLD = -50;

  function renderSignalStatus(levels) {
    const dot   = $('signal-dot');
    const label = $('signal-label');
    const peak  = $('signal-peak');
    if (!dot) return;

    if (!levels || !levels.active) {
      dot.className   = 'signal-dot';
      if (label) label.textContent = 'NO SIGNAL';
      if (peak)  peak.textContent  = '';
      return;
    }

    const maxPeak = Math.max(...(levels.peak_db || [-90]));
    const hasSignal = maxPeak > SIG_THRESHOLD;

    dot.className   = 'signal-dot' + (hasSignal ? ' sig-on' : '');
    if (label) label.textContent = hasSignal ? 'SIGNAL' : 'SILENT';
    if (peak)  peak.textContent  = hasSignal ? `${fmtDb(maxPeak)} dB` : '';
  }

  /* ── Waveform oscilloscope (ring-buffer, full-redraw) ───────────────── */
  //
  // Ring buffer stores the last WF_BUF samples (oldest→newest).
  // Every levels frame, new points are written in; the whole canvas is
  // redrawn from left→right mapping oldest→newest.  No drawImage tricks,
  // so DPR scaling is never an issue.

  const wfCanvas = $('waveform');
  const wfCtx    = wfCanvas ? wfCanvas.getContext('2d') : null;
  const WF_BUF   = 800;          // ~10 frames × 80 pts = 1 second visible
  const wfBuf    = new Float32Array(WF_BUF);
  let   wfHead   = 0;            // next write slot (mod WF_BUF)

  const WF_BG   = '#0e1219';
  const WF_LINE = '#4f8cff';
  const WF_GRID = '#1c2230';
  const WF_ZERO = '#2a3242';

  function waveformResize() {
    if (!wfCanvas || !wfCtx) return;
    const cssW = wfCanvas.clientWidth;
    const cssH = wfCanvas.clientHeight;
    if (!cssW || !cssH) return;
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.floor(cssW * dpr);
    const ph = Math.floor(cssH * dpr);
    if (wfCanvas.width === pw && wfCanvas.height === ph) return;
    wfCanvas.width  = pw;
    wfCanvas.height = ph;
    wfCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    waveformDraw();
  }

  function waveformDraw() {
    if (!wfCtx || !wfCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W   = wfCanvas.width  / dpr;
    const H   = wfCanvas.height / dpr;
    const mid = H / 2;

    // Background
    wfCtx.fillStyle = WF_BG;
    wfCtx.fillRect(0, 0, W, H);

    // Grid lines
    wfCtx.lineWidth = 1;
    [[WF_ZERO, mid], [WF_GRID, H * 0.25], [WF_GRID, H * 0.75]].forEach(([col, y]) => {
      wfCtx.strokeStyle = col;
      wfCtx.beginPath(); wfCtx.moveTo(0, y); wfCtx.lineTo(W, y); wfCtx.stroke();
    });

    // Waveform: map ring buffer oldest→newest to left→right
    wfCtx.strokeStyle = WF_LINE;
    wfCtx.lineWidth   = 1.5;
    wfCtx.lineJoin    = 'round';
    wfCtx.beginPath();
    for (let i = 0; i < WF_BUF; i++) {
      const v = wfBuf[(wfHead + i) % WF_BUF];
      const x = (i / (WF_BUF - 1)) * W;
      const y = mid - v * mid * 0.88;
      i === 0 ? wfCtx.moveTo(x, y) : wfCtx.lineTo(x, y);
    }
    wfCtx.stroke();
  }

  function waveformPush(points) {
    if (!points || !points.length) return;
    for (const v of points) {
      wfBuf[wfHead] = v;
      wfHead = (wfHead + 1) % WF_BUF;
    }
    waveformDraw();
  }

  function waveformClear() {
    wfBuf.fill(0);
    wfHead = 0;
    waveformDraw();
  }

  // Use ResizeObserver when available — it fires reliably after layout.
  if (wfCanvas) {
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(waveformResize).observe(wfCanvas);
    } else {
      window.addEventListener('resize', waveformResize);
    }
    // First paint: try immediately, then again after one frame in case CSS
    // hasn't finished calculating the card width yet.
    waveformResize();
    requestAnimationFrame(waveformResize);
  }

  /* ── Transport state ─────────────────────────────────────────────────── */

  function applyStatus(s) {
    if (!s) return;
    const wasRecording = state.is_recording;
    state = s;
    const rec    = s.is_recording;
    const paused = s.is_paused;

    // When recording starts, the backend kills the monitor automatically;
    // reflect that in the UI.
    if (rec && monitorActive) applyMonitorUi(false);

    // When recording stops, restart the input monitor so the engineer can
    // immediately see the level without touching anything.
    if (wasRecording && !rec) {
      setTimeout(autoStartMonitor, 400);
    }

    el.record.disabled = rec && !paused;
    el.pause.disabled = !rec;
    el.stop.disabled = !rec;
    el.marker.disabled = !rec || paused;
    el.label.disabled = rec;
    if (btnMonitor) btnMonitor.disabled = rec;

    el.pause.textContent = paused ? 'Resume' : 'Pause';
    el.record.textContent = paused ? 'Resume' : 'Record';
    el.timer.classList.toggle('live', rec && !paused);

    if (s.session) {
      localDuration = s.session.duration || 0;
      el.timer.textContent = ZP.hms(localDuration);
      el.sub.textContent = paused
        ? 'Paused — audio already saved to card'
        : s.session.base_name;

      $('s-size').textContent = ZP.bytes(s.session.size_bytes);
      $('s-format').textContent =
        `${(s.session.sample_rate / 1000).toFixed(1)}k/${s.session.bit_depth}`;
      $('s-parts').textContent = (s.session.segments || []).length;
      $('s-device').textContent = s.session.channels === 2 ? 'Stereo' : 'Mono';

      renderMarkers(s.session.markers || []);
    } else {
      localDuration = 0;
      el.timer.textContent = '00:00:00';
      el.sub.textContent = 'Ready';
      ['s-size', 's-format', 's-parts', 's-device'].forEach((k) => {
        $(k).textContent = '—';
      });
      el.markers.innerHTML = '';
      clearMeters();
    }

    if (s.last_error) showAlert(s.last_error, 'error');
    startTick(rec && !paused);
  }

  function startTick(run) {
    clearInterval(tick);
    if (!run) return;
    tick = setInterval(() => {
      localDuration += 1;
      el.timer.textContent = ZP.hms(localDuration);
    }, 1000);
  }

  function renderMarkers(markers) {
    el.markers.innerHTML = markers
      .slice(-8)
      .map((m) => `<span class="marker-chip">${ZP.hms(m.offset_seconds)} · ${escapeHtml(m.label)}</span>`)
      .join('');
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  function showAlert(message, kind = '') {
    el.alert.textContent = message;
    el.alert.className = `banner ${kind}`;
  }

  function hideAlert() {
    el.alert.className = 'banner hidden';
  }

  /* ── System telemetry ────────────────────────────────────────────────── */

  function renderSystem(sys) {
    if (!sys) return;
    const st = sys.storage || {};
    $('d-free').textContent = st.free_mb > 1024
      ? `${(st.free_mb / 1024).toFixed(1)}G` : `${st.free_mb || 0}M`;
    $('d-hours').textContent = st.recording_hours_left ?? '—';

    const temp = sys.cpu_temp_c;
    const tempEl = $('d-temp');
    tempEl.textContent = temp ?? '—';
    tempEl.className = 'stat-value' + (temp > 75 ? ' bad' : temp > 65 ? ' warn' : '');

    $('d-cpu').textContent = `${sys.cpu_percent ?? 0}`;

    const b = sys.battery || {};
    $('d-batt').textContent = b.present ? `${b.percent}%` : 'AC';
    $('d-ip').textContent = sys.ip || '—';

    const pct = st.percent_used || 0;
    const bar = $('d-bar');
    bar.style.width = `${pct}%`;
    bar.className = 'bar-fill' + (pct > 92 ? ' bad' : pct > 80 ? ' warn' : '');

    // Under-voltage is the most common cause of USB audio dropouts on a Pi.
    const t = sys.throttled || {};
    if (t.under_voltage_now) {
      showAlert('Under-voltage detected — use a 2.5 A+ supply, audio may drop', 'error');
    }
  }

  /* ── Actions ─────────────────────────────────────────────────────────── */

  async function guard(fn) {
    try { await fn(); }
    catch (err) { ZP.toast(err.message, 'error'); }
  }

  el.record.addEventListener('click', () => guard(async () => {
    if (state.is_paused) {
      applyStatus(await ZP.api('/record/resume', { method: 'POST' }));
      return;
    }
    hideAlert();
    const res = await ZP.api('/record/start', {
      method: 'POST',
      body: { label: el.label.value.trim() },
    });
    applyStatus(res);
    ZP.toast('Recording started', 'ok');
  }));

  el.pause.addEventListener('click', () => guard(async () => {
    const path = state.is_paused ? '/record/resume' : '/record/pause';
    applyStatus(await ZP.api(path, { method: 'POST' }));
  }));

  el.stop.addEventListener('click', () => guard(async () => {
    let res;
    try {
      res = await ZP.api('/record/stop', { method: 'POST' });
    } catch (err) {
      // Recording lock: confirm before ending a live take.
      if (err.payload?.requires_confirmation) {
        if (!confirm('Recording is locked. Stop anyway?')) return;
        res = await ZP.api('/record/stop', { method: 'POST', body: { confirm: true } });
      } else {
        throw err;
      }
    }
    ZP.toast(`Saved: ${res.session.base_name}`, 'ok');
    el.label.value = '';
    applyStatus({ is_recording: false, is_paused: false, session: null });
  }));

  el.marker.addEventListener('click', () => guard(async () => {
    const m = await ZP.api('/record/marker', { method: 'POST', body: {} });
    ZP.toast(`Marker at ${ZP.hms(m.offset_seconds)}`, 'ok');
  }));

  el.notes.addEventListener('click', () => guard(async () => {
    const current = state.session?.notes || '';
    const notes = prompt('Notes for this recording:', current);
    if (notes === null) return;
    if (state.is_recording) {
      await ZP.api('/record/notes', { method: 'POST', body: { notes } });
      ZP.toast('Notes saved', 'ok');
    } else {
      ZP.toast('Notes apply to an active recording', 'error');
    }
  }));

  el.clip.addEventListener('click', () => guard(async () => {
    await ZP.api('/levels/reset-clip', { method: 'POST' });
    el.clip.classList.remove('on');
  }));

  /* ── Monitor toggle ──────────────────────────────────────────────────── */

  let monitorActive = false;
  const btnMonitor = $('btn-monitor');

  function applyMonitorUi(active) {
    monitorActive = active;
    if (!btnMonitor) return;
    btnMonitor.textContent = active ? 'Monitoring…' : 'Monitor Off';
    btnMonitor.classList.toggle('monitor-on', active);
    btnMonitor.classList.toggle('monitor-off', !active);
  }

  async function autoStartMonitor() {
    if (monitorActive || state.is_recording) return;
    try {
      await ZP.api('/monitor', { method: 'POST', body: { active: true } });
      applyMonitorUi(true);
    } catch (_) {
      // Device unavailable or already recording — fail silently.
    }
  }

  if (btnMonitor) {
    btnMonitor.addEventListener('click', () => guard(async () => {
      if (state.is_recording) {
        ZP.toast('Cannot change monitor while recording', 'error');
        return;
      }
      const next = !monitorActive;
      await ZP.api('/monitor', { method: 'POST', body: { active: next } });
      applyMonitorUi(next);
      if (!next) {
        waveformClear();
        renderSignalStatus(null);
        ZP.toast('Monitor off', '');
      }
    }));
  }

  // Auto-start the monitor when the page opens so the engineer can see the
  // input level immediately — no button press needed.
  setTimeout(autoStartMonitor, 600);

  /* ── Input Gain control ──────────────────────────────────────────────── */

  const gainSlider    = $('gain-slider');
  const gainDbLabel   = $('gain-db-label');
  const gainStatus    = $('gain-status');
  let gainSupported   = false;
  let gainDebounce    = null;

  function gainLabel(v) {
    const n = parseInt(v, 10);
    return n >= 0 ? `+${n} dB` : `${n} dB`;
  }

  function applyGainUi(gain_db, supported) {
    gainSupported = supported;
    const card = $('gain-card');
    if (card) card.classList.toggle('gain-unsupported', !supported);
    if (gainSlider) gainSlider.disabled = !supported;
    if (gainDbLabel) gainDbLabel.textContent = gainLabel(gain_db);
    if (gainSlider)  gainSlider.value = gain_db;
    if (gainStatus) {
      gainStatus.textContent = supported
        ? ''
        : 'Gain control not available for this device';
    }
  }

  async function sendGain(db) {
    try {
      const res = await ZP.api('/gain', { method: 'POST', body: { gain_db: db } });
      if (res.ok) {
        ZP.toast(`Gain set to ${gainLabel(res.gain_db)}`, 'ok');
        if (gainDbLabel) gainDbLabel.textContent = gainLabel(res.gain_db);
      } else {
        ZP.toast('Gain not supported on this device', 'error');
        applyGainUi(0, false);
      }
    } catch (err) {
      ZP.toast(err.message, 'error');
    }
  }

  if (gainSlider) {
    gainSlider.addEventListener('input', () => {
      if (gainDbLabel) gainDbLabel.textContent = gainLabel(gainSlider.value);
      clearTimeout(gainDebounce);
      gainDebounce = setTimeout(() => sendGain(parseInt(gainSlider.value, 10)), 300);
    });
  }

  document.querySelectorAll('[data-gain]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!gainSupported) { ZP.toast('Gain control not supported', 'error'); return; }
      const db = parseInt(btn.dataset.gain, 10);
      if (gainSlider) gainSlider.value = db;
      if (gainDbLabel) gainDbLabel.textContent = gainLabel(db);
      sendGain(db);
    });
  });

  // Load current gain on page open
  ZP.api('/gain').then((r) => applyGainUi(r.gain_db, r.supported)).catch(() => applyGainUi(0, false));

  /* ── Wire up ─────────────────────────────────────────────────────────── */

  ZP.on('status', applyStatus);
  ZP.on('levels', (p) => { renderLevels(p.levels); if (p.status) applyStatus(p.status); });
  ZP.on('system', renderSystem);
  ZP.on('segment_split', () => ZP.toast('New file started (auto-split)'));
  ZP.on('capture_restarted', () => showAlert('Capture hiccup — recording resumed automatically', ''));
  ZP.on('capture_failed', (p) => showAlert(`Recording failed: ${p.error}`, 'error'));
  ZP.on('storage_full', () => showAlert('Storage full — recording stopped', 'error'));

  ZP.api('/system').then(renderSystem).catch(() => {});
})();
