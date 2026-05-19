import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { openCardModal } from '/ui/shell/card_modal.js';
import { createDialog, toast } from '/ui/shell/dialog.js';
import { resolvePreferredWorkingDir } from '/ui/shell/working_directory.js';

const COLUMN_LABELS = {
  inbox: 'Inbox',
  designing: 'Designing',
  planning: 'Planning',
  ready: 'Ready',
  executing: 'Executing',
  reviewing: 'Reviewing',
  blocked: 'Blocked',
  done: 'Done',
};
const ACTIVE_JOB_STATUSES = new Set(['pending', 'paused', 'running', 'merging']);

let filterQuery = '';
let mobileLaneFilter = '';
let progressTimer = 0;
const expandedCardIds = new Set();
const liveFileStreams = new Map();
const FILE_STREAM_VISUAL_FLOOR = 6;
const FILE_STREAM_SOFT_CAP_PERCENT = 98.4;
const FILE_STREAM_LERP_PER_SECOND = 6.5;
const FILE_PATH_RE = /(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])?(?:[\w.-]+[\\/])*\w[\w.-]*\.[A-Za-z0-9]{1,8}/g;
let boardRenderRaf = 0;

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function hasPendingQuestions(card) {
  const pending = card?.metadata?.pending_questions;
  return Boolean(pending && Array.isArray(pending.questions) && pending.questions.length);
}

function resumeState(card) {
  const resume = card?.metadata?.resume_from;
  return resume?.status ? resume : null;
}

function coordinationState(card) {
  const coordination = card?.metadata?.coordination;
  return coordination?.status ? coordination : null;
}

function mergeRecommendation(card) {
  const recommendation = card?.metadata?.merge_recommendation;
  return recommendation?.best ? recommendation : null;
}

