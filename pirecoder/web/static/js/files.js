/* File library: search, sort, playback, rename, delete, bulk export. */

(() => {
  const $ = (id) => document.getElementById(id);
  const list = $('list');
  const selected = new Set();
  let files = [];

  const key = (f) => `${f.folder}/${f.filename}`;

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  /* ── Load ────────────────────────────────────────────────────────────── */

  async function load() {
    const params = new URLSearchParams({
      search: $('search').value.trim(),
      sort: $('sort').value,
      folder: $('folder').value,
    });
    try {
      const data = await ZP.api(`/recordings?${params}`);
      files = data.files;
      renderStats(data.stats);
      render();
    } catch (err) {
      list.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    }
  }

  async function loadFolders() {
    try {
      const folders = await ZP.api('/folders');
      $('folder').innerHTML =
        '<option value="">All days</option>' +
        folders.map((f) => `<option value="${f.name}">${f.name} (${f.count})</option>`).join('');
    } catch (e) { /* non-fatal */ }
  }

  function renderStats(s) {
    if (!s) return;
    $('t-count').textContent = s.total_files;
    $('t-size').textContent = s.total_size_mb > 1024
      ? `${(s.total_size_mb / 1024).toFixed(1)}G` : `${Math.round(s.total_size_mb)}M`;
    $('t-hours').textContent = s.total_duration_hours;
    $('t-days').textContent = s.folders;
  }

  /* ── Render ──────────────────────────────────────────────────────────── */

  function render() {
    if (!files.length) {
      list.innerHTML = '<div class="empty">No recordings found</div>';
      updateBulkBar();
      return;
    }

    // Group by day folder so a long event reads chronologically.
    const groups = {};
    files.forEach((f) => (groups[f.folder] = groups[f.folder] || []).push(f));

    list.innerHTML = Object.keys(groups).sort().reverse().map((folder) => `
      <div class="folder-head">
        <span>${escapeHtml(folder)}</span>
        <a href="/api/folders/${encodeURIComponent(folder)}/export"
           class="btn btn-ghost btn-sm" style="text-decoration:none">ZIP day</a>
      </div>
      ${groups[folder].map(row).join('')}
    `).join('');

    bindRows();
    updateBulkBar();
  }

  function row(f) {
    const id = key(f);
    const dur = f.duration ? ZP.hms(f.duration) : '—';
    return `
      <div class="file" data-key="${escapeHtml(id)}">
        <input type="checkbox" class="file-check" ${selected.has(id) ? 'checked' : ''}>
        <div class="file-main">
          <div class="file-name">${escapeHtml(f.filename)}</div>
          <div class="file-meta">${dur} · ${ZP.bytes(f.size_bytes)} · ${ZP.when(f.modified)}</div>
          <div class="player"></div>
        </div>
        <div class="file-actions">
          <button class="icon-btn" data-act="play"   title="Play">▶</button>
          <a class="icon-btn" href="/api/recordings/${encodeURIComponent(f.folder)}/${encodeURIComponent(f.filename)}/download"
             title="Download" style="text-decoration:none">↓</a>
          <button class="icon-btn" data-act="rename" title="Rename">✎</button>
          <button class="icon-btn danger" data-act="delete" title="Delete">✕</button>
        </div>
      </div>`;
  }

  function bindRows() {
    list.querySelectorAll('.file').forEach((rowEl) => {
      const id = rowEl.dataset.key;
      const [folder, filename] = splitKey(id);

      rowEl.querySelector('.file-check').addEventListener('change', (e) => {
        e.target.checked ? selected.add(id) : selected.delete(id);
        updateBulkBar();
      });

      rowEl.querySelectorAll('[data-act]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const act = btn.dataset.act;
          if (act === 'play') togglePlayer(rowEl, folder, filename);
          if (act === 'rename') doRename(folder, filename);
          if (act === 'delete') doDelete(folder, filename);
        });
      });
    });
  }

  function splitKey(id) {
    const i = id.indexOf('/');
    return [id.slice(0, i), id.slice(i + 1)];
  }

  /* ── Actions ─────────────────────────────────────────────────────────── */

  function togglePlayer(rowEl, folder, filename) {
    const holder = rowEl.querySelector('.player');
    if (holder.firstChild) { holder.innerHTML = ''; return; }

    // Only one player at a time — otherwise a phone tries to decode several
    // multi-hundred-megabyte WAVs simultaneously.
    list.querySelectorAll('.player').forEach((p) => (p.innerHTML = ''));

    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.src = `/api/recordings/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/stream`;
    holder.appendChild(audio);
    audio.play().catch(() => ZP.toast('Tap play again to start audio'));
  }

  async function doRename(folder, filename) {
    const base = filename.replace(/\.[^.]+$/, '');
    const next = prompt('New name:', base);
    if (!next || next === base) return;
    try {
      const res = await ZP.api(
        `/recordings/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/rename`,
        { method: 'POST', body: { new_name: next } }
      );
      ZP.toast(`Renamed to ${res.renamed_to}`, 'ok');
      selected.clear();
      load();
    } catch (err) { ZP.toast(err.message, 'error'); }
  }

  async function doDelete(folder, filename) {
    if (!confirm(`Delete ${filename}?\n\nThis cannot be undone.`)) return;
    try {
      await ZP.api(
        `/recordings/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`,
        { method: 'DELETE' }
      );
      selected.delete(`${folder}/${filename}`);
      ZP.toast('Deleted', 'ok');
      load();
      loadFolders();
    } catch (err) { ZP.toast(err.message, 'error'); }
  }

  /* ── Bulk ────────────────────────────────────────────────────────────── */

  function updateBulkBar() {
    const bar = $('bulk-bar');
    bar.style.display = selected.size ? 'block' : 'none';
    $('bulk-count').textContent = `${selected.size} selected`;
  }

  function selectedItems() {
    return [...selected].map((id) => {
      const [folder, filename] = splitKey(id);
      return { folder, filename };
    });
  }

  $('bulk-clear').addEventListener('click', () => {
    selected.clear();
    render();
  });

  $('bulk-delete').addEventListener('click', async () => {
    if (!confirm(`Delete ${selected.size} recording(s)?\n\nThis cannot be undone.`)) return;
    try {
      const res = await ZP.api('/recordings/delete-many', {
        method: 'POST',
        body: { items: selectedItems() },
      });
      ZP.toast(`Deleted ${res.deleted.length}`, 'ok');
      selected.clear();
      load();
      loadFolders();
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  $('bulk-zip').addEventListener('click', async () => {
    ZP.toast('Building ZIP…');
    try {
      const res = await fetch('/api/recordings/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: selectedItems() }),
      });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `recordings-${Date.now()}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) { ZP.toast(err.message, 'error'); }
  });

  /* ── Filters ─────────────────────────────────────────────────────────── */

  let debounce;
  $('search').addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(load, 250);
  });
  $('sort').addEventListener('change', load);
  $('folder').addEventListener('change', load);

  // A take that just finished should appear without a manual refresh.
  ZP.on('recording_stopped', () => { load(); loadFolders(); });

  loadFolders();
  load();
})();
