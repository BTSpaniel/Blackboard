// History panel — git-backed timeline of every state change in data/, with safe revert.
import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { createDialog, toast, promptDialog, confirmDialog } from '/ui/shell/dialog.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function fmtTime(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString(); } catch { return ''; }
}

const KIND_COLORS = {
  'vcs.init': '#6b7280',
  'vcs.checkpoint': '#3b82f6',
  'vcs.role_override': '#8b5cf6',
  'vcs.role_override_delete': '#8b5cf6',
  'vcs.key_override': '#f59e0b',
  'vcs.key_override_delete': '#f59e0b',
  'vcs.project_create': '#22c55e',
  'vcs.project_switch': '#14b8a6',
  'vcs.rollback': '#ef4444',
  'board.card.created': '#22c55e',
  'board.card.updated': '#4f7cff',
  'board.card.moved': '#14b8a6',
  'board.card.deleted': '#ef4444',
  'coding.sync_checkpoint': '#fb923c',
};

function kindChip(kind) {
  if (!kind) return '<span class="bb-chip" style="opacity:0.6">—</span>';
  const color = KIND_COLORS[kind] || 'var(--accent, #4f7cff)';
  return `<span class="bb-chip" style="border-color:${color};color:${color}">${escapeHtml(kind)}</span>`;
}

function entryKey(entry) {
  return entry.entry_type === 'sync_checkpoint' ? `checkpoint:${entry.id}` : `commit:${entry.sha}`;
}

function entryFiles(entry) {
  if (Array.isArray(entry.files)) return entry.files;
  if (Array.isArray(entry.files_touched)) return entry.files_touched;
  return [];
}

function entryTimestamp(entry) {
  return Number(entry.timestamp || entry.created_at || 0) || 0;
}

function entrySubject(entry) {
  return String(entry.subject || entry.objective || 'Untitled change');
}

function entrySearchBlob(entry) {
  return [
    entrySubject(entry),
    entry.kind || '',
    entry.short_sha || '',
    entry.sha || '',
    entry.author || '',
    entry.card_id || '',
    entry.project_id || '',
    entry.cwd || '',
    ...entryFiles(entry),
  ].join(' ').toLowerCase();
}

function mergedEntries(commits, checkpoints) {
  const commitEntries = (commits || []).map((commit) => ({ ...commit, entry_type: 'commit' }));
  const checkpointEntries = (checkpoints || []).map((checkpoint) => ({ ...checkpoint, entry_type: 'sync_checkpoint' }));
  return [...checkpointEntries, ...commitEntries].sort((left, right) => entryTimestamp(right) - entryTimestamp(left));
}

function entryFileSummary(entry) {
  const files = entryFiles(entry);
  if (!files.length) return '';
  const visible = files.slice(0, 3).map((file) => `<code>${escapeHtml(file)}</code>`).join(' · ');
  return `${visible}${files.length > 3 ? ` <span class="bb-history__more">+${files.length - 3}</span>` : ''}`;
}

function checkpointStatusChip(status) {
  const value = String(status || 'active');
  const cls = value === 'restored' ? 'bb-history__status--done' : value === 'partial' ? 'bb-history__status--partial' : 'bb-history__status--active';
  return `<span class="bb-history__status ${cls}">${escapeHtml(value)}</span>`;
}