function executionObjective(card) {
  return card?.metadata?.execution_objective || card?.body || card?.title || '';
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function currentProjectId() {
  return store.get().activeProjectId || '';
}

function isMobileBoardViewport() {
  return window.matchMedia('(max-width: 900px)').matches;
}

export function openNewCardDialog(initial = {}) {
  const projectId = currentProjectId();
  if (!projectId) {
    toast('Select a project before creating a card.', { kind: 'warn' });
    return null;
  }
  const dlg = createDialog({ title: 'New card', size: 'sm' });
  dlg.body.innerHTML = `
    <div style="display:grid;gap:12px">
      <label style="display:grid;gap:6px">
        <span style="font-size:12px;color:var(--fg-2)">Title</span>
        <input id="bb-mobile-card-title" class="bb-input" value="${escapeHtml(initial.title || '')}" placeholder="Add a clear card title" />
      </label>
      <label style="display:grid;gap:6px">
        <span style="font-size:12px;color:var(--fg-2)">Body</span>
        <textarea id="bb-mobile-card-body" class="bb-input" rows="6" placeholder="Briefly describe the task or request">${escapeHtml(initial.body || '')}</textarea>
      </label>
      <label style="display:grid;gap:6px">
        <span style="font-size:12px;color:var(--fg-2)">Status</span>
        <select id="bb-mobile-card-status" class="bb-input">
          <option value="inbox">Inbox</option>
          <option value="designing">Designing</option>
          <option value="planning">Planning</option>
          <option value="ready">Ready</option>
        </select>
      </label>
    </div>
  `;
  dlg.setFooter([
    { label: 'Cancel', onClick: () => dlg.close() },
    { spacer: true },
    {
      label: 'Create',
      primary: true,
      onClick: async () => {
        const titleEl = dlg.body.querySelector('#bb-mobile-card-title');
        const bodyEl = dlg.body.querySelector('#bb-mobile-card-body');
        const statusEl = dlg.body.querySelector('#bb-mobile-card-status');
        const title = String(titleEl?.value || '').trim();
        const body = String(bodyEl?.value || '').trim();
        const status = String(statusEl?.value || 'inbox').trim() || 'inbox';
        if (!title) {
          toast('Card title is required.', { kind: 'warn' });
          titleEl?.focus();
          return;
        }
        try {
          await api.createCard(projectId, { title, body, status });
          const board = await api.board(projectId);
          store.setBoard(board);
          dlg.close();
          toast('Card created', { kind: 'success' });
        } catch (err) {
          toast(`Card create failed: ${err.message}`, { kind: 'error', timeout: 5000 });
        }
      },
    },
  ]);
  dlg.open();
  const statusEl = dlg.body.querySelector('#bb-mobile-card-status');
  if (statusEl) statusEl.value = String(initial.status || 'inbox');
  dlg.body.querySelector('#bb-mobile-card-title')?.focus();
  return dlg;
}

function basename(path) {
  const normalized = String(path || '').replace(/\\/g, '/');
  return normalized.split('/').pop() || normalized;
}

function normalizePathText(value) {
  return String(value || '').trim().replace(/\\/g, '/').toLowerCase();
}

function scheduleBoardRender() {
  if (boardRenderRaf) return;
  boardRenderRaf = window.requestAnimationFrame(() => {
    boardRenderRaf = 0;
    refreshVisibleCardProgress();
  });
}

function activeTranscriptJobForProject(projectId = currentProjectId()) {
  if (!projectId) return null;
  const jobs = store.get().jobs || [];
  const rank = { running: 4, merging: 3, pending: 2, paused: 1 };
  const matches = jobs.filter((job) => {
    const status = String(job?.status || '');
    if (!ACTIVE_JOB_STATUSES.has(status) || status === 'paused') return false;
    const task = job?.task || {};
    return String(task.project_id || '') === projectId && String(task.card_id || '');
  });
  matches.sort((left, right) => {
    const statusDelta = (rank[String(right?.status || '')] || 0) - (rank[String(left?.status || '')] || 0);
    if (statusDelta) return statusDelta;
    const startedDelta = (Number(right?.started_at || 0) || Number(right?.created_at || 0) || 0)
      - (Number(left?.started_at || 0) || Number(left?.created_at || 0) || 0);
    return startedDelta;
  });
  return matches[0] || null;
}

function currentFileFallback(card, touchedFiles = 0) {
  const files = Array.isArray(card?.files) ? card.files.filter(Boolean) : [];
  if (!files.length) return '';
  const index = clamp(Math.max(0, Number(touchedFiles || 0) || 0), 0, files.length - 1);
  return String(files[index] || '');
}

function inferCurrentFileFromTranscript(card, transcriptText, fallback = '') {
  const files = Array.isArray(card?.files) ? card.files.filter(Boolean) : [];
  const rawText = String(transcriptText || '');
  const normalizedText = normalizePathText(rawText);
  for (const file of files.slice().sort((left, right) => String(right).length - String(left).length)) {
    const normalized = normalizePathText(file);
    const base = basename(file).toLowerCase();
    if ((normalized && normalizedText.includes(normalized)) || (base && normalizedText.includes(base))) {
      return String(file);
    }
  }
  const genericMatch = rawText.match(FILE_PATH_RE);
  if (genericMatch?.length) {
    const candidate = String(genericMatch[genericMatch.length - 1] || '').trim();
    if (candidate) return candidate.replace(/\\/g, '/');
  }
  return String(fallback || '');
}

function syncLiveFileStream(cardId, patch = {}) {
  if (!cardId) return null;
  const previous = liveFileStreams.get(cardId) || {};
  const next = { ...previous, ...patch };
  liveFileStreams.set(cardId, next);
  return next;
}

function setLiveFileStreamStateFromJob(payload, active) {
  const cardId = String(payload?.card_id || '');
  if (!cardId) return;
  const nowMs = Date.now();
  const card = findCard(cardId) || { files: payload?.files || [] };
  const previous = liveFileStreams.get(cardId) || {};
  const nextFile = previous.currentFile || currentFileFallback(card, 0);
  syncLiveFileStream(cardId, {
    jobId: String(payload?.job_id || previous.jobId || ''),
    projectId: String(payload?.project_id || previous.projectId || currentProjectId() || ''),
    startedAt: previous.startedAt || nowMs,
    currentFile: nextFile,
    progressNote: active ? String(previous.progressNote || '') : '',
    active: Boolean(active),
    lastEstimateAt: nowMs,
    visualPercent: active ? Number(previous.visualPercent || FILE_STREAM_VISUAL_FLOOR) : Number(previous.visualPercent || 100),
  });
}

function applyLiveJobProgress(payload) {
  const cardId = String(payload?.card_id || '');
  if (!cardId) return;
  const nowMs = Date.now();
  const card = findCard(cardId) || { files: payload?.files || [] };
  const previous = liveFileStreams.get(cardId) || {};
  const inferredFile = inferCurrentFileFromTranscript(
    card,
    String(payload?.note || ''),
    String(payload?.current_file || previous.currentFile || currentFileFallback(card, 0) || ''),
  );
  syncLiveFileStream(cardId, {
    jobId: String(payload?.job_id || previous.jobId || ''),
    projectId: String(payload?.project_id || previous.projectId || currentProjectId() || ''),
    startedAt: previous.startedAt || nowMs,
    active: true,
    currentFile: inferredFile,
    progressNote: String(payload?.note || previous.progressNote || ''),
    lastTextAt: nowMs,
    lastEstimateAt: nowMs,
  });
  scheduleBoardRender();
}

function applyLiveTranscript(payload) {
  const job = activeTranscriptJobForProject();
  if (!job) return;
  const task = job?.task || {};
  const cardId = String(task.card_id || job?.card_id || '');
  if (!cardId) return;
  const text = String(payload?.text || '');
  if (!text) return;
  const nowMs = Date.now();
  const card = findCard(cardId) || { files: task.files || [] };
  const previous = liveFileStreams.get(cardId) || {};
  const transcriptTail = `${String(previous.transcriptTail || '')}\n${text}`.slice(-4000);
  const currentFile = inferCurrentFileFromTranscript(card, transcriptTail, previous.currentFile || currentFileFallback(card, 0));
  syncLiveFileStream(cardId, {
    jobId: String(job?.job_id || previous.jobId || ''),
    projectId: String(task.project_id || previous.projectId || currentProjectId() || ''),
    startedAt: previous.startedAt || nowMs,
    active: true,
    chunkCount: Math.max(0, Number(previous.chunkCount || 0) || 0) + 1,
    charCount: Math.max(0, Number(previous.charCount || 0) || 0) + text.length,
    transcriptTail,
    currentFile,
    lastTextAt: nowMs,
  });
  scheduleBoardRender();
}

function liveJobForCard(card) {
  const projectId = currentProjectId();
  if (!projectId || !card?.id) return null;
  return activeJobForCard(projectId, card.id) || null;
}

function liveOverallPercent(card, basePercent) {
  const liveJob = liveJobForCard(card);
  const cardStatus = compactText(card?.status || '');
  const jobStatus = compactText(liveJob?.status || card?.metadata?.last_job?.status || '');
  let percent = clamp(Number(basePercent || 0) || 0, 0, 100);

  if (cardStatus === 'done') return 100;
  if (cardStatus === 'reviewing') percent = Math.max(percent, 85);
  if (cardStatus === 'blocked') percent = Math.max(percent, 90);
  if (cardStatus === 'executing') percent = Math.max(percent, 10);

  if (!liveJob) {
    if (jobStatus === 'running') return Math.max(percent, 12);
    if (jobStatus === 'merging') return Math.max(percent, 92);
    if (jobStatus === 'paused') return Math.max(percent, 6);
    return percent;
  }

  const startedAt = Number(liveJob.started_at || 0) || Number(liveJob.created_at || 0) || 0;
  const elapsed = startedAt > 0 ? Math.max(0, (Date.now() / 1000) - startedAt) : 0;

  if (jobStatus === 'pending') {
    return Math.max(percent, 6);
  }
  if (jobStatus === 'paused') {
    return Math.max(percent, 6);
  }
  if (jobStatus === 'merging') {
    return Math.max(percent, 92);
  }
  if (jobStatus === 'running') {
    const curve = 1 - Math.exp(-elapsed / 16);
    const inferred = 12 + (curve * 56);
    return clamp(Math.max(percent, inferred), 0, 78);
  }
  return percent;
}

function compactText(value) {
  return String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
}

function smartCurrentLabel(card, context = {}) {
  const status = compactText(card?.metadata?.last_job?.status || card?.status || '');
  const reason = compactText(card?.metadata?.last_job?.transition_reason || '');
  const error = compactText(card?.metadata?.last_job?.error || '');
  const liveState = liveFileStreams.get(card?.id) || {};
  const liveNote = String(liveState.progressNote || '').trim();
  const touchedFiles = Math.max(0, Number(context.touchedFiles || 0) || 0);
  const expectedFiles = Math.max(0, Number(context.expectedFiles || 0) || 0);
  const expectedChecks = Math.max(0, Number(context.expectedChecks || 0) || 0);
  const currentPercent = clamp(Number(context.currentPercent || 0) || 0, 0, 100);

  const fileCountLabel = expectedFiles > 0
    ? `${Math.min(touchedFiles, expectedFiles)}/${expectedFiles} files`
    : touchedFiles > 0
      ? `${touchedFiles} touches`
      : '';

  if (status === 'reviewing' || reason.includes('review') || reason.includes('verify')) {
    return expectedChecks > 0 && currentPercent > 0 ? `verifying · ${Math.round(currentPercent)}%` : 'verifying';
  }
  if (status === 'paused') {
    if (reason.includes('conflict') || reason.includes('coordination')) return 'paused on conflict';
    return 'paused';
  }
  if (status === 'blocked' || compactText(card?.status || '') === 'blocked') {
    if (error.includes('server restart') || reason.includes('server restart') || reason.includes('restart')) return 'blocked after restart';
    if (reason.includes('review')) return 'blocked in review';
    if (reason.includes('conflict') || reason.includes('coordination')) return 'blocked on conflict';
    if (reason.includes('user input')) return 'waiting for input';
    return 'blocked';
  }
  if (status === 'running' || status === 'merging' || status === 'pending' || compactText(card?.status || '') === 'executing') {
    if (liveNote) return liveNote;
    if (expectedFiles > 0) {
      if (touchedFiles <= 0) return 'creating files';
      if (touchedFiles < expectedFiles) return `creating files · ${fileCountLabel}`;
      return `finishing files · ${fileCountLabel}`;
    }
    if (expectedChecks > 0) return expectedChecks > 0 && currentPercent > 0 ? `verifying · ${Math.round(currentPercent)}%` : 'verifying';
    return status === 'merging' ? 'merging changes' : 'working';
  }
  if (status === 'done' || compactText(card?.status || '') === 'done') {
    return 'complete';
  }
  if (fileCountLabel) return fileCountLabel;
  return currentPercent > 0 ? 'active' : 'idle';
}

function jobStateModel(card) {
  const liveJob = liveJobForCard(card);
  const liveStatus = compactText(liveJob?.status || '');
  const lastStatus = compactText(card?.metadata?.last_job?.status || '');
  const coordination = coordinationState(card);
  const status = liveStatus || lastStatus;
  if (!status) return { visible: false, tone: '', label: '' };
  if (status === 'paused') {
    if (coordination?.reason) return { visible: true, tone: 'paused', label: `Paused · ${coordination.reason}` };
    return { visible: true, tone: 'paused', label: 'Paused' };
  }
  if (status === 'running') return { visible: true, tone: 'running', label: 'Running' };
  if (status === 'pending') return { visible: true, tone: 'pending', label: 'Queued' };
  if (status === 'reviewing') return { visible: true, tone: 'reviewing', label: 'Reviewing' };
  if (status === 'merging') return { visible: true, tone: 'merging', label: 'Merging' };
  if (status === 'success') return { visible: true, tone: 'success', label: 'Completed' };
  if (status === 'failed') return { visible: true, tone: 'failed', label: 'Failed' };
  return { visible: true, tone: status, label: status.charAt(0).toUpperCase() + status.slice(1) };
}

function fileStreamProgressModel(card, context = {}) {
  const liveJob = liveJobForCard(card);
  const liveState = liveFileStreams.get(card?.id) || {};
  const files = Array.isArray(card?.files) ? card.files.filter(Boolean) : [];
  const fallbackFile = liveState.currentFile || currentFileFallback(card, context.touchedFiles);
  const currentFile = inferCurrentFileFromTranscript(card, liveState.transcriptTail || '', fallbackFile);
  const active = Boolean(
    liveState.active
    || compactText(card?.status || '') === 'executing'
    || compactText(card?.metadata?.last_job?.status || '') === 'running'
    || compactText(liveJob?.status || '') === 'running'
    || compactText(liveJob?.status || '') === 'pending'
  );
  const nowMs = Date.now();
  const startedAtMs = Number(liveState.startedAt || 0) || ((Number(liveJob?.started_at || 0) || Number(liveJob?.created_at || 0) || 0) * 1000) || nowMs;
  const elapsed = Math.max(0, (nowMs - startedAtMs) / 1000);
  const chunkCount = Math.max(0, Number(liveState.chunkCount || 0) || 0);
  const charCount = Math.max(0, Number(liveState.charCount || 0) || 0);
  const phase = charCount > 0 || chunkCount > 0 ? 'bleeping' : 'thinking';
  const nameWeight = Math.max(1, basename(currentFile).length || 0);
  const estimatedFirstReply = clamp(2.6 + Math.min(8, files.length * 0.42) + Math.min(4, nameWeight * 0.05), 2.5, 16);
  const estimatedTotal = clamp(estimatedFirstReply + 4.5 + Math.min(10, files.length * 0.9) + Math.min(8, nameWeight * 0.08), estimatedFirstReply + 2, 40);
  let targetPercent = FILE_STREAM_VISUAL_FLOOR;
  if (phase === 'bleeping') {
    const replyBudget = Math.max(estimatedTotal - estimatedFirstReply, 3);
    const streamSignal = charCount + (chunkCount * 42);
    const replyElapsed = Math.max(0, elapsed - estimatedFirstReply) + (streamSignal / Math.max(180, 120 + (nameWeight * 8)));
    const replyCurve = 1 - Math.exp(-replyElapsed / Math.max(replyBudget * 0.82, 2.5));
    targetPercent = 64 + (replyCurve * 30);
    targetPercent += Math.min(3.5, Math.log1p(chunkCount) * 0.95);
  } else {
    const thinkCurve = 1 - Math.exp(-elapsed / Math.max(estimatedFirstReply * 0.92, 2.4));
    targetPercent = 8 + (thinkCurve * 56);
    targetPercent += Math.min(4.5, Math.log1p(Math.max(0, chunkCount)) * 0.75);
    targetPercent = Math.min(targetPercent, 68);
  }
  const cap = active ? FILE_STREAM_SOFT_CAP_PERCENT : 100;
  targetPercent = clamp(targetPercent, FILE_STREAM_VISUAL_FLOOR, cap);
  const lastAt = Number(liveState.lastEstimateAt || 0) || nowMs;
  const deltaSeconds = Math.max(0.016, (nowMs - lastAt) / 1000);
  const factor = 1 - Math.exp(-FILE_STREAM_LERP_PER_SECOND * deltaSeconds);
  const startPercent = clamp(Number(liveState.visualPercent || FILE_STREAM_VISUAL_FLOOR), FILE_STREAM_VISUAL_FLOOR, cap);
  const visualPercent = active
    ? clamp(startPercent + ((targetPercent - startPercent) * factor), FILE_STREAM_VISUAL_FLOOR, cap)
    : clamp(Math.max(startPercent, targetPercent), FILE_STREAM_VISUAL_FLOOR, 100);
  const streamPercent = phase === 'bleeping'
    ? clamp(((visualPercent - 64) / 30) * 100, 0, 100)
    : clamp(((visualPercent - 8) / 60) * 100, 0, 100);
  const fileLabel = basename(currentFile || currentFileFallback(card, context.touchedFiles) || 'stream text');
  const streamLabel = phase === 'bleeping'
    ? `streaming ${fileLabel} · ${Math.round(streamPercent)}%`
    : `starting ${fileLabel}${streamPercent >= 4 ? ` · ${Math.round(streamPercent)}%` : ''}`;
  if (card?.id) {
    syncLiveFileStream(card.id, {
      ...liveState,
      startedAt: startedAtMs,
      currentFile,
      visualPercent,
      lastEstimateAt: nowMs,
      active,
    });
  }
  return {
    shouldRender: active || chunkCount > 0 || charCount > 0 || Boolean(liveState.currentFile),
    streamPercent,
    streamLabel,
  };
}

function cardProgressModel(card) {
  const overallPercent = liveOverallPercent(card, Number(card?.progress || 0) || 0);
  const expectedFiles = Math.max(0, Number(card?.files?.length || 0));
  const expectedChecks = Math.max(0, Number(card?.verification?.length || 0));
  const status = compactText(card?.metadata?.last_job?.status || card?.status || '');
  const lastJob = card?.metadata?.last_job || {};
  const newFileCount = Math.max(0, Number(lastJob.new_file_count || 0) || 0);
  const patchCount = Math.max(0, Number(lastJob.patch_count || 0) || 0);
  const touchedFiles = Math.max(newFileCount, patchCount);
  let currentPercent = 0;

  if (expectedFiles > 0) {
    if (touchedFiles > 0) {
      currentPercent = clamp((Math.min(touchedFiles, expectedFiles) / expectedFiles) * 100, 0, 100);
    } else if (status === 'running' || status === 'pending' || compactText(card?.status || '') === 'executing') {
      currentPercent = clamp(Math.max(12, overallPercent * 0.55), 0, 84);
    }
  } else if (expectedChecks > 0) {
    let inferredChecks = 0;
    if (overallPercent >= 100) {
      inferredChecks = expectedChecks;
    } else if (status === 'reviewing') {
      inferredChecks = Math.max(1, Math.round(expectedChecks * 0.72));
    } else if (status === 'running' || compactText(card?.status || '') === 'executing') {
      inferredChecks = Math.max(1, Math.round(expectedChecks * 0.24));
    } else if (overallPercent >= 85) {
      inferredChecks = Math.max(1, Math.round(expectedChecks * 0.72));
    } else if (overallPercent >= 45) {
      inferredChecks = Math.max(1, Math.round(expectedChecks * 0.36));
    }
    currentPercent = clamp((Math.min(inferredChecks, expectedChecks) / expectedChecks) * 100, 0, 100);
  } else if (overallPercent > 0) {
    currentPercent = clamp(card?.status === 'done' ? 100 : Math.max(8, overallPercent * 0.62), 0, 100);
  }

  const currentLabel = smartCurrentLabel(card, {
    touchedFiles,
    expectedFiles,
    expectedChecks,
    currentPercent,
  });
  const stream = fileStreamProgressModel(card, {
    touchedFiles,
    expectedFiles,
    expectedChecks,
    currentPercent,
  });
  const jobState = jobStateModel(card);

  const shouldRender = overallPercent > 0 || currentPercent > 0 || stream.shouldRender || ['executing', 'reviewing', 'blocked'].includes(String(card?.status || ''));
  return {
    shouldRender,
    overallPercent,
    overallLabel: `${Math.round(overallPercent)}%`,
    currentPercent,
    currentLabel,
    streamPercent: stream.streamPercent,
    streamLabel: stream.streamLabel,
    streamShouldRender: stream.shouldRender,
    jobStateVisible: jobState.visible,
    jobStateTone: jobState.tone,
    jobStateLabel: jobState.label,
  };
}

function progressStackMarkup(progress) {
  return `
      <div class="bb-card__progress-stack" data-card-progress-stack>
        ${progress.jobStateVisible ? `
        <div class="bb-card__job-state bb-card__job-state--${escapeHtml(progress.jobStateTone || 'idle')}" data-job-state>
          <span>Job</span>
          <strong data-job-state-label>${escapeHtml(progress.jobStateLabel)}</strong>
        </div>
        ` : ''}
        <div class="bb-card__progress-row" data-progress-row="overall">
          <div class="bb-card__progress-head">
            <span>Overall progress</span>
            <strong data-progress-value="overall">${escapeHtml(progress.overallLabel)}</strong>
          </div>
          <div class="bb-card__progress bb-card__progress--overall"><div class="bb-card__progress-bar" data-progress-bar="overall" style="width:${progress.overallPercent}%"></div></div>
        </div>
        <div class="bb-card__progress-row" data-progress-row="current">
          <div class="bb-card__progress-head">
            <span>Current progress</span>
            <strong data-progress-value="current">${escapeHtml(progress.currentLabel)}</strong>
          </div>
          <div class="bb-card__progress bb-card__progress--current"><div class="bb-card__progress-bar" data-progress-bar="current" style="width:${progress.currentPercent}%"></div></div>
        </div>
        ${progress.streamShouldRender ? `
        <div class="bb-card__progress-row" data-progress-row="stream">
          <div class="bb-card__progress-head">
            <span>Current file stream</span>
            <strong data-progress-value="stream">${escapeHtml(progress.streamLabel)}</strong>
          </div>
          <div class="bb-card__progress bb-card__progress--stream"><div class="bb-card__progress-bar" data-progress-bar="stream" style="width:${progress.streamPercent}%"></div></div>
        </div>
        ` : ''}
      </div>
    `;
}

function progressRowMarkup(kind, title, label, percent) {
  return `
        <div class="bb-card__progress-row" data-progress-row="${kind}">
          <div class="bb-card__progress-head">
            <span>${title}</span>
            <strong data-progress-value="${kind}">${escapeHtml(label)}</strong>
          </div>
          <div class="bb-card__progress bb-card__progress--${kind}"><div class="bb-card__progress-bar" data-progress-bar="${kind}" style="width:${percent}%"></div></div>
        </div>
      `;
}

function syncProgressRow(stack, kind, title, label, percent, present = true) {
  let row = stack.querySelector(`[data-progress-row="${kind}"]`);
  if (!present) {
    if (row) row.remove();
    return;
  }
  if (!row) {
    const anchor = kind === 'stream' ? null : stack.querySelector('[data-progress-row="stream"]');
    const markup = progressRowMarkup(kind, title, label, percent);
    if (anchor) anchor.insertAdjacentHTML('beforebegin', markup);
    else stack.insertAdjacentHTML('beforeend', markup);
    row = stack.querySelector(`[data-progress-row="${kind}"]`);
  }
  const value = row?.querySelector(`[data-progress-value="${kind}"]`);
  const bar = row?.querySelector(`[data-progress-bar="${kind}"]`);
  if (value) value.textContent = String(label || '');
  if (bar) bar.style.width = `${percent}%`;
}

function syncJobState(stack, progress) {
  let el = stack.querySelector('[data-job-state]');
  if (!progress.jobStateVisible) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    stack.insertAdjacentHTML('afterbegin', `
        <div class="bb-card__job-state bb-card__job-state--${escapeHtml(progress.jobStateTone || 'idle')}" data-job-state>
          <span>Job</span>
          <strong data-job-state-label>${escapeHtml(progress.jobStateLabel)}</strong>
        </div>
      `);
    el = stack.querySelector('[data-job-state]');
  }
  el.className = `bb-card__job-state bb-card__job-state--${progress.jobStateTone || 'idle'}`;
  const label = el.querySelector('[data-job-state-label]');
  if (label) label.textContent = String(progress.jobStateLabel || '');
}

