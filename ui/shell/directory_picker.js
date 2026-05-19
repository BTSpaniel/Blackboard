// Directory picker — modal dialog backed by /api/files endpoints.
//
// Online-best-practice UX features baked in:
//   • Default initial path resolves to: caller-passed `initial` → last-used (localStorage)
//     → server-side default workspace (data/workspace) → first shortcut.
//   • Type-ahead filter narrows the visible directory list as you type.
//   • Keyboard-first navigation:
//       ↑ / ↓        — move selection
//       Enter        — enter selected dir (or confirm if it's the cwd)
//       Backspace    — go to parent
//       Esc          — cancel
//       Ctrl+L       — focus the path input (browser convention)
//       Ctrl+H       — toggle hidden files
//       Ctrl+Shift+N — new folder
//   • Pin/star folders with server-side persisted favorites.
//   • Sort by name or mtime, ascending or descending, sticky between sessions.
//   • Hidden files toggle.
//   • "+ New folder" button (calls POST /api/files/mkdir).
//   • Refresh button.
//   • Footer keymap hint so users discover shortcuts.

import { api } from '/ui/core/api.js';
import { createDialog, toast, promptDialog } from '/ui/shell/dialog.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

const LS_LAST_DIR = 'bb.picker.lastDir';
const LS_SORT = 'bb.picker.sort';      // "name:asc" | "name:desc" | "mtime:asc" | "mtime:desc"
const LS_HIDDEN = 'bb.picker.hidden';  // "1" | "0"

function fmtTime(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleDateString(); } catch { return ''; }
}

function basename(p) {
  if (!p) return '';
  const norm = p.replace(/[\\/]+$/, '');
  const m = norm.split(/[\\/]/);
  return m[m.length - 1] || norm || p;
}

function pathSep() { return navigator.platform.toLowerCase().includes('win') ? '\\' : '/'; }

/**
 * Open the directory picker.
 *
 * @param {object} opts
 * @param {string} [opts.initial]       Initial directory. If empty/falsy, the picker
 *                                      falls through last-used → workspace default.
 * @param {string} [opts.title]
 * @param {string} [opts.confirmLabel]
 * @returns {Promise<string|null>}      The chosen absolute path, or null on cancel.
 */
