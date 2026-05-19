import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';

const stream = [];
const CONSOLE_SCROLLBACK_LIMIT = 2000;
const recentLines = new Map();
let lastProviderSummary = '';
let lastProviderHealth = '';
let liveStateTimer = null;
const EXEC_COLLAPSED_KEY = 'bb.exec.collapsed.v1';

function escapeHtml(t) {
  return String(t).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
}

function truncateText(value, max = 260) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}… (+${text.length - max} chars)` : text;
}

function compactLogValue(value, key = '') {
  if (typeof value === 'string') return truncateText(value, key === 'objective' || key === 'summary' ? 320 : 220);
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) {
    const items = value.slice(0, 8).map((item) => compactLogValue(item));
    if (value.length > 8) items.push(`… ${value.length - 8} more`);
    return items;
  }
  const out = {};
  for (const [k, v] of Object.entries(value)) out[k] = compactLogValue(v, k);
  return out;
}

function logJson(label, data, level = 'log') {
  const compact = compactLogValue(data);
  try {
    console.groupCollapsed(label);
    console[level](JSON.stringify(compact, null, 2));
    console.groupEnd();
  } catch {
    console[level](label, data);
  }
}

function readExecCollapsed() {
  try {
    return localStorage.getItem(EXEC_COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function applyExecCollapsed(collapsed) {
  const app = document.getElementById('app');
  const toggles = Array.from(document.querySelectorAll('[data-exec-toggle="1"]'));
  const restore = document.getElementById('exec-restore');
  if (app) {
    if (collapsed) app.dataset.execCollapsed = '1';
    else delete app.dataset.execCollapsed;
  }
  for (const toggle of toggles) {
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggle.setAttribute('title', collapsed ? 'Show execution panel' : 'Hide execution panel');
  }
  if (restore) restore.hidden = !collapsed;
}

function setExecCollapsed(collapsed) {
  applyExecCollapsed(Boolean(collapsed));
  try {
    localStorage.setItem(EXEC_COLLAPSED_KEY, collapsed ? '1' : '0');
  } catch {
  }
}

function initExecToggle() {
  const toggles = Array.from(document.querySelectorAll('[data-exec-toggle="1"]'));
  if (!toggles.length || toggles.every((toggle) => toggle.dataset.bound === '1')) return;
  applyExecCollapsed(readExecCollapsed());
  for (const toggle of toggles) {
    if (toggle.dataset.bound === '1') continue;
    toggle.dataset.bound = '1';
    toggle.addEventListener('click', () => {
      const collapsed = document.getElementById('app')?.dataset?.execCollapsed === '1';
      setExecCollapsed(!collapsed);
    });
  }
}

function paintJobs() {
  const root = document.getElementById('exec-jobs');
  const summary = document.getElementById('exec-summary');
  const count = document.getElementById('exec-jobs-count');
  const jobs = (store.get().jobs || []).filter((j) => ['pending', 'running', 'merging'].includes(j.status));
  summary.textContent = jobs.length ? `${jobs.length} active job${jobs.length === 1 ? '' : 's'}` : 'no active jobs';
  summary.classList.toggle('bb-exec__summary--active', jobs.length > 0);
  if (count) count.textContent = String(jobs.length);
  root.innerHTML = '';
  if (!jobs.length) {
    root.innerHTML = `
      <div class="bb-exec__empty">
        <span class="bb-exec__empty-icon">◌</span>
        <div>
          <strong>Idle</strong>
          <span>No coding jobs are running.</span>
        </div>
      </div>
    `;
    return;
  }
  for (const j of jobs) {
    const el = document.createElement('div');
    el.className = `bb-exec__job bb-exec__job--${escapeHtml(j.status || 'pending')}`;
    const task = j.task || {};
    const fileCount = Array.isArray(task.files) ? task.files.length : 0;
    const verifyCount = Array.isArray(task.verification) ? task.verification.length : 0;
    const cwd = task.cwd || j.cwd || '';
    el.innerHTML = `
      <div class="bb-exec__job__top">
        <span class="bb-exec__job__status">${escapeHtml(j.status || 'pending')}</span>
        <code>${escapeHtml(j.job_id || '')}</code>
      </div>
      <div class="bb-exec__job__title">${escapeHtml(task.objective || j.job_id)}</div>
      <div class="bb-exec__job__meta">
        <span>retries ${j.retries}/${j.max_retries}</span>
        <span>${escapeHtml(j.worktree_branch || '(no branch)')}</span>
      </div>
      <div class="bb-exec__job__meta bb-exec__job__meta--detail">
        ${task.card_id ? `<span>card ${escapeHtml(task.card_id)}</span>` : ''}
        <span>${fileCount} file${fileCount === 1 ? '' : 's'}</span>
        <span>${verifyCount} check${verifyCount === 1 ? '' : 's'}</span>
      </div>
      ${cwd ? `<div class="bb-exec__job__path" title="${escapeHtml(cwd)}">${escapeHtml(cwd)}</div>` : ''}
    `;
    root.appendChild(el);
  }
}

function setLiveState(label = 'idle', kind = 'idle', holdMs = 2200) {
  const el = document.getElementById('exec-live-state');
  if (!el) return;
  el.textContent = label;
  el.dataset.state = kind;
  if (liveStateTimer) clearTimeout(liveStateTimer);
  if (kind !== 'idle' && holdMs > 0) {
    liveStateTimer = setTimeout(() => setLiveState('idle', 'idle', 0), holdMs);
  }
}

function pushStream(line, kind = 'event', label = '') {
  const key = `${kind}:${label}:${String(line || '')}`;
  const now = performance.now();
  const last = recentLines.get(key) || 0;
  if (now - last < 350) return;
  recentLines.set(key, now);
  if (recentLines.size > 300) {
    for (const [k, t] of recentLines) {
      if (now - t > 5000) recentLines.delete(k);
    }
  }
  const stamp = new Date().toLocaleTimeString();
  stream.push({ stamp, kind, label, line: String(line || '') });
  if (stream.length > CONSOLE_SCROLLBACK_LIMIT) stream.splice(0, stream.length - CONSOLE_SCROLLBACK_LIMIT);
  const el = document.getElementById('exec-stream');
  if (!el) return;
  el.innerHTML = stream.map((item) => `
    <div class="bb-exec__stream-line bb-exec__stream-line--${escapeHtml(item.kind)}">
      <span class="bb-exec__stream-time">${escapeHtml(item.stamp)}</span>
      <span class="bb-exec__stream-kind">${escapeHtml(item.label || item.kind)}</span>
      <span class="bb-exec__stream-text">${escapeHtml(item.line)}</span>
    </div>
  `).join('');
  el.scrollTop = el.scrollHeight;
  if (kind === 'thinking') setLiveState('thinking', 'thinking', 0);
  else if (kind === 'answer') setLiveState('bleeping', 'bleeping', 0);
  else if (kind === 'job') setLiveState('job active', 'job', 2600);
  else if (kind === 'router') setLiveState('routing', 'router', 2200);
  else if (kind === 'error') setLiveState('attention', 'error', 4500);
}

function providerSummary(payload) {
  const profiles = Array.isArray(payload?.profiles) ? payload.profiles : [];
  if (!profiles.length) return '';
  const usable = profiles.filter((p) => p.available && (!p.secret_status?.required || p.secret_status?.has_value) && p.ok === true);
  const names = usable.slice(0, 4).map((p) => `${p.id}:${p.model || 'model?'}`).join(' → ');
  return `${usable.length}/${profiles.length} usable${names ? ` · ${names}` : ''}`;
}

export function renderExecution() {
  initExecToggle();
  paintJobs();
  setLiveState('idle', 'idle', 0);
  if (!stream.length) pushStream('system console armed · watching chat, bleep output, jobs, board changes, router, and provider health', 'system', 'system');
  bus.on('store:jobs', paintJobs);
  bus.on('store:board',             (p) => pushStream(`board refreshed · columns=${Object.keys(p?.cards_by_column || {}).length}`, 'board', 'board'));
  bus.on('store:active',            (p) => pushStream(`active project → ${p || 'none'}`, 'system', 'project'));
  bus.on('store:providers',         (p) => {
    const summary = providerSummary(p);
    if (summary && summary !== lastProviderSummary) {
      lastProviderSummary = summary;
      pushStream(`router refreshed · ${summary}`, 'router', 'router');
    }
  });
  bus.on('store:ws',                (p) => pushStream(`websocket ${p?.connected ? 'connected' : 'disconnected'}`, p?.connected ? 'system' : 'error', 'ws'));
  bus.on('ws:connected',            () => pushStream('websocket connected · live events online', 'system', 'ws'));
  bus.on('ws:disconnected',         () => pushStream('websocket disconnected · reconnecting', 'error', 'ws'));
  bus.on('ws:coding:job.created',   (p) => {
    logJson('[job] created', p);
    pushStream(`created ${p.job_id}: ${p.objective || ''}`, 'job', 'job');
  });
  bus.on('ws:coding:job.started',   (p) => {
    logJson('[job] started', p);
    pushStream(`started ${p.job_id} cwd=${p.cwd || ''}`, 'job', 'job');
  });
  bus.on('ws:coding:job.reviewing', (p) => {
    logJson('[job] reviewing', p);
    pushStream(`reviewing ${p.job_id} files=${(p.files_changed || []).length}`, 'job', 'review');
  });
  bus.on('ws:coding:job.completed', (p) => {
    logJson('[job] completed', p, p.success ? 'log' : 'warn');
    pushStream(`completed ${p.job_id} success=${p.success} patches=${p.patch_count || 0} new=${p.new_file_count || 0} error=${p.error || ''}`, p.success ? 'job' : 'error', p.success ? 'job' : 'error');
  });
  bus.on('ws:coding:job.failed',    (p) => {
    logJson('[job] failed', p, 'error');
    pushStream(`failed ${p.job_id}: ${p.error || ''}`, 'error', 'error');
  });
  bus.on('ws:coding:job.paused',    (p) => pushStream(`paused ${p.job_id} · waiting on ${p.related_job_id || p.related_card_id || 'related work'} · conflicts=${(p.conflicts || []).join(', ')}`, 'error', 'pause'));
  bus.on('ws:coding:job.merged',    (p) => pushStream(`merged ${p.job_id} branch=${p.branch || ''}`, 'job', 'merge'));
  bus.on('ws:board:card.job_synced', (p) => {
    logJson('[card] job synced', p, p.card_status === 'blocked' ? 'warn' : 'log');
    pushStream(`card ${p.card_id} → ${p.card_status} reason=${p.reason || ''}`, p.card_status === 'blocked' ? 'error' : 'board', 'card');
  });
  bus.on('ws:board:card.created',    (p) => pushStream(`card created ${p?.card?.id || ''}: ${p?.card?.title || ''}`, 'board', 'card'));
  bus.on('ws:board:card.updated',    (p) => pushStream(`card updated ${p?.card?.id || ''} → ${p?.card?.status || ''}: ${p?.card?.title || ''}`, 'board', 'card'));
  bus.on('ws:board:card.deleted',    (p) => pushStream(`card deleted ${p?.card_id || ''}`, 'board', 'card'));
  bus.on('ws:card:receipt.written',  (p) => pushStream(`receipt written for card ${p?.card_id || ''}`, 'board', 'receipt'));
  bus.on('ws:coding:cli.transcript', (p) => pushStream(`[${p.kind || 'cli'}] ${(p.text || '').slice(0, 520)}`, 'cli', 'cli'));
  bus.on('ws:chat.started',          (p) => {
    setLiveState('thinking', 'thinking', 0);
    pushStream(`planner stream started · session=${p.session_id || 'session'}`, 'chat', 'chat');
  });
  bus.on('ws:chat.progress',         (p) => pushStream(`${p.heartbeat ? 'still bleeping' : 'progress'} · ${p.detail || 'working'}`, p.heartbeat ? 'thinking' : 'chat', p.heartbeat ? 'bleep' : 'chat'));
  bus.on('ws:chat.token',            () => setLiveState('bleeping', 'bleeping', 0));
  bus.on('ws:chat.thinking',         () => setLiveState('thinking', 'thinking', 0));
  bus.on('ws:chat.done',             (p) => {
    pushStream(`planner stream done · reply=${String(p?.reply || '').length} chars cards=${(p?.cards || []).length}`, 'chat', 'chat');
    setLiveState('idle', 'idle', 0);
  });
  bus.on('ws:chat.error',            (p) => {
    pushStream(`planner error · ${p.error || 'unknown'}`, 'error', 'error');
    setLiveState('attention', 'error', 4500);
  });
  bus.on('ws:providers:snapshot',    (p) => {
    const summary = providerSummary(p);
    if (summary && summary !== lastProviderSummary) {
      lastProviderSummary = summary;
      pushStream(`router refreshed · ${summary}`, 'router', 'router');
    }
  });
  bus.on('ws:providers:health',      (p) => {
    const total = Object.keys(p || {}).length;
    const ok = Object.values(p || {}).filter((h) => h?.ok).length;
    const summary = total ? `${ok}/${total}` : '';
    if (summary && summary !== lastProviderHealth) {
      lastProviderHealth = summary;
      pushStream(`health changed · ${ok}/${total} online`, ok === total ? 'router' : 'error', 'health');
    }
  });
}