function updateProgressStackInPlace(stack, progress) {
  syncJobState(stack, progress);
  syncProgressRow(stack, 'overall', 'Overall progress', progress.overallLabel, progress.overallPercent, true);
  syncProgressRow(stack, 'current', 'Current progress', progress.currentLabel, progress.currentPercent, true);
  syncProgressRow(stack, 'stream', 'Current file stream', progress.streamLabel, progress.streamPercent, Boolean(progress.streamShouldRender));
}

function applyCardProgressToElement(cardElement, card) {
  if (!cardElement || !card) return;
  const details = cardElement.querySelector('.bb-card__details');
  if (!details) return;
  const progress = cardProgressModel(card);
  const existing = details.querySelector('[data-card-progress-stack]');
  if (!progress.shouldRender) {
    if (existing) existing.remove();
    return;
  }
  if (!existing) {
    details.insertAdjacentHTML('beforeend', progressStackMarkup(progress));
    return;
  }
  updateProgressStackInPlace(existing, progress);
}

function refreshVisibleCardProgress() {
  const root = document.getElementById('board');
  if (!root) return;
  for (const cardElement of Array.from(root.querySelectorAll('.bb-card[data-card-id]'))) {
    const card = findCard(cardElement.dataset.cardId || '');
    if (!card) continue;
    applyCardProgressToElement(cardElement, card);
  }
}

