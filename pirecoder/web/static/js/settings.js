/* Settings: loads real hardware capabilities so the form can only offer
   formats the connected interface actually supports. */

(() => {
  const $ = (id) => document.getElementById(id);

  const NUMERIC = ['sample_rate', 'bit_depth', 'channels', 'auto_split_mb',
    'auto_split_minutes', 'cleanup_threshold_pct', 'min_free_mb',
    'post_highpass_hz', 'gpio_record_button', 'gpio_stop_button',
    'gpio_status_led'];

  const BOOLEAN = ['recording_lock', 'dual_recording', 'auto_cleanup',
    'post_limiter', 'post_compressor', 'post_noise_gate',
    'hardware_enabled', 'oled_enabled', 'auth_enabled'];

  const TEXT = ['device_name', 'audio_device', 'output_format',
    'wifi_mode', 'ap_ssid', 'ap_password'];

  const ALL = [...NUMERIC, ...BOOLEAN, ...TEXT];

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  /* ── Load ────────────────────────────────────────────────────────────── */

  async function load() {
    const [{ settings, capabilities }, devices] = await Promise.all([
      ZP.api('/settings'),
      ZP.api('/devices').catch(() => ({ devices: [], active: null })),
    ]);

    populateDevices(devices);
    populateFormats(capabilities, settings);

    ALL.forEach((k) => {
      const el = $(k);
      if (!el || settings[k] === undefined) return;
      if (el.type === 'checkbox') el.checked = Boolean(settings[k]);
      else el.value = settings[k];
    });

    snapToSupported(settings);

    // Password is never returned by the API; leave the field blank.
    $('ap_password').value = settings.ap_password ?? '';
  }

  function populateDevices({ devices, active }) {
    const sel = $('audio_device');
    sel.innerHTML = '<option value="auto">Auto-detect (prefer USB)</option>' +
      devices.map((d) =>
        `<option value="${escapeHtml(d.alsa_id)}">${escapeHtml(d.name)} (${d.alsa_id})</option>`
      ).join('');

    if (active) {
      const rates = active.supported_rates.map((r) => `${r / 1000}k`).join(', ') || 'unknown';
      const depths = active.supported_depths.map((d) => `${d}-bit`).join(', ') || 'unknown';
      $('device-info').textContent =
        `${active.name} · supports ${rates} @ ${depths}`;
    } else {
      $('device-info').textContent = 'No capture device detected';
    }
  }

  // Mirrors negotiate_format() on the server so the warning names the format
  // that will really be used, not a guess.
  function nearestRate(want, rates) {
    return rates.reduce((best, r) =>
      Math.abs(r - want) < Math.abs(best - want) ? r : best, rates[0]);
  }

  function nearestDepth(want, depths) {
    const below = depths.filter((d) => d < want);
    return below.length ? Math.max(...below) : Math.min(...depths);
  }

  // What the connected interface can actually do, as last reported.
  const hw = { rates: [48000], depths: [16], maxChannels: 2 };

  // Assigning an unsupported value to a <select> leaves it with no selection
  // and an empty value, which collect() would then save as 0 — a format the
  // recorder cannot open. Show the fallback the server would negotiate.
  function snapToSupported(settings) {
    if (!hw.rates.includes(settings.sample_rate)) {
      $('sample_rate').value = nearestRate(settings.sample_rate, hw.rates);
    }
    if (!hw.depths.includes(settings.bit_depth)) {
      $('bit_depth').value = nearestDepth(settings.bit_depth, hw.depths);
    }
    if (settings.channels > hw.maxChannels) {
      $('channels').value = hw.maxChannels;
    }
  }

  function populateFormats(caps, settings) {
    const rates = caps.rates?.length ? caps.rates : [48000];
    const depths = caps.depths?.length ? caps.depths : [16];
    const maxChannels = caps.max_channels || 2;
    Object.assign(hw, { rates, depths, maxChannels });

    $('sample_rate').innerHTML = rates
      .map((r) => `<option value="${r}">${(r / 1000).toFixed(1)} kHz</option>`).join('');
    $('bit_depth').innerHTML = depths
      .map((d) => `<option value="${d}">${d}-bit</option>`).join('');

    // A mono-only interface must not offer Stereo: arecord would refuse the
    // format and the take would fail at the moment it matters most.
    const stereo = $('channels').querySelector('[value="2"]');
    if (stereo) {
      stereo.disabled = maxChannels < 2;
      if (maxChannels < 2) stereo.textContent = 'Stereo (not supported)';
    }

    // If the saved setting exceeds what this interface can do, show what will
    // actually be used rather than silently lying.
    if (!rates.includes(settings.sample_rate)) {
      ZP.toast(`Interface can't do ${settings.sample_rate / 1000} kHz — `
        + `will use ${nearestRate(settings.sample_rate, rates) / 1000} kHz`);
    }
    if (!depths.includes(settings.bit_depth)) {
      ZP.toast(`Interface can't do ${settings.bit_depth}-bit — `
        + `will use ${nearestDepth(settings.bit_depth, depths)}-bit`);
    }
    if (maxChannels < 2 && settings.channels > 1) {
      ZP.toast('Interface is mono — will record 1 channel');
    }

    // Grey out what this FFmpeg build cannot encode instead of letting a take
    // finish and the conversion fail an hour later.
    const fmt = $('output_format');
    const missing = [];
    if (!caps.flac) missing.push('FLAC');
    if (!caps.mp3) missing.push('MP3');
    fmt.querySelectorAll('option').forEach((opt) => {
      const needsFlac = opt.value.includes('flac');
      const needsMp3 = opt.value.includes('mp3');
      opt.disabled = (needsFlac && !caps.flac) || (needsMp3 && !caps.mp3);
    });
    if (missing.length) {
      $('format-info').textContent = caps.ffmpeg
        ? `${missing.join(' and ')} unavailable — this FFmpeg build lacks the encoder.`
        : 'FFmpeg is not installed — only WAV is available. Re-run install.sh.';
    }
  }

  /* ── Save ────────────────────────────────────────────────────────────── */

  function collect() {
    const out = {};
    ALL.forEach((k) => {
      const el = $(k);
      if (!el) return;
      if (el.type === 'checkbox') out[k] = el.checked;
      else if (NUMERIC.includes(k)) out[k] = Number(el.value) || 0;
      else out[k] = el.value;
    });
    return out;
  }

  $('save').addEventListener('click', async () => {
    try {
      await ZP.api('/settings', { method: 'POST', body: collect() });
      ZP.toast('Settings saved', 'ok');
      load();
    } catch (err) {
      if (err.payload?.blocked) {
        ZP.toast(`Locked while recording: ${err.payload.blocked.join(', ')}`, 'error');
      } else {
        ZP.toast(err.message, 'error');
      }
    }
  });

  $('save-password').addEventListener('click', async () => {
    const value = $('new_password').value;
    if (!value) return;
    try {
      await ZP.api('/password', { method: 'POST', body: { new_password: value } });
      $('new_password').value = '';
      ZP.toast('Password updated', 'ok');
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  $('run-cleanup').addEventListener('click', async () => {
    try {
      const res = await ZP.api('/storage/cleanup', { method: 'POST', body: { force: true } });
      ZP.toast(res.ran ? `Freed ${res.freed_mb} MB` : res.reason, 'ok');
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  /* ── Input gain ──────────────────────────────────────────────────────────
     Gain is applied straight to the ALSA mixer and saved by the API, so it is
     deliberately *not* part of the Save Settings payload — moving the slider
     is the commit. */

  const gainSlider = $('settings-gain');
  const gainValue  = $('settings-gain-value');
  const gainStatus = $('settings-gain-status');
  let gainSupported = false;
  let gainDebounce = null;
  let gainInFlight = false;
  let gainQueued = null;

  function gainLabel(percent, db) {
    return db === null || db === undefined
      ? `${percent}%`
      : `${percent}% (${db > 0 ? '+' : ''}${db.toFixed(1)} dB)`;
  }

  function applyGainUi(res) {
    gainSupported = !!res.supported;
    gainSlider.disabled = !gainSupported;
    gainSlider.value = res.percent ?? 0;
    gainValue.textContent = gainSupported ? gainLabel(res.percent, res.db) : '—';
    gainStatus.textContent = gainSupported
      ? `Using ALSA control "${res.control}"`
      : (res.reason || 'This interface has no software input gain — '
                     + 'use its hardware gain knob.');
  }

  async function sendGain(percent) {
    // Serialise: a dragged slider would otherwise queue dozens of amixer calls
    // and the last reply to land could be an out-of-date value.
    if (gainInFlight) { gainQueued = percent; return; }
    gainInFlight = true;
    try {
      applyGainUi(await ZP.api('/gain', { method: 'POST', body: { percent } }));
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

  gainSlider.addEventListener('input', () => {
    gainValue.textContent = `${gainSlider.value}%`;
    clearTimeout(gainDebounce);
    gainDebounce = setTimeout(() => sendGain(Number(gainSlider.value)), 150);
  });

  document.querySelectorAll('[data-gain]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!gainSupported) {
        ZP.toast('No software input gain on this interface', 'error');
        return;
      }
      const percent = Number(btn.dataset.gain);
      gainSlider.value = percent;
      gainValue.textContent = `${percent}%`;
      clearTimeout(gainDebounce);
      sendGain(percent);
    });
  });

  ZP.api('/gain')
    .then(applyGainUi)
    .catch(() => applyGainUi({ supported: false, percent: 0, db: null }));

  /* ── Wi-Fi ───────────────────────────────────────────────────────────── */

  async function wifiState() {
    try {
      const s = await ZP.api('/wifi/status');
      if (!s.available) {
        $('wifi-state').textContent = 'NetworkManager not available on this system';
        return;
      }
      $('wifi-state').textContent = s.connected
        ? `${s.mode === 'ap' ? 'Hosting' : 'Connected to'} ${s.ssid} · ${s.ip || 'no IP'}${s.signal ? ` · ${s.signal}%` : ''}`
        : 'Not connected';
    } catch (e) { $('wifi-state').textContent = 'Wi-Fi status unavailable'; }
  }

  $('wifi-scan').addEventListener('click', async () => {
    const box = $('wifi-list');
    box.innerHTML = '<div class="file-meta">Scanning…</div>';
    try {
      const nets = await ZP.api('/wifi/scan');
      if (!nets.length) { box.innerHTML = '<div class="file-meta">No networks found</div>'; return; }
      box.innerHTML = nets.slice(0, 15).map((n) => `
        <div class="file">
          <div class="file-main">
            <div class="file-name">${escapeHtml(n.ssid)}${n.saved ? ' ✓' : ''}</div>
            <div class="file-meta">${n.signal}% · ${escapeHtml(n.security)}</div>
          </div>
          <button class="btn btn-ghost btn-sm" data-ssid="${escapeHtml(n.ssid)}"
                  data-secure="${n.security !== 'open' ? '1' : ''}">Join</button>
        </div>`).join('');

      box.querySelectorAll('[data-ssid]').forEach((btn) => {
        btn.addEventListener('click', () => joinNetwork(btn.dataset.ssid, btn.dataset.secure));
      });
    } catch (err) { box.innerHTML = `<div class="file-meta">${escapeHtml(err.message)}</div>`; }
  });

  async function joinNetwork(ssid, secure) {
    const password = secure ? prompt(`Password for ${ssid}:`) : '';
    if (secure && password === null) return;
    ZP.toast(`Connecting to ${ssid}…`);
    try {
      const res = await ZP.api('/wifi/connect', {
        method: 'POST',
        body: { ssid, password: password || '' },
      });
      ZP.toast(res.ok ? `Connected to ${ssid}` : res.error, res.ok ? 'ok' : 'error');
      wifiState();
    } catch (err) { ZP.toast(err.message, 'error'); }
  }

  $('wifi-ap').addEventListener('click', async () => {
    if (!confirm('Start hotspot? You will need to reconnect your phone to it.')) return;
    try {
      await ZP.api('/wifi/ap/start', {
        method: 'POST',
        body: { ssid: $('ap_ssid').value, password: $('ap_password').value },
      });
      ZP.toast('Hotspot starting — reconnect your phone', 'ok');
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  $('wifi-auto').addEventListener('click', async () => {
    ZP.toast('Running auto-connect…');
    try {
      await ZP.api('/wifi/auto', { method: 'POST' });
      wifiState();
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  /* ── Recording lock warning ──────────────────────────────────────────── */

  ZP.on('status', (s) => {
    const warn = $('rec-warning');
    warn.className = s.is_recording ? 'banner' : 'banner hidden';
    ['sample_rate', 'bit_depth', 'channels', 'audio_device'].forEach((k) => {
      const el = $(k);
      if (el) el.disabled = s.is_recording;
    });
  });

  ZP.api('/hardware').then((hw) => {
    $('hw-state').textContent = hw.gpio_available
      ? 'gpiozero detected — restart service after changing'
      : 'gpiozero not installed';
  }).catch(() => {});

  load().catch((err) => ZP.toast(err.message, 'error'));
  wifiState();
})();
