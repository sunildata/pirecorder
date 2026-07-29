/* Settings: loads real hardware capabilities so the form can only offer
   formats the connected interface actually supports. */

(() => {
  const $ = (id) => document.getElementById(id);

  const NUMERIC = ['sample_rate', 'bit_depth', 'channels', 'auto_split_mb',
    'auto_split_minutes', 'cleanup_threshold_pct', 'min_free_mb',
    'post_highpass_hz', 'post_normalize_lufs', 'pre_roll_seconds',
    'gpio_record_button', 'gpio_stop_button', 'gpio_status_led', 'ap_channel'];

  const BOOLEAN = ['recording_lock', 'dual_recording', 'auto_cleanup',
    'post_limiter', 'post_compressor', 'post_noise_gate', 'post_normalize',
    'auto_record_on_boot', 'hardware_enabled', 'oled_enabled', 'auth_enabled'];

  const TEXT = ['device_name', 'audio_device', 'output_format', 'mp3_bitrate',
    'take_prefix', 'wifi_mode', 'ap_ssid', 'ap_password'];

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

  function populateFormats(caps, settings) {
    const rates = caps.rates?.length ? caps.rates : [48000];
    const depths = caps.depths?.length ? caps.depths : [16];

    $('sample_rate').innerHTML = rates
      .map((r) => `<option value="${r}">${(r / 1000).toFixed(1)} kHz</option>`).join('');
    $('bit_depth').innerHTML = depths
      .map((d) => `<option value="${d}">${d}-bit</option>`).join('');

    // If the saved setting exceeds what this interface can do, show what will
    // actually be used rather than silently lying.
    if (!rates.includes(settings.sample_rate)) {
      ZP.toast(`Interface can't do ${settings.sample_rate / 1000}kHz — will use ${rates[0] / 1000}kHz`);
    }
    if (!depths.includes(settings.bit_depth)) {
      ZP.toast(`Interface is ${Math.max(...depths)}-bit — 24-bit unavailable`);
    }

    if (!caps.ffmpeg) {
      $('output_format').querySelector('[value="wav+mp3"]').disabled = true;
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

  /* ── Conditional field visibility ───────────────────────────────────────── */

  function syncMp3Options() {
    const show = $('output_format')?.value === 'wav+mp3';
    const row = $('mp3-options');
    if (row) row.style.display = show ? '' : 'none';
  }

  function syncNormalizeLufs() {
    const show = $('post_normalize')?.checked;
    const row = $('normalize-lufs-row');
    if (row) row.style.display = show ? '' : 'none';
  }

  $('output_format')?.addEventListener('change', syncMp3Options);
  $('post_normalize')?.addEventListener('change', syncNormalizeLufs);

  /* ── System info ─────────────────────────────────────────────────────────── */

  async function loadSystemInfo() {
    try {
      const s = await ZP.api('/system');
      if ($('sys-ip'))     $('sys-ip').textContent     = s.ip || '—';
      if ($('sys-host'))   $('sys-host').textContent   = s.hostname || '—';
      if ($('sys-uptime')) $('sys-uptime').textContent = s.uptime?.human || '—';
      if ($('sys-temp'))   $('sys-temp').textContent   = s.cpu_temp_c != null ? `${s.cpu_temp_c}°` : '—';
    } catch (e) { /* non-fatal */ }
  }

  $('sys-reboot')?.addEventListener('click', async () => {
    if (!confirm('Reboot the device now?')) return;
    try {
      const res = await ZP.api('/system/reboot', { method: 'POST' });
      ZP.toast(res.message || 'Rebooting…', 'ok');
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  $('sys-shutdown')?.addEventListener('click', async () => {
    if (!confirm('Shut down the device? You will need physical access to restart.')) return;
    try {
      const res = await ZP.api('/system/shutdown', { method: 'POST' });
      ZP.toast(res.message || 'Shutting down…', 'ok');
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

  load().catch((err) => ZP.toast(err.message, 'error')).then(() => {
    syncMp3Options();
    syncNormalizeLufs();
  });
  loadSystemInfo();
  wifiState();
})();