function cardEl(card) {
  const el = document.createElement('div');
  el.className = 'bb-card';
  el.dataset.cardId = card.id;
  el.dataset.status = card.status;
  el.draggable = true;
  const isExpanded = expandedCardIds.has(card.id);
  const fileBadge = card.files?.length ? `<span class="bb-badge bb-badge--blue">${card.files.length} file${card.files.length === 1 ? '' : 's'}</span>` : '';
  const verifyBadge = card.verification?.length ? `<span class="bb-badge bb-badge--amber">${card.verification.length} verify</span>` : '';
  const depBadge = card.deps?.length ? `<span class="bb-badge bb-badge--gray">${card.deps.length} deps</span>` : '';
  const checkpointBadge = hasPendingQuestions(card) ? '<span class="bb-badge bb-badge--amber">checkpoint</span>' : '';
  const resume = resumeState(card);
  const resumeBadge = resume ? `<span class="bb-badge bb-badge--blue">resume: ${escapeHtml(resume.status)}</span>` : '';
  const coordination = coordinationState(card);
  const coordinationBadge = coordination ? '<span class="bb-badge bb-badge--red">coordination</span>' : '';
  const mergeBadge = mergeRecommendation(card) ? '<span class="bb-badge bb-badge--amber">merge?</span>' : '';
  const jobId = card.job_id || card.metadata?.last_job?.job_id || '';
  const workingDir = card.metadata?.last_job?.cwd || card.metadata?.last_job?.execution_cwd || '';
  const progress = cardProgressModel(card);
  const progressBars = progress.shouldRender ? progressStackMarkup(progress) : '';
  const bodyText = (card.body || '').slice(0, 1500);
  const hasBody = bodyText.length > 0;
  el.innerHTML = `
    <div class="bb-card__tab">
      <span class="bb-card__state-dot"></span>
      <div class="bb-card__title">${escapeHtml(card.title)}</div>
      <button class="bb-card__inspect" type="button">Inspect</button>
      <span class="bb-card__chevron">▸</span>
    </div>
    <div class="bb-card__details">
      ${hasBody ? `<div class="bb-card__body" data-clamped="1">${escapeHtml(bodyText)}</div>` : ''}
      <div class="bb-card__submeta">
        <span class="bb-card__submeta-item">${escapeHtml(card.id || '')}</span>
        ${jobId ? `<span class="bb-card__submeta-item">job ${escapeHtml(jobId)}</span>` : ''}
        ${workingDir ? `<span class="bb-card__submeta-item" title="${escapeHtml(workingDir)}">${escapeHtml(workingDir)}</span>` : ''}
      </div>
      <div class="bb-card__meta">
        <span class="bb-badge bb-badge--${statusColor(card.status)}">${card.status}</span>
        ${fileBadge}${verifyBadge}${depBadge}${checkpointBadge}${resumeBadge}${coordinationBadge}${mergeBadge}
      </div>
      ${progressBars}
    </div>
  `;
  if (isExpanded) {
    el.dataset.expanded = '1';
    const chevron = el.querySelector('.bb-card__chevron');
    if (chevron) chevron.textContent = '▾';
  }
  el.addEventListener('click', (e) => {
    if (e.target.closest('.bb-card__inspect')) return;
    const expanded = el.dataset.expanded === '1';
    const nextExpanded = !expanded;
    el.dataset.expanded = nextExpanded ? '1' : '0';
    if (nextExpanded) expandedCardIds.add(card.id);
    else expandedCardIds.delete(card.id);
    const chevron = el.querySelector('.bb-card__chevron');
    if (chevron) chevron.textContent = nextExpanded ? '▾' : '▸';
  });
  el.querySelector('.bb-card__inspect')?.addEventListener('click', (e) => {
    e.stopPropagation();
    openCardModal(card);
  });
  el.addEventListener('dragstart', (e) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', card.id);
  });
  return el;
}