export function pickDirectory({ initial = '', title = 'Select working directory', confirmLabel = 'Use this directory' } = {}) {
  return new Promise((resolve) => {
    const dlg = createDialog({ title, size: 'directory' });

    let cwd = '';
    let entries = [];
    let parent = null;
    let isDriveList = false;
    let shortcuts = [];
    let favorites = [];
    let workspaceDir = '';
    let filterText = '';
    let selectedIdx = 0;
    let sort = localStorage.getItem(LS_SORT) || 'name:asc';
    let showHidden = localStorage.getItem(LS_HIDDEN) === '1';
    let resolved = false;

    dlg.body.innerHTML = `
      <div class="bb-dir">
        <aside class="bb-dir__sidebar">
          <div class="bb-dir__section-title">Shortcuts</div>
          <div id="dir-shortcuts" class="bb-dir__shortcuts"></div>
          <div class="bb-dir__section-title" id="dir-favs-title" style="display:none">Favorites</div>
          <div id="dir-favs" class="bb-dir__shortcuts"></div>
          <div class="bb-dir__section-title">Recent</div>
          <div id="dir-recents" class="bb-dir__shortcuts"></div>
        </aside>
        <main class="bb-dir__main">
          <div class="bb-dir__toolbar">
            <button class="bb-btn bb-btn--xs" id="dir-up" title="Parent (Backspace)">↑</button>
            <input type="text" id="dir-input" class="bb-input bb-dir__path" placeholder="/path/to/folder (Ctrl+L)" />
            <button class="bb-btn" id="dir-go" title="Go (Enter)">Go</button>
            <button class="bb-btn bb-btn--xs" id="dir-refresh" title="Refresh">⟳</button>
          </div>
          <div class="bb-dir__crumbs" id="dir-crumbs"></div>
          <div class="bb-dir__sub-toolbar">
            <input type="search" id="dir-filter" class="bb-input bb-dir__filter" placeholder="Filter folders…" />
            <select id="dir-sort" class="bb-input bb-dir__sort" title="Sort">
              <option value="name:asc">Name ↑</option>
              <option value="name:desc">Name ↓</option>
              <option value="mtime:desc">Modified ↓</option>
              <option value="mtime:asc">Modified ↑</option>
            </select>
            <button class="bb-btn bb-btn--xs" id="dir-hidden" title="Toggle hidden files (Ctrl+H)">${showHidden ? '👁' : '🚫'}</button>
            <button class="bb-btn bb-btn--xs" id="dir-mkdir" title="New folder (Ctrl+Shift+N)">＋ Folder</button>
            <button class="bb-btn bb-btn--xs" id="dir-pin" title="Pin current folder">☆</button>
          </div>
          <div class="bb-dir__list" id="dir-list" tabindex="0" role="listbox" aria-label="Directory contents">Loading…</div>
          <div class="bb-dir__hint">
            <kbd>↑</kbd><kbd>↓</kbd> select · <kbd>Enter</kbd> open · <kbd>⌫</kbd> up · <kbd>Esc</kbd> cancel · <kbd>Ctrl+L</kbd> path · <kbd>Ctrl+H</kbd> hidden
          </div>
        </main>
      </div>
    `;
    dlg.setFooter([
      { html: '<span id="dir-status" style="color:var(--fg-3);font-size:11px"></span>' },
      { spacer: true },
      { label: 'Cancel', onClick: () => finish(null) },
      { label: confirmLabel, primary: true, onClick: () => finish(cwd) },
    ]);

    // Apply persisted sort.
    dlg.body.querySelector('#dir-sort').value = sort;

    function finish(value) {
      if (resolved) return;
      resolved = true;
      if (value) {
        try { localStorage.setItem(LS_LAST_DIR, value); } catch { /* private mode */ }
        rememberRecent(value);
      }
      dlg.close();
      resolve(value);
    }

    // ── Recents (localStorage list of up to 8 paths) ─────────
    function loadRecents() {
      try {
        const raw = localStorage.getItem('bb.picker.recents');
        return raw ? JSON.parse(raw).slice(0, 8) : [];
      } catch { return []; }
    }
    function rememberRecent(path) {
      try {
        const list = loadRecents().filter((p) => p !== path);
        list.unshift(path);
        localStorage.setItem('bb.picker.recents', JSON.stringify(list.slice(0, 8)));
      } catch { /* ignore */ }
    }

    // ── Sidebar render ───────────────────────────────────────
    function renderSidebar() {
      const recentsEl = dlg.body.querySelector('#dir-recents');
      const recents = loadRecents();
      recentsEl.innerHTML = recents.length
        ? recents.map((p) => `<button class="bb-dir__shortcut" data-path="${escapeHtml(p)}" title="${escapeHtml(p)}">${escapeHtml(basename(p) || p)}</button>`).join('')
        : '<div style="color:var(--fg-3);font-size:11px;padding:4px 8px">No recents yet.</div>';
      recentsEl.querySelectorAll('[data-path]').forEach((el) => {
        el.addEventListener('click', () => navigate(el.dataset.path));
      });

      const favsEl = dlg.body.querySelector('#dir-favs');
      const favsTitle = dlg.body.querySelector('#dir-favs-title');
      if (favorites.length) {
        favsTitle.style.display = '';
        favsEl.innerHTML = favorites.map((f) => `
          <div class="bb-dir__shortcut bb-dir__shortcut--row">
            <button class="bb-dir__shortcut-go" data-path="${escapeHtml(f.path)}" title="${escapeHtml(f.path)}">★ ${escapeHtml(f.label)}</button>
            <button class="bb-dir__shortcut-rm" data-path="${escapeHtml(f.path)}" title="Remove favorite">×</button>
          </div>
        `).join('');
        favsEl.querySelectorAll('.bb-dir__shortcut-go').forEach((el) => {
          el.addEventListener('click', () => navigate(el.dataset.path));
        });
        favsEl.querySelectorAll('.bb-dir__shortcut-rm').forEach((el) => {
          el.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
              const r = await api.fsRemoveFavorite(el.dataset.path);
              favorites = r.favorites || [];
              renderSidebar();
              updatePinButton();
            } catch (err) { toast(`Unpin failed: ${err.message}`, { kind: 'error' }); }
          });
        });
      } else {
        favsTitle.style.display = 'none';
        favsEl.innerHTML = '';
      }
    }

    function renderShortcuts() {
      const el = dlg.body.querySelector('#dir-shortcuts');
      el.innerHTML = shortcuts.map((s) => {
        const icon = s.kind === 'workspace' ? '🗂' : '📁';
        const cls = s.kind === 'workspace' ? 'bb-dir__shortcut bb-dir__shortcut--workspace' : 'bb-dir__shortcut';
        return `<button class="${cls}" data-path="${escapeHtml(s.path)}" title="${escapeHtml(s.path)}">${icon} ${escapeHtml(s.label)}</button>`;
      }).join('');
      el.querySelectorAll('[data-path]').forEach((btn) => {
        btn.addEventListener('click', () => navigate(btn.dataset.path));
      });
    }

    // ── Crumbs (clickable path segments) ──────────────────────
    function renderCrumbs() {
      const el = dlg.body.querySelector('#dir-crumbs');
      if (isDriveList) { el.innerHTML = '<span class="bb-dir__crumb-sep">My Computer</span>'; return; }
      if (!cwd) { el.innerHTML = ''; return; }
      const sep = pathSep();
      const parts = cwd.split(/[\\/]+/).filter(Boolean);
      const winRoot = /^[A-Za-z]:$/.test(parts[0] || '') ? parts[0] + sep : '';
      let acc = winRoot || sep;
      const crumbs = [];
      if (winRoot) {
        crumbs.push(`<span class="bb-dir__crumb" data-path="${escapeHtml(winRoot)}">${escapeHtml(winRoot)}</span>`);
        parts.shift();
      } else {
        crumbs.push(`<span class="bb-dir__crumb" data-path="/">/</span>`);
      }
      parts.forEach((seg) => {
        acc = acc.endsWith(sep) ? acc + seg : acc + sep + seg;
        crumbs.push('<span class="bb-dir__crumb-sep">›</span>');
        crumbs.push(`<span class="bb-dir__crumb" data-path="${escapeHtml(acc)}">${escapeHtml(seg)}</span>`);
      });
      el.innerHTML = crumbs.join(' ');
      el.querySelectorAll('[data-path]').forEach((c) => {
        c.addEventListener('click', () => navigate(c.dataset.path));
      });
    }

    // ── Listing ───────────────────────────────────────────────
    function getVisibleEntries() {
      const q = filterText.toLowerCase();
      let list = entries.filter((e) => !q || e.name.toLowerCase().includes(q));
      const [field, dir] = sort.split(':');
      const mult = dir === 'desc' ? -1 : 1;
      list.sort((a, b) => {
        if (field === 'mtime') return mult * ((a.mtime || 0) - (b.mtime || 0));
        return mult * a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
      });
      return list;
    }

    function renderList() {
      const list = dlg.body.querySelector('#dir-list');
      const visible = getVisibleEntries();
      if (!visible.length) {
        list.innerHTML = `<div class="bb-dir__empty">${filterText ? 'No matches.' : '(empty folder)'}</div>`;
        updateStatus();
        return;
      }
      if (selectedIdx >= visible.length) selectedIdx = visible.length - 1;
      if (selectedIdx < 0) selectedIdx = 0;
      list.innerHTML = visible.map((e, i) => `
        <div class="bb-dir__row ${i === selectedIdx ? 'bb-dir__row--active' : ''}" data-idx="${i}" data-path="${escapeHtml(e.path)}" role="option" aria-selected="${i === selectedIdx}">
          <span class="bb-dir__icon">${e.type === 'drive' ? '💽' : '📁'}</span>
          <span class="bb-dir__name">${escapeHtml(e.name)}</span>
          <span class="bb-dir__time">${fmtTime(e.mtime)}</span>
        </div>
      `).join('');
      list.querySelectorAll('.bb-dir__row').forEach((row) => {
        row.addEventListener('click', () => {
          selectedIdx = Number(row.dataset.idx);
          renderList();
        });
        row.addEventListener('dblclick', () => navigate(row.dataset.path));
      });
      // Keep the active row in view.
      const active = list.querySelector('.bb-dir__row--active');
      if (active) active.scrollIntoView({ block: 'nearest' });
      updateStatus();
    }

    function updateStatus() {
      const status = dlg.body.querySelector('#dir-status');
      if (!status) return;
      if (isDriveList) { status.textContent = `${entries.length} drive${entries.length === 1 ? '' : 's'}`; return; }
      const visible = getVisibleEntries();
      status.textContent = filterText
        ? `${visible.length} of ${entries.length} folder${entries.length === 1 ? '' : 's'} · ${escapeHtml(cwd)}`
        : `${entries.length} folder${entries.length === 1 ? '' : 's'} · ${cwd}`;
    }

    function updatePinButton() {
      const btn = dlg.body.querySelector('#dir-pin');
      if (!btn) return;
      const isPinned = favorites.some((f) => f.path === cwd);
      btn.textContent = isPinned ? '★' : '☆';
      btn.title = isPinned ? 'Unpin this folder' : 'Pin this folder to favorites';
      btn.disabled = !cwd || isDriveList;
    }

    // ── Navigation ────────────────────────────────────────────
    async function navigate(path) {
      try {
        const data = await api.listDir(path || '', { onlyDirs: true, showHidden });
        cwd = data.cwd || '';
        parent = data.parent;
        entries = data.entries || [];
        isDriveList = !!data.is_drive_list;
        filterText = '';
        selectedIdx = 0;
        const filterEl = dlg.body.querySelector('#dir-filter');
        if (filterEl) filterEl.value = '';
        const inputEl = dlg.body.querySelector('#dir-input');
        if (inputEl) inputEl.value = cwd || '';
        renderCrumbs();
        renderList();
        updatePinButton();
        dlg.body.querySelector('#dir-up').disabled = !parent;
      } catch (err) {
        toast(`Could not open: ${err.message}`, { kind: 'error', timeout: 4000 });
      }
    }

    // ── Toolbar wiring ────────────────────────────────────────
    const inputEl = dlg.body.querySelector('#dir-input');
    const filterEl = dlg.body.querySelector('#dir-filter');
    const sortEl = dlg.body.querySelector('#dir-sort');

    dlg.body.querySelector('#dir-go').addEventListener('click', () => navigate(inputEl.value.trim()));
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); navigate(inputEl.value.trim()); }
    });
    dlg.body.querySelector('#dir-up').addEventListener('click', () => parent && navigate(parent));
    dlg.body.querySelector('#dir-refresh').addEventListener('click', () => navigate(cwd));
    filterEl.addEventListener('input', (e) => { filterText = e.target.value; selectedIdx = 0; renderList(); });
    sortEl.addEventListener('change', (e) => {
      sort = e.target.value;
      try { localStorage.setItem(LS_SORT, sort); } catch { /* */ }
      renderList();
    });
    dlg.body.querySelector('#dir-hidden').addEventListener('click', () => {
      showHidden = !showHidden;
      try { localStorage.setItem(LS_HIDDEN, showHidden ? '1' : '0'); } catch { /* */ }
      const btn = dlg.body.querySelector('#dir-hidden');
      btn.textContent = showHidden ? '👁' : '🚫';
      navigate(cwd);
    });
    dlg.body.querySelector('#dir-mkdir').addEventListener('click', createFolder);
    dlg.body.querySelector('#dir-pin').addEventListener('click', togglePin);

    async function createFolder() {
      if (!cwd || isDriveList) { toast('Pick a folder first', { kind: 'warn' }); return; }
      const name = await promptDialog({
        title: 'New folder',
        label: `Create inside ${cwd}`,
        placeholder: 'folder-name',
        confirmLabel: 'Create',
      });
      if (!name) return;
      try {
        const r = await api.fsMkdir(cwd, name.trim());
        toast(`Created ${name}`, { kind: 'success', timeout: 1500 });
        await navigate(cwd);
        // Auto-select the freshly-created folder.
        const visible = getVisibleEntries();
        const idx = visible.findIndex((e) => e.path === r.path);
        if (idx >= 0) { selectedIdx = idx; renderList(); }
      } catch (err) { toast(`Create failed: ${err.message}`, { kind: 'error' }); }
    }

    async function togglePin() {
      if (!cwd || isDriveList) return;
      const isPinned = favorites.some((f) => f.path === cwd);
      try {
        const r = isPinned
          ? await api.fsRemoveFavorite(cwd)
          : await api.fsAddFavorite(cwd, basename(cwd));
        favorites = r.favorites || [];
        renderSidebar();
        updatePinButton();
      } catch (err) { toast(`Pin failed: ${err.message}`, { kind: 'error' }); }
    }

    // ── Keyboard navigation on the list ───────────────────────
    function handleKey(e) {
      const tag = (e.target?.tagName || '').toLowerCase();
      const inField = tag === 'input' || tag === 'textarea' || tag === 'select';

      if (e.key === 'Escape') { e.preventDefault(); finish(null); return; }
      if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) { e.preventDefault(); inputEl.focus(); inputEl.select(); return; }
      if (e.ctrlKey && (e.key === 'h' || e.key === 'H')) { e.preventDefault(); dlg.body.querySelector('#dir-hidden').click(); return; }
      if (e.ctrlKey && e.shiftKey && (e.key === 'N' || e.key === 'n')) { e.preventDefault(); createFolder(); return; }

      // List navigation only when not inside the path/filter inputs.
      if (inField && tag === 'input' && e.target.id !== 'dir-filter' && e.target.id !== 'dir-input') return;

      const visible = getVisibleEntries();
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (visible.length) { selectedIdx = Math.min(visible.length - 1, selectedIdx + 1); renderList(); }
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (visible.length) { selectedIdx = Math.max(0, selectedIdx - 1); renderList(); }
      } else if (e.key === 'Enter' && tag !== 'input') {
        e.preventDefault();
        if (visible[selectedIdx]) navigate(visible[selectedIdx].path);
        else if (cwd) finish(cwd);
      } else if (e.key === 'Backspace' && tag !== 'input') {
        e.preventDefault();
        if (parent) navigate(parent);
      }
    }
    dlg.body.addEventListener('keydown', handleKey);

    // ── Boot ──────────────────────────────────────────────────
    (async () => {
      let home = { shortcuts: [], default_workspace: '', favorites: [] };
      try { home = await api.fsHome(); } catch { /* keep empty */ }
      shortcuts = home.shortcuts || [];
      favorites = home.favorites || [];
      workspaceDir = home.default_workspace || '';
      renderShortcuts();
      renderSidebar();

      // Resolve initial path: explicit caller value (if non-trivial) → last-used → workspace.
      let target = (initial || '').trim();
      const trivial = !target || target === '.' || target === './';
      if (trivial) {
        let last = '';
        try { last = localStorage.getItem(LS_LAST_DIR) || ''; } catch { /* */ }
        if (last) {
          // Validate it still exists; fall back to workspace if not.
          try {
            const probe = await api.fsProbe(last);
            target = probe.exists && probe.is_dir ? last : '';
          } catch { target = ''; }
        }
        if (!target) target = workspaceDir;
        if (!target && shortcuts[0]) target = shortcuts[0].path;
      }
      navigate(target);
      // Pre-focus the list so arrow keys work immediately.
      setTimeout(() => dlg.body.querySelector('#dir-list')?.focus(), 0);
    })();

    dlg.onClose(() => finish(null));
    dlg.open();
  });
}
