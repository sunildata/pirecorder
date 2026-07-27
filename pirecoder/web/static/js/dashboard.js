/* Dashboard: transport, VU meters, waveform, gain control, telemetry.

   The timer runs off a local interval seeded by server duration rather than
   waiting for each frame. That way it keeps counting smoothly even if the
   socket stalls — and it re-syncs the moment a real update lands. */

(() => {
  const $ = (id) => document.getElementById(id);

  const el = {
    timer: $('timer'), sub: $('timer-sub'), alert: $('alert'),
    label: $('label'), clip: $('clip'),
    record: $('btn-record'), pause: $('btn-pause'), stop: $('btn-stop'),
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
  }

  function clearMeters() {
    ['l', 'r'].forEach((ch) => {
      $(`m-${ch}`).style.width = '0%';
      $(`h-${ch}`).style.opacity = '0';
      $(`db-${ch}`).textContent = '−∞';
    });
    waveformClear();
  }

  /* ── Waveform oscilloscope ───────────────────────────────────────────── */

  const wfCanvas = $('waveform');
  const wfCtx = wfCanvas ? wfCanvas.getContext('2d') : null;
  // Pixels scrolled left each time new data arrives (10 Hz → 40px/frame ≈ 400px/s)
  const WF_SCROLL = 40;
  // CSS colours matching app.css design tokens
  const WF_BG    = '#0e1219';
  const WF_LINE  = '#4f8cff';
  const WF_GRID  = '#1c2230';
  const WF_ZERO  = '#2a3242';

  function waveformResize() {
    if (!wfCanvas || !wfCtx) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = wfCanvas.clientWidth;
    const cssH = wfCanvas.clientHeight;
    if (!cssW) return;
    wfCanvas.width  = cssW * dpr;
    wfCanvas.height = cssH * dpr;
    wfCtx.scale(dpr, dpr);
    waveformDrawGrid(cssW, cssH);
  }

  function waveformDrawGrid(w, h) {
    if (!wfCtx) return;
    wfCtx.fillStyle = WF_BG;
    wfCtx.fillRect(0, 0, w, h);
    // Horizontal centre line
    wfCtx.strokeStyle = WF_ZERO;
    wfCtx.lineWidth = 1;
    wfCtx.beginPath();
    wfCtx.moveTo(0, h / 2);
    wfCtx.lineTo(w, h / 2);
    wfCtx.stroke();
    // ±50% guide lines
    wfCtx.strokeStyle = WF_GRID;
    [0.25, 0.75].forEach((frac) => {
      wfCtx.beginPath();
      wfCtx.moveTo(0, h * frac);
      wfCtx.lineTo(w, h * frac);
      wfCtx.stroke();
    });
  }

  function waveformClear() {
    if (!wfCtx || !wfCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    waveformDrawGrid(wfCanvas.width / dpr, wfCanvas.height / dpr);
  }

  function waveformPush(points) {
    if (!wfCtx || !wfCanvas || !points.length) return;
    const dpr = window.devicePixelRatio || 1;
    const W = wfCanvas.width  / dpr;
    const H = wfCanvas.height / dpr;
    const mid = H / 2;

    // Shift existing content left by WF_SCROLL pixels
    wfCtx.drawImage(wfCanvas, -WF_SCROLL * dpr, 0, wfCanvas.width, wfCanvas.height,
                              0, 0, wfCanvas.width, wfCanvas.height);

    // Clear the right strip (accounting for DPR artefacts)
    wfCtx.fillStyle = WF_BG;
    wfCtx.fillRect(W - WF_SCROLL - 1, 0, WF_SCROLL + 1, H);

    // Redraw grid lines in the new strip
    wfCtx.strokeStyle = WF_ZERO;
    wfCtx.lineWidth = 1;
    wfCtx.beginPath();
    wfCtx.moveTo(W - WF_SCROLL, mid);
    wfCtx.lineTo(W, mid);
    wfCtx.stroke();
    wfCtx.strokeStyle = WF_GRID;
    [0.25, 0.75].forEach((frac) => {
      wfCtx.beginPath();
      wfCtx.moveTo(W - WF_SCROLL, H * frac);
      wfCtx.lineTo(W, H * frac);
      wfCtx.stroke();
    });

    // Draw new waveform samples in the right strip
    wfCtx.strokeStyle = WF_LINE;
    wfCtx.lineWidth = 1.5;
    wfCtx.lineJoin = 'round';
    wfCtx.beginPath();
    const xStep = WF_SCROLL / (points.length - 1 || 1);
    points.forEach((v, i) => {
      const x = W - WF_SCROLL + i * xStep;
      const y = mid - v * mid * 0.88;
      i === 0 ? wfCtx.moveTo(x, y) : wfCtx.lineTo(x, y);
    });
    wfCtx.stroke();
  }

  window.addEventListener('resize', waveformResize);
  // Defer until layout is complete so clientWidth is non-zero
  setTimeout(waveformResize, 0);

  /* ── Transport state ─────────────────────────────────────────────────── */

  function applyStatus(s) {
    if (!s) return;
    state = s;
    const rec = s.is_recording;
    const paused = s.is_paused;

    el.record.disabled = rec && !paused;
    el.pause.disabled = !rec;
    el.stop.disabled = !rec;
    el.label.disabled = rec;

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

    } else {
      localDuration = 0;
      el.timer.textContent = '00:00:00';
      el.sub.textContent = 'Ready';
      ['s-size', 's-format', 's-parts', 's-device'].forEach((k) => {
        $(k).textContent = '—';
      });
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

  el.clip.addEventListener('click', () => guard(async () => {
    await ZP.api('/levels/reset-clip', { method: 'POST' });
    el.clip.classList.remove('on');
  }));

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
