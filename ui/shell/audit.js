// Audit panel — search + kind-filter chips + color-coded rows + auto-refresh toggle.
import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { createDialog, toast } from '/ui/shell/dialog.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function colorForKind(kind) {
  const k = String(kind || '').toLowerCase();
  if (k.startsWith('provider.')) return 'bb-audit__row--provider';
  if (k.startsWith('coding.') || k.includes('job'))  return 'bb-audit__row--coding';
  if (k.startsWith('card.') || k.includes('board')) return 'bb-audit__row--card';
  if (k.includes('error') || k.includes('fail'))    return 'bb-audit__row--error';
  if (k.includes('playwright') || k.includes('preview') || k.includes('terminal')) return 'bb-audit__row--exec';
  return 'bb-audit__row--default';
}

function fmtTime(ts) {
  try { const d = new Date((ts || 0) * 1000); return d.toLocaleTimeString(); } catch { return ''; }
}
function fmtPayload(payload) {
  if (!payload) return '';
  try { return JSON.stringify(payload); } catch { return String(payload); }
}

function entryKey(entry) {
  return String(entry?.id || `${entry?.ts || 0}:${entry?.kind || ''}`);
}

function entryRenderSignature(entry) {
  return `${entryKey(entry)}|${entry?.ts || 0}|${entry?.kind || ''}|${fmtPayload(entry?.payload)}`;
}

function syncChildren(parent, nodes) {
  let cursor = parent.firstChild;
  for (const node of nodes) {
    if (node === cursor) {
      cursor = cursor?.nextSibling || null;
      continue;
    }
    parent.insertBefore(node, cursor || null);
  }
  while (cursor) {
    const next = cursor.nextSibling;
    parent.removeChild(cursor);
    cursor = next;
  }
}