function hasLiveJobCards() {
  const projectId = currentProjectId();
  if (!projectId) return false;
  const jobs = store.get().jobs || [];
  return jobs.some((job) => {
    if (!ACTIVE_JOB_STATUSES.has(String(job?.status || ''))) return false;
    const task = job?.task || {};
    return String(task.project_id || '') === projectId && String(task.card_id || '');
  });
}

function syncProgressTimer() {
  const shouldRun = hasLiveJobCards();
  if (shouldRun && !progressTimer) {
    progressTimer = window.setInterval(() => refreshVisibleCardProgress(), 900);
    return;
  }
  if (!shouldRun && progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = 0;
  }
}

/** Show a "▾ more" toggle only when the clamped body actually overflows. */
function attachExpandToggle(cardElement) {
  const body = cardElement.querySelector('.bb-card__body');
  const more = cardElement.querySelector('.bb-card__more');
  if (!body || !more) return;
  const overflows = body.scrollHeight - body.clientHeight > 2;
  if (!overflows) {
    body.dataset.fits = '1';
    more.remove();
    return;
  }
  more.hidden = false;
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    const expanded = body.dataset.expanded === '1';
    if (expanded) {
      delete body.dataset.expanded;
      more.textContent = '▾ more';
    } else {
      body.dataset.expanded = '1';
      more.textContent = '▴ less';
    }
  });
}

