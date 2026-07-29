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

  /* ── Waveform oscilloscope (ring-buffer, full-redraw each frame) ────── */
  //
  // drawImage-scroll is broken under DPR scaling — source coords are physical
  // pixels but destination coords are CSS pixels after setTransform, so the
  // canvas shifts the wrong amount and content piles up in one corner.
  //
  // Ring-buffer approach: store last WF_BUF samples, redraw the whole canvas
  // every frame.  At 10 Hz on a phone browser this is negligible CPU cost.

  const wfCanvas = $('waveform');
  const wfCtx    = wfCanvas ? wfCanvas.getContext('2d') : null;
  // Server sends 60 points per frame at 20 Hz = 1200 pts/s.
  // 600 slots therefore shows a 0.5 s window, so the display scrolls its full
  // width twice a second — that is what reads as "live" to the eye.
  const WF_BUF   = 600;
  const wfBuf    = new Float32Array(WF_BUF);
  let   wfHead   = 0;             // next write slot (wraps at WF_BUF)
  let   wfPendingFrame = false;   // rAF coalescing guard

  const WF_BG   = '#0e1219';
  const WF_LINE = '#4f8cff';
  const WF_CLIP = '#ff3b3b';
  const WF_GRID = '#1c2230';
  const WF_ZERO = '#2a3242';

  function waveformResize() {
    if (!wfCanvas || !wfCtx) return;
    const cssW = wfCanvas.clientWidth;
    const cssH = wfCanvas.clientHeight;
    if (!cssW || !cssH) return;
    const dpr = window.devicePixelRatio || 1;
    const pw  = Math.floor(cssW * dpr);
    const ph  = Math.floor(cssH * dpr);
    if (wfCanvas.width === pw && wfCanvas.height === ph) return;
    wfCanvas.width  = pw;
    wfCanvas.height = ph;
    // Reassert the DPR scale (setting .width resets the context transform).
    wfCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    waveformDraw();
  }

  function waveformDraw() {
    if (!wfCtx || !wfCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W   = wfCanvas.width  / dpr;   // CSS pixels
    const H   = wfCanvas.height / dpr;
    const mid = H / 2;

    // Background + grid
    wfCtx.fillStyle = WF_BG;
    wfCtx.fillRect(0, 0, W, H);
    wfCtx.lineWidth = 1;
    [[WF_ZERO, mid], [WF_GRID, H * 0.25], [WF_GRID, H * 0.75]].forEach(([col, y]) => {
      wfCtx.strokeStyle = col;
      wfCtx.beginPath(); wfCtx.moveTo(0, y); wfCtx.lineTo(W, y); wfCtx.stroke();
    });

    // Check if any sample in the buffer is near clipping — turn line red.
    let hasClip = false;
    for (let i = 0; i < WF_BUF; i++) {
      if (Math.abs(wfBuf[i]) >= 0.9) { hasClip = true; break; }
    }

    // Draw ring buffer oldest→newest mapped to left→right across full width.
    wfCtx.strokeStyle = hasClip ? WF_CLIP : WF_LINE;
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

  // Coalesce to one repaint per display frame. Samples are never dropped —
  // they all land in the ring buffer — only redundant redraws are skipped if
  // several socket frames arrive within a single screen refresh.
  function waveformSchedule() {
    if (wfPendingFrame) return;
    wfPendingFrame = true;
    requestAnimationFrame(() => {
      wfPendingFrame = false;
      waveformDraw();
    });
  }

  function waveformPush(points) {
    if (!points || !points.length) return;
    for (const v of points) {
      wfBuf[wfHead] = v;
      wfHead = (wfHead + 1) % WF_BUF;
    }
    waveformSchedule();
  }

  function waveformClear() {
    wfBuf.fill(0);
    wfHead = 0;
    waveformDraw();
  }

  // ResizeObserver fires after layout so clientWidth is always valid.
  if (wfCanvas) {
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(waveformResize).observe(wfCanvas);
    } else {
      window.addEventListener('resize', waveformResize);
    }
    waveformResize();
    requestAnimationFrame(waveformResize);
  }

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

  // Gain is driven in percent because that is what every ALSA capture control
  // understands. The dB figure is shown alongside when the device reports one.
  const gainSlider  = $('gain-slider');
  const gainValue   = $('gain-value');
  const gainStatus  = $('gain-status');
  let gainSupported = false;
  let gainDebounce  = null;
  let gainInFlight  = false;
  let gainQueued    = null;      // most recent value while a call is running

  function gainLabel(percent, db) {
    return db === null || db === undefined
      ? `${percent}%`
      : `${percent}% · ${db > 0 ? '+' : ''}${db.toFixed(1)} dB`;
  }

  function applyGainUi(res) {
    gainSupported = !!res.supported;
    const card = $('gain-card');
    if (card) card.classList.toggle('gain-unsupported', !gainSupported);
    if (gainSlider) {
      gainSlider.disabled = !gainSupported;
      gainSlider.value = res.percent ?? 0;
    }
    if (gainValue) {
      gainValue.textContent = gainSupported ? gainLabel(res.percent, res.db) : '—';
    }
    if (gainStatus) {
      gainStatus.textContent = gainSupported
        ? (res.control ? `ALSA control: ${res.control}` : '')
        : (res.reason || 'This interface has no software input gain — '
                       + 'use its hardware gain knob.');
    }
  }

  async function sendGain(percent) {
    // Coalesce: keep only the newest target while a request is in flight so
    // dragging the slider never queues a backlog of amixer calls.
    if (gainInFlight) { gainQueued = percent; return; }
    gainInFlight = true;
    try {
      const res = await ZP.api('/gain', { method: 'POST', body: { percent } });
      applyGainUi(res);
      if (!res.ok && res.supported) {
        ZP.toast(`Device clamped gain to ${res.percent}%`, '');
      } else if (!res.supported) {
        ZP.toast('No software input gain on this interface', 'error');
      }
    } catch (err) {
      ZP.toast(err.message, 'error');
    } finally {
      gainInFlight = false;
      if (gainQueued !== null) {
        const next = gainQueued;
        gainQueued = null;
        sendGain(next);
      }
    }
  }

  if (gainSlider) {
    gainSlider.addEventListener('input', () => {
      // Optimistic label while dragging; the read-back corrects it after.
      if (gainValue) gainValue.textContent = `${gainSlider.value}%`;
      clearTimeout(gainDebounce);
      gainDebounce = setTimeout(() => sendGain(parseInt(gainSlider.value, 10)), 150);
    });
  }

  document.querySelectorAll('[data-gain]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!gainSupported) { ZP.toast('No software input gain on this interface', 'error'); return; }
      const percent = parseInt(btn.dataset.gain, 10);
      if (gainSlider) gainSlider.value = percent;
      if (gainValue)  gainValue.textContent = `${percent}%`;
      clearTimeout(gainDebounce);
      sendGain(percent);
    });
  });

  // Read the device's real gain on page open.
  ZP.api('/gain')
    .then(applyGainUi)
    .catch(() => applyGainUi({ supported: false, percent: 0, db: null }));

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