export async function openHistoryPanel() {
  const projectId = store.get().activeProjectId || '';
  const dlg = createDialog({ title: 'Version history', size: 'xl' });
  dlg.body.innerHTML = `
    <div class="bb-history">
      <div class="bb-history__toolbar">
        <select id="hist-scope" class="bb-input" style="max-width:220px">
          <option value="project">Project code</option>
          <option value="data">Blackboard state</option>
        </select>
        <input type="search" id="hist-search" class="bb-input" placeholder="Search subject / kind / sha / file…" />
        <select id="hist-filter" class="bb-input" style="max-width:220px">
          <option value="">All kinds</option>
        </select>
        <span style="flex:1"></span>
        <span class="bb-live" id="hist-live">live</span>
      </div>
      <div class="bb-history__split">
        <div class="bb-history__list" id="hist-list">Loading…</div>
        <div class="bb-history__detail" id="hist-detail">
          <div class="bb-history__empty">Select a history item to inspect its diff or rollback options.</div>
        </div>
      </div>
    </div>
  `;
  dlg.setFooter([
    { html: '<span id="hist-status" style="color:var(--fg-3);font-size:11px">—</span>' },
    { spacer: true },
    { label: '＋ Checkpoint', onClick: createCheckpoint },
    { label: 'Close', primary: true, onClick: () => dlg.close() },
  ]);

  let allEntries = [];
  let activeKindFilter = '';
  let activeEntry = '';
  let activeScope = 'project';
  let activeStatus = null;

  function scopeOptions() {
    return { scope: activeScope, projectId };
  }

  function setStatus(text) {
    const node = document.getElementById('hist-status');
    if (node) node.textContent = text;
  }

  function currentEntry() {
    return allEntries.find((entry) => entryKey(entry) === activeEntry) || null;
  }

  function applyFilter() {
    const query = (document.getElementById('hist-search')?.value || '').toLowerCase();
    return allEntries.filter((entry) => {
      if (activeKindFilter && entry.kind !== activeKindFilter) return false;
      if (!query) return true;
      return entrySearchBlob(entry).includes(query);
    });
  }

  function renderList() {
    const filtered = applyFilter();
    const list = document.getElementById('hist-list');
    setStatus(`${filtered.length} of ${allEntries.length} history items`);
    if (!filtered.length) {
      list.innerHTML = '<div class="bb-history__empty">No history items match the filter.</div>';
      return;
    }
    list.innerHTML = filtered.map((entry) => {
      const key = entryKey(entry);
      const active = key === activeEntry ? ' bb-history__row--active' : '';
      const files = entryFiles(entry);
      const secondary = entry.entry_type === 'sync_checkpoint'
        ? `${files.length} file${files.length === 1 ? '' : 's'}${entry.card_id ? ` · card ${escapeHtml(entry.card_id)}` : ''}${entry.cwd ? ` · ${escapeHtml(entry.cwd)}` : ''}`
        : `${files.length} file${files.length === 1 ? '' : 's'}${entry.author ? ` · ${escapeHtml(entry.author)}` : ''}`;
      return `
        <div class="bb-history__row${active}" data-key="${escapeHtml(key)}">
          <div class="bb-history__row-head">
            <code class="bb-history__sha">${escapeHtml(entry.short_sha || entry.id || '').slice(0, 8)}</code>
            ${kindChip(entry.kind)}
            ${entry.entry_type === 'sync_checkpoint' ? checkpointStatusChip(entry.status) : '<span class="bb-history__type">commit</span>'}
            <span class="bb-history__time">${fmtTime(entryTimestamp(entry))}</span>
          </div>
          <div class="bb-history__subject">${escapeHtml(entrySubject(entry))}</div>
          <div class="bb-history__meta">${secondary}</div>
          ${files.length ? `<div class="bb-history__files">${entryFileSummary(entry)}</div>` : ''}
        </div>
      `;
    }).join('');
    list.querySelectorAll('.bb-history__row').forEach((row) => {
      row.addEventListener('click', () => {
        activeEntry = row.dataset.key || '';
        renderList();
        void showDetail(activeEntry);
      });
    });
  }

  async function showCommitDetail(entry) {
    const detail = document.getElementById('hist-detail');
    detail.innerHTML = `<div class="bb-history__loading">Loading <code>${escapeHtml(String(entry.sha || '').slice(0, 8))}</code>…</div>`;
    let data;
    try {
      data = await api.vcsDiff(entry.sha, scopeOptions());
    } catch (err) {
      detail.innerHTML = `<div class="bb-history__error">${escapeHtml(err.message)}</div>`;
      return;
    }
    detail.innerHTML = `
      <div class="bb-history__detail-head">
        <div>
          <strong>${escapeHtml(entrySubject(entry))}</strong>
          <div class="bb-history__detail-subhead">
            <code>${escapeHtml(entry.sha || '')}</code>
            ${entry.kind ? ` · ${kindChip(entry.kind)}` : ''}
            ${entry.author ? ` · ${escapeHtml(entry.author)}` : ''}
            · ${fmtTime(entryTimestamp(entry))}
          </div>
        </div>
        <div class="bb-history__detail-actions">
          <button class="bb-btn bb-btn--xs" id="hist-revert">↶ Revert</button>
          <button class="bb-btn bb-btn--xs bb-btn--danger" id="hist-hard" title="Hard reset — destructive, drops all commits after this">⚠ Hard reset</button>
          <button class="bb-btn bb-btn--xs" id="hist-tag">🏷 Tag</button>
        </div>
      </div>
      <div class="bb-history__section">
        <div class="bb-history__section-title">Files in this commit</div>
        <div class="bb-history__files bb-history__files--detail">${entryFiles(entry).length ? entryFiles(entry).map((file) => `<code>${escapeHtml(file)}</code>`).join(' ') : '<span class="bb-history__muted">No file list available.</span>'}</div>
      </div>
      <pre class="bb-history__diff">${escapeHtml(data.diff || '(no diff)')}</pre>
    `;
    detail.querySelector('#hist-revert')?.addEventListener('click', () => doRollback(entry.sha, 'revert'));
    detail.querySelector('#hist-hard')?.addEventListener('click', () => doRollback(entry.sha, 'hard'));
    detail.querySelector('#hist-tag')?.addEventListener('click', () => doTag(entry.sha));
  }

  async function restoreCheckpoint(entry, files = []) {
    const isSingle = files.length === 1;
    const target = isSingle ? files[0] : `${entryFiles(entry).length} files`;
    const ok = await confirmDialog({
      title: isSingle ? 'Restore file' : 'Restore coding action',
      message: isSingle
        ? `Restore ${target} to its pre-edit state from this coding action?`
        : `Restore every file captured in this coding action back to its pre-edit state?`,
      confirmLabel: isSingle ? 'Restore file' : 'Restore all',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.vcsRestoreCheckpoint(entry.id, { files, reason: isSingle ? `restored ${target}` : 'restored coding action' });
      toast(isSingle ? `Restored ${target}` : 'Restored coding action', { kind: 'success' });
      await refresh();
    } catch (err) {
      toast(`Restore failed: ${err.message}`, { kind: 'error', timeout: 6000 });
    }
  }

  async function showCheckpointDetail(entry) {
    const detail = document.getElementById('hist-detail');
    const files = entryFiles(entry);
    const restoredFiles = new Set(Array.isArray(entry.restored_files) ? entry.restored_files : []);
    detail.innerHTML = `
      <div class="bb-history__detail-head">
        <div>
          <strong>${escapeHtml(entrySubject(entry))}</strong>
          <div class="bb-history__detail-subhead">
            <code>${escapeHtml(entry.id || '')}</code>
            · ${kindChip(entry.kind)}
            · ${checkpointStatusChip(entry.status)}
            · ${fmtTime(entryTimestamp(entry))}
          </div>
        </div>
        <div class="bb-history__detail-actions">
          <button class="bb-btn bb-btn--xs" id="hist-restore-all">↶ Restore all</button>
        </div>
      </div>
      <div class="bb-history__detail-scroll">
        <div class="bb-history__section-grid">
          <div class="bb-history__section-card">
            <div class="bb-history__section-label">Card</div>
            <div class="bb-history__section-value">${entry.card_id ? escapeHtml(entry.card_id) : '<span class="bb-history__muted">none</span>'}</div>
          </div>
          <div class="bb-history__section-card">
            <div class="bb-history__section-label">Project</div>
            <div class="bb-history__section-value">${entry.project_id ? escapeHtml(entry.project_id) : '<span class="bb-history__muted">none</span>'}</div>
          </div>
          <div class="bb-history__section-card bb-history__section-card--wide">
            <div class="bb-history__section-label">Working directory</div>
            <div class="bb-history__section-value"><code>${escapeHtml(entry.cwd || '')}</code></div>
          </div>
        </div>
        <div class="bb-history__section">
          <div class="bb-history__section-title">Rollback files</div>
          <div class="bb-history__checkpoint-list">
            ${files.length ? files.map((file) => {
              const restored = restoredFiles.has(file);
              return `
                <div class="bb-history__checkpoint-file">
                  <div>
                    <div class="bb-history__checkpoint-path"><code>${escapeHtml(file)}</code></div>
                    <div class="bb-history__checkpoint-state">${restored ? 'Restored already' : 'Available to restore from this action'}</div>
                  </div>
                  <button class="bb-btn bb-btn--xs" data-restore-file="${encodeURIComponent(file)}">Restore file</button>
                </div>
              `;
            }).join('') : '<div class="bb-history__muted">No files were captured for this coding action.</div>'}
          </div>
        </div>
      </div>
    `;
    detail.querySelector('#hist-restore-all')?.addEventListener('click', () => restoreCheckpoint(entry, []));
    detail.querySelectorAll('[data-restore-file]').forEach((button) => {
      button.addEventListener('click', () => restoreCheckpoint(entry, [decodeURIComponent(button.dataset.restoreFile || '')]));
    });
  }

  async function showDetail(key) {
    const entry = allEntries.find((item) => entryKey(item) === key);
    if (!entry) return;
    activeEntry = key;
    if (entry.entry_type === 'sync_checkpoint') {
      await showCheckpointDetail(entry);
      return;
    }
    await showCommitDetail(entry);
  }

  async function doRollback(sha, mode) {
    const ok = await confirmDialog({
      title: mode === 'hard' ? 'Hard reset' : 'Revert commit',
      message: mode === 'hard'
        ? `This will DISCARD every commit after ${sha.slice(0, 8)}. Files on disk will be reset to that exact state. Continue?`
        : `Create a new commit that undoes the changes from ${sha.slice(0, 8)}. The history stays intact.`,
      confirmLabel: mode === 'hard' ? 'Hard reset' : 'Revert',
      danger: mode === 'hard',
    });
    if (!ok) return;
    try {
      await api.vcsRollback(sha, mode, scopeOptions());
      toast(mode === 'hard' ? `Reset to ${sha.slice(0, 8)}` : `Reverted ${sha.slice(0, 8)}`, { kind: 'success' });
      await refresh();
    } catch (err) {
      toast(`Rollback failed: ${err.message}`, { kind: 'error', timeout: 6000 });
    }
  }

  async function doTag(sha) {
    const name = await promptDialog({
      title: 'Tag commit',
      label: 'Tag name (e.g. release-2026-05)',
      placeholder: 'milestone-name',
      confirmLabel: 'Create tag',
    });
    if (!name) return;
    try {
      await api.vcsTag(name.trim(), sha, '', scopeOptions());
      toast(`Tagged ${sha.slice(0, 8)} as ${name}`, { kind: 'success' });
    } catch (err) {
      toast(`Tag failed: ${err.message}`, { kind: 'error' });
    }
  }

  async function createCheckpoint() {
    const targetLabel = activeScope === 'project' ? 'the current project repository' : '<code>data/</code>';
    const message = await promptDialog({
      title: 'Manual checkpoint',
      label: 'Describe what state you\'re saving',
      placeholder: 'Before risky refactor',
      confirmLabel: 'Create checkpoint',
      help: `Captures the current state of ${targetLabel} as a labeled commit. Useful before risky changes.`,
    });
    if (!message) return;
    try {
      const result = await api.vcsCheckpoint(message.trim() || 'Manual checkpoint', scopeOptions());
      toast(`Checkpoint created: ${result.short_sha}`, { kind: 'success' });
      await refresh();
    } catch (err) {
      toast(`Checkpoint failed: ${err.message}`, { kind: 'error' });
    }
  }

  async function refresh() {
    try {
      activeStatus = await api.vcsStatus(scopeOptions());
      const historyData = await api.vcsHistory({ limit: 200, ...scopeOptions() });
      const includeCheckpoints = activeScope === 'project';
      const checkpointData = includeCheckpoints ? await api.vcsSyncCheckpoints({ limit: 200, projectId }) : { checkpoints: [] };
      allEntries = mergedEntries(historyData?.commits || [], checkpointData?.checkpoints || []);
      const kinds = Array.from(new Set(allEntries.map((entry) => entry.kind).filter(Boolean))).sort();
      const select = document.getElementById('hist-filter');
      const current = select.value;
      select.innerHTML = `<option value="">All kinds (${allEntries.length})</option>${kinds.map((kind) => `<option value="${escapeHtml(kind)}">${escapeHtml(kind)}</option>`).join('')}`;
      select.value = current;
      const rootLabel = String(activeStatus?.repo_root || '').trim();
      const repoKind = activeScope === 'project' ? 'project repo' : 'Blackboard data repo';
      setStatus(activeStatus?.available ? `${repoKind} · ${rootLabel || 'git repo'} · ${allEntries.length} history items` : `${repoKind} unavailable`);
      renderList();
      if (activeEntry && currentEntry()) {
        await showDetail(activeEntry);
      }
    } catch (err) {
      document.getElementById('hist-list').innerHTML = `<div class="bb-history__error">${escapeHtml(err.message)}</div>`;
    }
  }

  document.getElementById('hist-search').addEventListener('input', renderList);
  document.getElementById('hist-scope').addEventListener('change', async (event) => {
    activeScope = event.target.value || 'project';
    activeEntry = '';
    activeKindFilter = '';
    document.getElementById('hist-filter').value = '';
    await refresh();
  });
  document.getElementById('hist-filter').addEventListener('change', (event) => {
    activeKindFilter = event.target.value;
    renderList();
  });

  const onVersion = () => { void refresh(); };
  bus.on('ws:vcs.commit', onVersion);
  bus.on('ws:vcs.rollback', onVersion);
  bus.on('ws:vcs.checkpoint', onVersion);
  bus.on('ws:sync_checkpoint.recorded', onVersion);
  bus.on('ws:sync_checkpoint.restored', onVersion);
  dlg.onClose(() => {
    bus.off('ws:vcs.commit', onVersion);
    bus.off('ws:vcs.rollback', onVersion);
    bus.off('ws:vcs.checkpoint', onVersion);
    bus.off('ws:sync_checkpoint.recorded', onVersion);
    bus.off('ws:sync_checkpoint.restored', onVersion);
  });

  dlg.open();
  try {
    const projectStatus = await api.vcsStatus({ scope: 'project', projectId });
    if (!projectStatus?.available) {
      activeScope = 'data';
      const scopeEl = document.getElementById('hist-scope');
      if (scopeEl) scopeEl.value = 'data';
    }
  } catch (err) {
    activeScope = 'data';
    const scopeEl = document.getElementById('hist-scope');
    if (scopeEl) scopeEl.value = 'data';
  }
  await refresh();
}