function statusColor(status) {
  return {
    inbox: 'gray',
    designing: 'purple',
    planning: 'purple',
    ready: 'blue',
    executing: 'teal',
    reviewing: 'amber',
    blocked: 'red',
    done: 'green',
  }[status] || 'gray';
}

function colEl(status, cards) {
  const wrap = document.createElement('section');
  wrap.className = 'bb-col';
  wrap.dataset.status = status;
  wrap.innerHTML = `
    <header class="bb-col__header">
      <div class="bb-col__name">${COLUMN_LABELS[status] || status}</div>
      <div class="bb-col__count">${cards.length}</div>
    </header>
    <div class="bb-col__body" data-drop="${status}"></div>
  `;
  const body = wrap.querySelector('.bb-col__body');
  for (const card of cards) {
    if (filterQuery && !cardMatches(card, filterQuery)) continue;
    body.appendChild(cardEl(card));
  }
  body.addEventListener('dragover', (e) => {
    e.preventDefault();
    body.classList.add('bb-col__body--drop');
  });
  body.addEventListener('dragleave', () => body.classList.remove('bb-col__body--drop'));
  body.addEventListener('drop', async (e) => {
    e.preventDefault();
    body.classList.remove('bb-col__body--drop');
    const cardId = e.dataTransfer.getData('text/plain');
    if (!cardId) return;
    const projectId = store.get().activeProjectId;
    const card = findCard(cardId);
    try {
      const updates = buildMoveUpdates(card, status);
      await api.updateCard(projectId, cardId, updates);
      if (status === 'executing' && shouldSubmitIteration(card, updates)) {
        const context = iterationContext(card, updates.metadata);
        const pausedJob = activeJobForCard(projectId, card.id, 'paused');
        const result = pausedJob ? await api.resumeJob(pausedJob.job_id) : await api.submitJob({
          objective: executionObjective(card),
          cwd: iterationCwd(card) || await resolvePreferredWorkingDir(),
          files: card.files || [],
          constraints: card.constraints || [],
          verification: card.verification || [],
          context,
          project_id: projectId,
          card_id: card.id,
        });
        if (!result?.orchestrated) {
          const nextMetadata = { ...(updates.metadata || {}) };
          if (nextMetadata.resume_from) {
            nextMetadata.last_resume_from = nextMetadata.resume_from;
            delete nextMetadata.resume_from;
          }
          await api.updateCard(projectId, cardId, { job_id: result.job_id, metadata: nextMetadata });
        }
        store.setJobs(await api.listJobs());
        const toastText = result?.orchestrated
          ? `Orchestrated into ${Number(result?.child_card_ids?.length || 0) || 0} child jobs. Starting with ${result.job_id}.`
          : (result.status === 'paused' ? `Job ${result.job_id} is still paused for coordination.` : `Iteration job ${result.job_id} ${pausedJob ? 'resumed' : 'submitted'}.`);
        bus.emit('app:toast', { kind: result?.orchestrated ? 'info' : (result.status === 'paused' ? 'warning' : 'info'), text: toastText });
      }
      const board = await api.board(projectId);
      store.setBoard(board);
    } catch (err) {
      alert(`Move failed: ${err.message}`);
    }
  });
  return wrap;
}