export async function openAuditPanel() {
  const projectId = store.get().activeProjectId;
  if (!projectId) { toast('No active project', { kind: 'warn' }); return; }

  const dlg = createDialog({ title: 'Audit log', size: 'xl' });
  dlg.body.innerHTML = `
    <div class="bb-audit">
      <div class="bb-audit__toolbar">
        <input type="search" id="audit-search" class="bb-input" placeholder="Search kind or payload…" />
        <span style="flex:1"></span>
        <button class="bb-btn" id="audit-refresh">Refresh</button>
        <button class="bb-btn" id="audit-export" title="Copy JSON to clipboard">Export</button>
      </div>
      <div class="bb-audit__chips" id="audit-chips"></div>
      <div class="bb-audit__list" id="audit-list">Loading…</div>
      <div class="bb-audit__status" id="audit-status"></div>
    </div>
  `;
  dlg.setFooter([
    { html: '<span style="color:var(--fg-3);font-size:11px">Latest 200 events. Click a chip to filter by kind.</span>' },
    { spacer: true },
    { label: 'Close', primary: true, onClick: () => dlg.close() },
  ]);
  dlg.open();

  const searchInput = dlg.body.querySelector('#audit-search');
  const chipsHost = dlg.body.querySelector('#audit-chips');
  const listHost = dlg.body.querySelector('#audit-list');
  const statusEl = dlg.body.querySelector('#audit-status');

  let allEntries = [];
  let activeKindFilters = new Set();
  let autoTimer = null;
  let loadTimer = null;
  let loading = false;
  let queued = false;
  let closed = false;
  const unsubscribers = [];
  const chipNodes = new Map();
  const rowNodes = new Map();
  let renderedEntriesSignature = '';
  let renderedChipsSignature = '';
  let renderedFilterSignature = '';
  let emptyListNode = null;

  function applyFilters() {
    const q = searchInput.value.trim().toLowerCase();
    const activeFilterSignature = Array.from(activeKindFilters).sort().join('|');
    const filtered = allEntries.filter((e) => {
      const kind = (e.kind || '').toLowerCase();
      if (activeKindFilters.size > 0 && !activeKindFilters.has(kind)) return false;
      if (q) {
        const blob = `${kind} ${fmtPayload(e.payload)}`.toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    });
    const filterSignature = `${q}\n${activeFilterSignature}\n${filtered.map((entry) => entryKey(entry)).join('|')}`;
    renderList(filtered, filterSignature);
    statusEl.textContent = `${filtered.length} / ${allEntries.length} entries · live`;
  }

  function renderChips() {
    const counts = new Map();
    for (const e of allEntries) {
      const k = (e.kind || '(unknown)').toLowerCase();
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
    const chipsSignature = `${sorted.map(([kind, count]) => `${kind}:${count}`).join('|')}\n${Array.from(activeKindFilters).sort().join('|')}`;
    if (chipsSignature === renderedChipsSignature) return;
    renderedChipsSignature = chipsSignature;
    if (!sorted.length) {
      if (chipsHost.dataset.empty !== '1') {
        chipsHost.innerHTML = '<span style="color:var(--fg-3);font-size:11px">No events yet.</span>';
        chipsHost.dataset.empty = '1';
      }
      return;
    }
    delete chipsHost.dataset.empty;
    const nextNodes = [];
    const seenKinds = new Set();
    for (const [kind, count] of sorted) {
      const active = activeKindFilters.has(kind);
      let chip = chipNodes.get(kind);
      if (!chip) {
        chip = document.createElement('span');
        chip.addEventListener('click', () => {
          const k = chip.dataset.kind;
          if (activeKindFilters.has(k)) activeKindFilters.delete(k);
          else activeKindFilters.add(k);
          renderChips();
          applyFilters();
        });
        chipNodes.set(kind, chip);
      }
      chip.className = `bb-chip ${active ? 'bb-chip--active' : ''}`;
      chip.dataset.kind = kind;
      if (chip.dataset.count !== String(count) || chip.dataset.active !== (active ? '1' : '0')) {
        chip.innerHTML = `${escapeHtml(kind)} <span style="opacity:0.7;margin-left:4px">${count}</span>`;
        chip.dataset.count = String(count);
        chip.dataset.active = active ? '1' : '0';
      }
      nextNodes.push(chip);
      seenKinds.add(kind);
    }
    for (const [kind, chip] of chipNodes.entries()) {
      if (seenKinds.has(kind)) continue;
      if (chip.parentNode === chipsHost) chipsHost.removeChild(chip);
      chipNodes.delete(kind);
    }
    syncChildren(chipsHost, nextNodes);
  }

  function renderList(entries, filterSignature = '') {
    if (filterSignature && filterSignature === renderedFilterSignature) return;
    renderedFilterSignature = filterSignature;
    if (!entries.length) {
      if (!emptyListNode) {
        emptyListNode = document.createElement('div');
        emptyListNode.style.color = 'var(--fg-3)';
        emptyListNode.style.padding = '18px';
        emptyListNode.style.textAlign = 'center';
        emptyListNode.textContent = 'No events match the current filters.';
      }
      syncChildren(listHost, [emptyListNode]);
      return;
    }
    const nextNodes = [];
    const seenKeys = new Set();
    for (const entry of entries.slice().reverse()) {
      const key = entryKey(entry);
      const signature = entryRenderSignature(entry);
      let row = rowNodes.get(key);
      if (!row) {
        row = document.createElement('div');
        row._timeEl = document.createElement('div');
        row._kindEl = document.createElement('div');
        row._payloadEl = document.createElement('div');
        row._timeEl.className = 'bb-audit__time';
        row._kindEl.className = 'bb-audit__kind';
        row._payloadEl.className = 'bb-audit__payload';
        row.appendChild(row._timeEl);
        row.appendChild(row._kindEl);
        row.appendChild(row._payloadEl);
        rowNodes.set(key, row);
      }
      if (row.dataset.signature !== signature) {
        row.className = `bb-audit__row ${colorForKind(entry.kind)}`;
        row._timeEl.textContent = fmtTime(entry.ts);
        row._kindEl.textContent = entry.kind || '(unknown)';
        row._payloadEl.textContent = fmtPayload(entry.payload).slice(0, 600);
        row.dataset.signature = signature;
      }
      nextNodes.push(row);
      seenKeys.add(key);
    }
    for (const [key, row] of rowNodes.entries()) {
      if (seenKeys.has(key)) continue;
      if (row.parentNode === listHost) listHost.removeChild(row);
      rowNodes.delete(key);
    }
    syncChildren(listHost, nextNodes);
  }

  function scheduleLoad(delay = 120) {
    if (closed) return;
    if (loadTimer) clearTimeout(loadTimer);
    loadTimer = setTimeout(() => {
      loadTimer = null;
      void load({ silent: true });
    }, Math.max(0, delay));
  }

  async function load(options = {}) {
    const silent = Boolean(options && options.silent);
    if (closed) return;
    if (loading) {
      queued = true;
      return;
    }
    loading = true;
    const shouldShowLoading = !silent || !renderedEntriesSignature;
    if (shouldShowLoading) listHost.classList.add('bb-audit__list--loading');
    try {
      const data = await api.audit(projectId, 200);
      const nextEntries = Array.isArray(data) ? data : [];
      const nextSignature = nextEntries.map((entry) => entryRenderSignature(entry)).join('\u001e');
      allEntries = nextEntries;
      if (nextSignature !== renderedEntriesSignature) {
        renderedEntriesSignature = nextSignature;
        renderedFilterSignature = '';
        renderChips();
        applyFilters();
      } else if (!statusEl.textContent) {
        applyFilters();
      }
    } catch (err) {
      listHost.innerHTML = `<div style="color:var(--c-red);padding:14px">Failed: ${escapeHtml(err.message)}</div>`;
    } finally {
      loading = false;
      if (shouldShowLoading) listHost.classList.remove('bb-audit__list--loading');
      if (queued && !closed) {
        queued = false;
        scheduleLoad(80);
      }
    }
  }

  function isRelevantLiveTopic(topic) {
    const value = String(topic || '').toLowerCase();
    if (!value) return false;
    if (value === 'chat.token' || value === 'chat.thinking' || value === 'chat.progress') return false;
    if (value === 'providers:health') return false;
    return true;
  }

  function startAuto() {
    stopAuto();
    autoTimer = setInterval(() => { void load(); }, 3000);
    unsubscribers.push(bus.on('ws:any', (message) => {
      const topic = message?.topic || '';
      if (!isRelevantLiveTopic(topic)) return;
      scheduleLoad(100);
    }));
    unsubscribers.push(bus.on('ws:connected', () => scheduleLoad(0)));
    unsubscribers.push(bus.on('store:active', (nextProjectId) => {
      if (nextProjectId === projectId) scheduleLoad(0);
    }));
  }

  function stopAuto() {
    if (autoTimer) {
      clearInterval(autoTimer);
      autoTimer = null;
    }
    if (loadTimer) {
      clearTimeout(loadTimer);
      loadTimer = null;
    }
    while (unsubscribers.length) {
      try { unsubscribers.pop()?.(); } catch {}
    }
  }

  searchInput.addEventListener('input', applyFilters);
  dlg.body.querySelector('#audit-refresh').addEventListener('click', () => { void load({ silent: false }); });
  dlg.body.querySelector('#audit-export').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(JSON.stringify(allEntries, null, 2)); toast('Copied to clipboard', { kind: 'success' }); }
    catch (err) { toast(`Copy failed: ${err.message}`, { kind: 'error' }); }
  });
  dlg.onClose(() => { closed = true; stopAuto(); });

  startAuto();
  await load({ silent: false });
}