function renderMobileLaneFilter(root, columns) {
  let toolbar = root.querySelector('.bb-board__mobile-filter');
  if (!toolbar) {
    toolbar = document.createElement('div');
    toolbar.className = 'bb-board__mobile-filter';
    toolbar.innerHTML = `
      <label class="bb-board__mobile-filter-label" for="bb-board-mobile-lane">Lane</label>
      <select id="bb-board-mobile-lane" class="bb-board__mobile-filter-select"></select>
    `;
    root.appendChild(toolbar);
  }
  const select = toolbar.querySelector('#bb-board-mobile-lane');
  if (!select) return;
  const activeValue = columns.includes(mobileLaneFilter) ? mobileLaneFilter : (columns[0] || '');
  mobileLaneFilter = activeValue;
  select.innerHTML = columns.map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(COLUMN_LABELS[status] || status)}</option>`).join('');
  select.value = activeValue;
  if (select.dataset.bound !== '1') {
    select.dataset.bound = '1';
    select.addEventListener('change', () => {
      mobileLaneFilter = select.value || '';
      renderBoard();
    });
  }
}

function findCard(cardId) {
  const snapshot = store.get().board;
  const cardsByColumn = snapshot?.cards_by_column || {};
  for (const cards of Object.values(cardsByColumn)) {
    const match = (cards || []).find((card) => card.id === cardId);
    if (match) return match;
  }
  return null;
}

function buildMoveUpdates(card, status) {
  if (!card) return { status };
  const metadata = { ...(card.metadata || {}) };
  if (hasPendingQuestions(card) || metadata.resume_from || coordinationState(card)) {
    metadata.resume_from = {
      status,
      previous_status: card.status,
      reason: hasPendingQuestions(card) ? 'checkpoint rerouted by user' : coordinationState(card) ? 'coordination rerouted by user' : 'manual iteration reroute',
      moved_at: Date.now() / 1000,
    };
    if (metadata.pending_questions) {
      metadata.deferred_questions = metadata.pending_questions;
      delete metadata.pending_questions;
    }
    if (metadata.coordination) {
      metadata.last_coordination = metadata.coordination;
      delete metadata.coordination;
    }
    return { status, metadata, progress: 0 };
  }
  return { status };
}

function shouldSubmitIteration(card, updates) {
  return Boolean(card && updates?.metadata && (hasPendingQuestions(card) || coordinationState(card) || updates.metadata.resume_from));
}

function activeJobForCard(projectId, cardId, preferredStatus = '') {
  const card = findCard(cardId);
  const orchestration = card?.metadata?.orchestration || {};
  const childJobIds = new Set(Array.isArray(orchestration.child_job_ids) ? orchestration.child_job_ids.map((item) => String(item || '')) : []);
  const jobs = store.get().jobs || [];
  const matches = jobs.filter((job) => {
    if (!ACTIVE_JOB_STATUSES.has(job.status)) return false;
    const task = job.task || {};
    return (
      (task.project_id === projectId && task.card_id === cardId)
      || job.card_id === cardId
      || (task.project_id === projectId && task.parent_card_id === cardId)
      || (task.project_id === projectId && task.root_card_id === cardId)
      || childJobIds.has(String(job.job_id || ''))
    );
  });
  return matches.find((job) => job.status === preferredStatus) || matches[0] || null;
}

function iterationCwd(card) {
  return card?.metadata?.last_job?.cwd || card?.metadata?.last_job?.execution_cwd || '';
}

function iterationContext(card, metadata) {
  const lines = [];
  const resume = metadata?.resume_from;
  if (resume) {
    lines.push(`User manually rerouted this card to restart from pipeline state: ${resume.status}.`);
    if (resume.reason) lines.push(`Reroute reason: ${resume.reason}.`);
  }
  const answers = metadata?.checkpoint_answers || card?.metadata?.checkpoint_answers || [];
  if (Array.isArray(answers) && answers.length) {
    lines.push('User answered previous checkpoint questions:');
    lines.push(...answers.slice(-6).map((answer) => `- ${answer.prompt || answer.question_id}: ${answer.value || answer.label || answer.option_id}`));
  }
  const deferred = metadata?.deferred_questions;
  if (deferred?.reason) lines.push(`Previous deferred checkpoint: ${deferred.reason}`);
  return lines.join('\n');
}

function cardMatches(card, query) {
  return (
    (card.title || '').toLowerCase().includes(query) ||
    (card.body  || '').toLowerCase().includes(query) ||
    (executionObjective(card) || '').toLowerCase().includes(query) ||
    (card.files || []).some((f) => f.toLowerCase().includes(query))
  );
}

export function renderBoard() {
  const root = document.getElementById('board');
  if (!root) return;
  const snapshot = store.get().board;
  const knownCardIds = new Set();
  for (const cards of Object.values(snapshot?.cards_by_column || {})) {
    for (const card of cards || []) knownCardIds.add(card.id);
  }
  for (const cardId of Array.from(expandedCardIds)) {
    if (!knownCardIds.has(cardId)) expandedCardIds.delete(cardId);
  }
  for (const [cardId, state] of Array.from(liveFileStreams.entries())) {
    if (!knownCardIds.has(cardId) && !state?.active) liveFileStreams.delete(cardId);
  }
  root.innerHTML = '';
  if (!snapshot) {
    root.innerHTML = '<div style="margin:auto;color:var(--fg-3)">Loading board…</div>';
    return;
  }
  const columns = Array.isArray(snapshot.columns) ? snapshot.columns : [];
  const visibleStatuses = isMobileBoardViewport()
    ? (mobileLaneFilter && columns.includes(mobileLaneFilter) ? [mobileLaneFilter] : columns.slice(0, 1))
    : columns;
  root.dataset.mobileFiltered = isMobileBoardViewport() ? '1' : '0';
  if (isMobileBoardViewport()) {
    renderMobileLaneFilter(root, columns);
  } else {
    mobileLaneFilter = '';
  }
  for (const status of visibleStatuses) {
    const cards = snapshot.cards_by_column[status] || [];
    root.appendChild(colEl(status, cards));
  }
}

bus.on('store:board', renderBoard);
bus.on('store:jobs', () => {
  syncProgressTimer();
  renderBoard();
});
bus.on('store:active', () => {
  syncProgressTimer();
  renderBoard();
});
bus.on('ws:coding:job.started', (payload) => {
  setLiveFileStreamStateFromJob(payload, true);
  scheduleBoardRender();
});
bus.on('ws:coding:job.reviewing', (payload) => {
  setLiveFileStreamStateFromJob(payload, false);
  scheduleBoardRender();
});
bus.on('ws:coding:job.completed', (payload) => {
  setLiveFileStreamStateFromJob(payload, false);
  scheduleBoardRender();
});
bus.on('ws:coding:job.failed', (payload) => {
  setLiveFileStreamStateFromJob(payload, false);
  scheduleBoardRender();
});
bus.on('ws:coding:job.paused', (payload) => {
  setLiveFileStreamStateFromJob(payload, false);
  scheduleBoardRender();
});
bus.on('ws:coding:job.progress', applyLiveJobProgress);
bus.on('ws:coding:cli.transcript', applyLiveTranscript);
bus.on('board:filter', ({ query }) => {
  filterQuery = query || '';
  renderBoard();
});

if (window.__bbBoardResizeBound !== true) {
  window.__bbBoardResizeBound = true;
  window.addEventListener('resize', () => {
    renderBoard();
  });
}
