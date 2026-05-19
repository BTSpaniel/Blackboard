// Project-chat sidebar — conversational by default, with explicit card actions.
//
// UX patterns (per common chat UI guidance: ChatGPT / Claude / Linear):
//   • Enter sends, Shift+Enter inserts a newline, IME composition is respected.
//   • Textarea auto-grows up to 12 lines, then scrolls.
//   • Always scrolls to bottom on new messages and on session switch.
//   • A "Thinking…" placeholder bubble shows while the planner is working;
//     it's replaced atomically with the real assistant message.
//   • Hover any message to reveal a "Copy" button (no hidden interactions).
//   • Server is the source of truth — full history is reloaded on project
//     switch and on page reload (Luna pattern).
//   • Cards are no longer created for every prose turn; the assistant only
//     emits cards when the user explicitly asks (handled server-side).

import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { containsRenderableArtifacts, renderArtifactsInto } from '/ui/shell/artifacts.js';
import { confirmDialog, toast } from '/ui/shell/dialog.js';

let messages = [];
let currentSessionId = '';
let currentProjectId = '';
let loading = false;       // history reload state
let pending = false;       // request in flight
let activeClientMessageId = '';
let wsRenderedClientMessageId = '';
let awaitingHttpClientMessageId = '';
let streamingReplyText = '';
let activeProgress = null;
let paintTimer = null;
let paintRaf = null;
let lastPaintAt = 0;
const PAINT_THROTTLE_MS = 80;
const AUTO_SCROLL_BOTTOM_THRESHOLD_PX = 72;
const AUTO_SCROLL_MIN_DELTA_PX = 6;
let shouldAutoFollowNextPaint = false;
let forceBottomNextPaint = false;
let closingProgress = null;
let closingProgressTimer = null;
const PROGRESS_FADE_MS = 320;
const PROGRESS_STATS_KEY_PREFIX = 'bb.chat.progress.stats.v1';
const PROGRESS_FALLBACK_TOTAL_SECONDS = 18;
const PROGRESS_FALLBACK_FIRST_REPLY_SECONDS = 7;
const PROGRESS_VISUAL_FLOOR = 6;
const PROGRESS_SOFT_CAP_PERCENT = 98.4;
const PROGRESS_LERP_PER_SECOND = 6.5;
const PROGRESS_FORCE_PAINT_MS = 240;
let progressAnimationRaf = null;
let progressVisualPercent = PROGRESS_VISUAL_FLOOR;
let progressStartedAt = 0;
let progressFirstReplyAt = 0;
let progressLastFrameAt = 0;
let progressLastPaintTickAt = 0;
const CHAT_SCROLL_KEY_PREFIX = 'bb.chat.scroll.v1';
let progressStats = null;
let messagesVersion = 0;
let renderedMessagesVersion = -1;
let renderedSurfaceMode = '';
let chatMessagesRoot = null;
let chatHistoryHost = null;
let chatTransientHost = null;
let pendingBubbleEl = null;
let pendingBodyEl = null;
let pendingProgressEl = null;
let pendingMode = '';
let closingProgressEl = null;
let pendingScrollRestore = null;
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition || null;
const WAKE_WORD_RE = /^\s*(?:hey|hi|okay|ok)\s+blackboard\b[\s,:-]*/i;
let speechRecognition = null;
let speechMode = '';
let desiredSpeechMode = '';
let wakeAwaitingCommand = false;
let wakeAwaitingTimer = null;
let wakeRestartTimer = null;

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function stripCardsBlocks(text) {
  const value = String(text || '');
  const stripped = value.replace(/```cards\b[\s\S]*?```/gi, '').replace(/```cards\b[\s\S]*$/i, '');
  return stripped.trim();
}

const MARKDOWN_IMAGE_RE = /!\[([^\]]*)\]\((\S+?)(?:\s+"([^"]*)")?\)/g;
const HTML_IMAGE_RE = /<img\b[^>]*>/gi;
const DIRECT_IMAGE_URL_RE = /^https?:\/\/[^\s<>"']+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?[^\s<>"']*)?(?:#[^\s<>"']*)?$/i;

function htmlAttr(tag, name) {
  const match = String(tag || '').match(new RegExp(`${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'>]+))`, 'i'));
  return String(match?.[1] ?? match?.[2] ?? match?.[3] ?? '').trim();
}

function normalizeMessageImage(value) {
  const source = typeof value === 'string'
    ? String(value || '').trim()
    : String(value?.src || value?.url || '').trim();
  if (!source) return null;
  let resolved = source;
  if (/^data:/i.test(source)) {
    if (!/^data:image\//i.test(source)) return null;
  } else {
    try {
      const parsed = new URL(source, window.location.origin);
      const protocol = String(parsed.protocol || '').toLowerCase();
      if (!['http:', 'https:', 'blob:'].includes(protocol)) return null;
      resolved = parsed.toString();
    } catch {
      return null;
    }
  }
  const alt = typeof value === 'string' ? '' : String(value?.alt || '').trim();
  const title = typeof value === 'string' ? '' : String(value?.title || '').trim();
  return { src: resolved, alt, title };
}

function normalizeMessageImages(value) {
  const items = Array.isArray(value) ? value : value ? [value] : [];
  const out = [];
  const seen = new Set();
  for (const item of items) {
    const normalized = normalizeMessageImage(item);
    if (!normalized || seen.has(normalized.src)) continue;
    seen.add(normalized.src);
    out.push(normalized);
    if (out.length >= 8) break;
  }
  return out;
}

function collectMessageImageEmbeds(text) {
  const value = String(text || '');
  const out = [];
  const overlaps = (start, end) => out.some((item) => start < item.end && end > item.start);
  const push = (start, end, image) => {
    if (!image || start < 0 || end <= start || overlaps(start, end)) return;
    out.push({ ...image, start, end });
  };
  MARKDOWN_IMAGE_RE.lastIndex = 0;
  let match;
  while ((match = MARKDOWN_IMAGE_RE.exec(value)) !== null) {
    push(match.index, MARKDOWN_IMAGE_RE.lastIndex, normalizeMessageImage({ src: match[2], alt: match[1], title: match[3] || '' }));
  }
  HTML_IMAGE_RE.lastIndex = 0;
  while ((match = HTML_IMAGE_RE.exec(value)) !== null) {
    const rawTag = match[0] || '';
    push(match.index, HTML_IMAGE_RE.lastIndex, normalizeMessageImage({
      src: htmlAttr(rawTag, 'src'),
      alt: htmlAttr(rawTag, 'alt'),
      title: htmlAttr(rawTag, 'title'),
    }));
  }
  let offset = 0;
  for (const line of value.split('\n')) {
    const trimmed = String(line || '').trim();
    if (DIRECT_IMAGE_URL_RE.test(trimmed)) {
      const start = offset + line.indexOf(trimmed);
      const end = start + trimmed.length;
      push(start, end, normalizeMessageImage(trimmed));
    }
    offset += line.length + 1;
  }
  out.sort((left, right) => left.start - right.start);
  return out;
}

function hasRenderableMessageEmbeds(text, images = []) {
  return normalizeMessageImages(images).length > 0 || collectMessageImageEmbeds(text).length > 0;
}

function appendMessageTextSegment(host, text) {
  const value = String(text || '');
  if (!/\S/.test(value)) return;
  const block = document.createElement('div');
  block.className = 'bb-msg__segment';
  if (containsRenderableArtifacts(value)) {
    renderArtifactsInto(block, value);
  } else {
    block.style.whiteSpace = 'pre-wrap';
    block.textContent = value;
  }
  host.appendChild(block);
}

function appendMessageImage(host, image) {
  const figure = document.createElement('figure');
  figure.className = 'bb-msg__image';
  const frame = document.createElement('a');
  frame.className = 'bb-msg__image-frame';
  frame.href = image.src;
  frame.target = '_blank';
  frame.rel = 'noreferrer noopener';
  const img = document.createElement('img');
  img.src = image.src;
  img.alt = image.alt || image.title || 'Assistant image';
  img.loading = 'lazy';
  frame.appendChild(img);
  figure.appendChild(frame);
  const captionText = String(image.title || image.alt || '').trim();
  if (captionText) {
    const caption = document.createElement('figcaption');
    caption.className = 'bb-msg__image-caption';
    caption.textContent = captionText;
    figure.appendChild(caption);
  }
  host.appendChild(figure);
}

function renderMessageRichContent(host, text, images = []) {
  const value = String(text || '');
  const embeds = collectMessageImageEmbeds(value);
  const supplementalImages = normalizeMessageImages(images);
  host.replaceChildren();
  let lastIndex = 0;
  for (const embed of embeds) {
    appendMessageTextSegment(host, value.slice(lastIndex, embed.start));
    appendMessageImage(host, embed);
    lastIndex = embed.end;
  }
  appendMessageTextSegment(host, value.slice(lastIndex));
  const seen = new Set(embeds.map((item) => item.src));
  for (const image of supplementalImages) {
    if (seen.has(image.src)) continue;
    appendMessageImage(host, image);
  }
}

function normalizeTranscriptText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function clearWakeAwaiting() {
  wakeAwaitingCommand = false;
  if (wakeAwaitingTimer) {
    clearTimeout(wakeAwaitingTimer);
    wakeAwaitingTimer = null;
  }
}

function armWakeAwaiting() {
  clearWakeAwaiting();
  wakeAwaitingCommand = true;
  wakeAwaitingTimer = window.setTimeout(() => {
    wakeAwaitingCommand = false;
    wakeAwaitingTimer = null;
    updateVoiceControls();
  }, 10000);
}

function extractWakeCommand(text) {
  const normalized = normalizeTranscriptText(text);
  if (!normalized) return { matched: false, command: '' };
  if (!WAKE_WORD_RE.test(normalized)) return { matched: false, command: '' };
  return { matched: true, command: normalizeTranscriptText(normalized.replace(WAKE_WORD_RE, '')) };
}

function updateVoiceControls() {
  const voiceBtn = document.getElementById('chat-voice');
  const wakeBtn = document.getElementById('chat-wake');
  if (!voiceBtn || !wakeBtn) return;
  const supported = Boolean(SpeechRecognitionCtor);
  voiceBtn.disabled = !supported;
  wakeBtn.disabled = !supported;
  voiceBtn.classList.toggle('bb-sidebar__voice-btn--active', speechMode === 'voice');
  voiceBtn.classList.toggle('bb-sidebar__voice-btn--recording', speechMode === 'voice');
  wakeBtn.classList.toggle('bb-sidebar__voice-btn--active', desiredSpeechMode === 'wake' || speechMode === 'wake');
  wakeBtn.classList.toggle('bb-sidebar__voice-btn--armed', wakeAwaitingCommand);
  if (!supported) {
    voiceBtn.title = 'Browser speech recognition is not supported here';
    wakeBtn.title = 'Wake-word mode requires browser speech recognition support';
    return;
  }
  voiceBtn.title = speechMode === 'voice' ? 'Stop voice transcription' : 'Voice transcription';
  wakeBtn.title = desiredSpeechMode === 'wake' || speechMode === 'wake'
    ? (wakeAwaitingCommand ? 'Wake-word mode active — listening for your command' : 'Wake-word mode active — say hey blackboard')
    : 'Wake-word mode';
}

function stopSpeechRecognition() {
  desiredSpeechMode = '';
  clearWakeAwaiting();
  if (wakeRestartTimer) {
    clearTimeout(wakeRestartTimer);
    wakeRestartTimer = null;
  }
  const current = speechRecognition;
  speechRecognition = null;
  speechMode = '';
  updateVoiceControls();
  if (current) {
    current.onend = null;
    current.onerror = null;
    current.onresult = null;
    try { current.stop(); } catch { /* */ }
  }
}

function applyTranscriptText(text, { autoSend = false } = {}) {
  const input = document.getElementById('chat-input');
  if (!input) return;
  const normalized = normalizeTranscriptText(text);
  if (!normalized) return;
  const existing = String(input.value || '').trim();
  input.value = existing && !autoSend
    ? `${existing}${existing.endsWith('\n') ? '' : '\n'}${normalized}`
    : normalized;
  resizeInput();
  input.focus();
  input.dispatchEvent(new Event('input'));
  input.classList.add('transcript-flash');
  window.setTimeout(() => input.classList.remove('transcript-flash'), 700);
  if (autoSend && !pending) submit();
}

function handleSpeechResult(event, mode) {
  const chunks = [];
  for (let i = event.resultIndex; i < event.results.length; i += 1) {
    const result = event.results[i];
    if (!result?.isFinal) continue;
    chunks.push(normalizeTranscriptText(result[0]?.transcript || ''));
  }
  if (!chunks.length) return;
  if (mode === 'wake') {
    for (const chunk of chunks) {
      const wake = extractWakeCommand(chunk);
      if (wake.matched) {
        if (wake.command) {
          clearWakeAwaiting();
          applyTranscriptText(wake.command, { autoSend: true });
        } else {
          armWakeAwaiting();
          toast('Wake phrase heard. Listening for your command…', { kind: 'success', timeout: 1400 });
        }
        updateVoiceControls();
        continue;
      }
      if (wakeAwaitingCommand) {
        clearWakeAwaiting();
        applyTranscriptText(chunk, { autoSend: true });
        updateVoiceControls();
      }
    }
    return;
  }
  applyTranscriptText(chunks.join(' '), { autoSend: false });
  toast('Transcript ready', { kind: 'success', timeout: 1000 });
}

function startSpeechRecognition(mode = 'voice') {
  if (!SpeechRecognitionCtor) {
    toast('Speech recognition is not supported in this browser.', { kind: 'warn' });
    return;
  }
  if (speechMode === mode || (desiredSpeechMode === mode && speechRecognition)) {
    stopSpeechRecognition();
    return;
  }
  stopSpeechRecognition();
  const recognition = new SpeechRecognitionCtor();
  desiredSpeechMode = mode;
  speechMode = mode;
  recognition.lang = 'en-US';
  recognition.interimResults = mode !== 'wake';
  recognition.maxAlternatives = 1;
  recognition.continuous = mode === 'wake';
  recognition.onresult = (event) => handleSpeechResult(event, mode);
  recognition.onerror = (event) => {
    const code = String(event?.error || '').trim();
    if (code === 'no-speech') return;
    stopSpeechRecognition();
    toast(code === 'not-allowed' ? 'Microphone permission denied.' : `Voice recognition failed: ${code || 'unknown error'}`, { kind: 'error' });
  };
  recognition.onend = () => {
    speechRecognition = null;
    speechMode = '';
    updateVoiceControls();
    if (desiredSpeechMode === 'wake') {
      wakeRestartTimer = window.setTimeout(() => {
        wakeRestartTimer = null;
        if (desiredSpeechMode === 'wake' && !speechRecognition) startSpeechRecognition('wake');
      }, 250);
      return;
    }
    desiredSpeechMode = '';
    clearWakeAwaiting();
    updateVoiceControls();
  };
  speechRecognition = recognition;
  updateVoiceControls();
  try {
    recognition.start();
    if (mode === 'wake') toast('Wake-word mode enabled. Say “hey blackboard”…', { kind: 'success', timeout: 1400 });
  } catch (err) {
    stopSpeechRecognition();
    toast(`Voice recognition failed: ${err.message}`, { kind: 'error' });
  }
}

function newSessionId() {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
}

function newClientMessageId() {
  return `m_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
}

function lsKey(projectId) { return `bb.chat.session.${projectId}`; }

function readPersistedSession(projectId) {
  if (!projectId) return '';
  try { return localStorage.getItem(lsKey(projectId)) || ''; }
  catch { return ''; }
}

function writePersistedSession(projectId, sessionId) {
  if (!projectId) return;
  try { localStorage.setItem(lsKey(projectId), sessionId); } catch { /* */ }
}

function scrollKey(projectId, sessionId) {
  return `${CHAT_SCROLL_KEY_PREFIX}.${projectId || 'global'}.${sessionId || 'default'}`;
}

function readPersistedScroll(projectId, sessionId) {
  if (!projectId || !sessionId) return null;
  try {
    const raw = localStorage.getItem(scrollKey(projectId, sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return {
      top: Math.max(0, Number(parsed.top || 0) || 0),
      atBottom: Boolean(parsed.atBottom),
    };
  } catch {
    return null;
  }
}

function writePersistedScroll(projectId, sessionId, state) {
  if (!projectId || !sessionId) return;
  try {
    localStorage.setItem(scrollKey(projectId, sessionId), JSON.stringify({
      top: Math.max(0, Number(state?.top || 0) || 0),
      atBottom: Boolean(state?.atBottom),
    }));
  } catch { /* */ }
}

function persistCurrentScroll(root = document.getElementById('chat-messages')) {
  if (!root || !currentProjectId || !currentSessionId) return;
  writePersistedScroll(currentProjectId, currentSessionId, {
    top: root.scrollTop,
    atBottom: isNearBottom(root),
  });
}

function markMessagesDirty() {
  messagesVersion += 1;
}

function normalizeWebProvenance(provenance) {
  if (!provenance || typeof provenance !== 'object' || Array.isArray(provenance)) return null;
  const normalizedSources = Array.isArray(provenance.sources)
    ? provenance.sources
      .map((item) => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
        const title = String(item.title || '').trim();
        const url = String(item.url || '').trim();
        const backend = String(item.backend || '').trim();
        if (!title && !url && !backend) return null;
        return { title, url, backend };
      })
      .filter(Boolean)
      .slice(0, 4)
    : [];
  const normalized = {
    ...provenance,
    success: provenance.success !== false,
    tool: String(provenance.tool || '').trim(),
    search_backend: String(provenance.search_backend || '').trim(),
    fetch_backends: Array.isArray(provenance.fetch_backends)
      ? provenance.fetch_backends.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 4)
      : [],
    source_urls: Array.isArray(provenance.source_urls)
      ? provenance.source_urls.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 6)
      : [],
    attempted_tools: Array.isArray(provenance.attempted_tools)
      ? provenance.attempted_tools.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 6)
      : [],
    sources: normalizedSources,
  };
  if (!normalized.tool && !normalized.search_backend && !normalized.fetch_backends.length && !normalized.source_urls.length && !normalized.attempted_tools.length && !normalized.sources.length) {
    return null;
  }
  return normalized;
}

function normalizeMessage(message) {
  const next = { ...(message || {}) };
  next.role = String(next.role || 'system');
  next.content = stripCardsBlocks(typeof next.content === 'string' ? next.content : String(next.content || ''));
  next.raw = next.raw ? stripCardsBlocks(String(next.raw)).slice(0, 4000) : '';
  next.web_provenance = normalizeWebProvenance(next.web_provenance || next.metadata?.web_provenance || null);
  next.images = normalizeMessageImages(next.images || next.metadata?.images || null);
  return next;
}

function replaceMessages(nextMessages) {
  messages = Array.isArray(nextMessages) ? nextMessages.map((message) => normalizeMessage(message)) : [];
  markMessagesDirty();
}

function appendMessage(message) {
  messages.push(normalizeMessage(message));
  markMessagesDirty();
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function progressStatsKey(projectId) {
  return `${PROGRESS_STATS_KEY_PREFIX}.${projectId || 'global'}`;
}

function readProgressStats(projectId) {
  try {
    const raw = localStorage.getItem(progressStatsKey(projectId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const sampleCount = clamp(Number(parsed.sample_count || 0) || 0, 0, 50);
    const totalAvg = Number(parsed.total_avg_seconds || 0);
    const firstReplyAvg = Number(parsed.first_reply_avg_seconds || 0);
    if (!sampleCount || !Number.isFinite(totalAvg) || totalAvg <= 0 || !Number.isFinite(firstReplyAvg) || firstReplyAvg <= 0) return null;
    return {
      sample_count: sampleCount,
      total_avg_seconds: totalAvg,
      first_reply_avg_seconds: firstReplyAvg,
    };
  } catch {
    return null;
  }
}

function writeProgressStats(projectId, stats) {
  try {
    localStorage.setItem(progressStatsKey(projectId), JSON.stringify(stats));
  } catch { /* */ }
}

function formatEta(seconds) {
  const safe = Math.max(0, Number(seconds || 0));
  if (safe < 1) return '<1s';
  if (safe < 90) return `${Math.round(safe)}s`;
  const minutes = Math.floor(safe / 60);
  const remainder = Math.round(safe % 60);
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function currentElapsedSeconds(progress = {}, now = performance.now()) {
  const serverElapsed = Number(progress.elapsed_seconds || 0);
  const clientElapsed = progressStartedAt ? Math.max(0, (now - progressStartedAt) / 1000) : 0;
  return Math.max(serverElapsed, clientElapsed);
}

function selectorEscape(value) {
  return String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function progressEstimate(progress = {}, now = performance.now()) {
  const phase = progress.phase || (streamingReplyText ? 'bleeping' : 'thinking');
  const elapsed = currentElapsedSeconds(progress, now);
  const thinkingTurns = Number(progress.thinking_turns || 0);
  const replyChunks = Number(progress.reply_chunks || 0);
  const turns = Number(progress.turn_count || 0);
  const stats = progressStats || readProgressStats(currentProjectId);
  const sampleCount = Math.max(0, Number(stats?.sample_count || 0));
  const estimatedFirstReply = clamp(sampleCount ? Number(stats?.first_reply_avg_seconds || 0) : PROGRESS_FALLBACK_FIRST_REPLY_SECONDS, 2.5, 45);
  const estimatedTotal = clamp(sampleCount ? Number(stats?.total_avg_seconds || 0) : PROGRESS_FALLBACK_TOTAL_SECONDS, estimatedFirstReply + 2, 120);
  const observedFirstReply = progressFirstReplyAt && progressStartedAt
    ? Math.max(0.35, (progressFirstReplyAt - progressStartedAt) / 1000)
    : estimatedFirstReply;
  let targetPercent = PROGRESS_VISUAL_FLOOR;
  let currentPercent = phase === 'bleeping' ? 8 : 10;
  if (phase === 'bleeping') {
    const replyBudget = Math.max(estimatedTotal - observedFirstReply, 3);
    const replyElapsed = Math.max(0, elapsed - observedFirstReply);
    const replyCurve = 1 - Math.exp(-replyElapsed / Math.max(replyBudget * 0.82, 2.5));
    currentPercent = clamp((replyCurve * 100) + Math.min(10, Math.log1p(Math.max(0, replyChunks)) * 4.4), 8, 100);
    targetPercent = 64 + (replyCurve * 30);
    targetPercent += Math.min(3.5, Math.log1p(Math.max(0, replyChunks)) * 0.95);
    targetPercent += Math.min(2.6, Math.max(0, currentPercent - 46) * 0.032);
  } else {
    const thinkCurve = 1 - Math.exp(-elapsed / Math.max(observedFirstReply * 0.92, 2.4));
    const activityTurns = Math.max(0, thinkingTurns + Math.max(0, turns - 1));
    currentPercent = clamp((thinkCurve * 100) + Math.min(12, Math.log1p(activityTurns) * 4.2), 10, 99);
    targetPercent = 8 + (thinkCurve * 56);
    targetPercent += Math.min(4.5, Math.log1p(Math.max(0, thinkingTurns)) * 0.75);
    targetPercent += Math.min(2.2, Math.max(0, currentPercent - 38) * 0.034);
    targetPercent = Math.min(targetPercent, 68);
  }
  if (elapsed > estimatedTotal) {
    targetPercent += Math.min(3, Math.log1p(elapsed - estimatedTotal + 1) * 1.2);
  }
  targetPercent = clamp(targetPercent, PROGRESS_VISUAL_FLOOR, pending ? PROGRESS_SOFT_CAP_PERCENT : 100);
  const lastAt = progressLastFrameAt || now;
  const deltaSeconds = Math.max(0.016, (now - lastAt) / 1000);
  const factor = 1 - Math.exp(-PROGRESS_LERP_PER_SECOND * deltaSeconds);
  const startPercent = clamp(Number(progress.estimate_percent ?? progressVisualPercent ?? PROGRESS_VISUAL_FLOOR), PROGRESS_VISUAL_FLOOR, pending ? PROGRESS_SOFT_CAP_PERCENT : 100);
  const visualPercent = pending
    ? clamp(startPercent + ((targetPercent - startPercent) * factor), PROGRESS_VISUAL_FLOOR, PROGRESS_SOFT_CAP_PERCENT)
    : clamp(Number(progress.estimate_percent || 100), PROGRESS_VISUAL_FLOOR, 100);
  const progressRemainingSeconds = Math.max(0.35, estimatedTotal * Math.max(0, (100 - visualPercent) / 100));
  let etaSeconds;
  if (phase === 'bleeping') {
    const replyBudget = Math.max(estimatedTotal - observedFirstReply, 3);
    const replyElapsed = Math.max(0, elapsed - observedFirstReply);
    const replyRemainingSeconds = Math.max(0.35, replyBudget - replyElapsed);
    etaSeconds = Math.max(0.35, Math.min(progressRemainingSeconds + 1.1, replyRemainingSeconds + 1.75));
  } else {
    const firstReplyRemaining = Math.max(1.5, observedFirstReply - elapsed);
    const thinkPenalty = Math.min(10, Math.log1p(Math.max(0, thinkingTurns)) * 1.35);
    etaSeconds = Math.max(progressRemainingSeconds, firstReplyRemaining + thinkPenalty);
  }
  const basisLabel = sampleCount ? `${sampleCount} learned run${sampleCount === 1 ? '' : 's'}` : 'learning estimate';
  const remainingLabel = !pending && progress.closing
    ? 'Complete'
    : (etaSeconds <= 0.75 || visualPercent >= 97) && phase === 'bleeping'
      ? 'Finishing up'
      : `ETA ${formatEta(etaSeconds)}`;
  return {
    phase,
    elapsed,
    thinkingTurns,
    replyChunks,
    turns,
    sampleCount,
    basisLabel,
    currentPercent,
    visualPercent,
    targetPercent,
    remainingLabel,
    etaSeconds,
  };
}

function currentPhaseProgress(estimate = {}) {
  const phase = String(estimate.phase || 'thinking');
  const currentPercent = clamp(Number(estimate.currentPercent || 0) || 0, 0, 100);
  if (phase === 'bleeping') {
    const percent = currentPercent;
    const label = estimate.replyChunks > 0 ? 'Replying' : 'Preparing reply';
    return { percent, label };
  }
  const percent = currentPercent;
  const label = estimate.thinkingTurns > 0 ? 'Thinking' : 'Preparing analysis';
  return { percent, label };
}

function stopProgressAnimation() {
  if (progressAnimationRaf) {
    cancelAnimationFrame(progressAnimationRaf);
    progressAnimationRaf = null;
  }
  progressLastFrameAt = 0;
  progressLastPaintTickAt = 0;
}

function ensureProgressAnimation() {
  if (progressAnimationRaf || !pending || !activeProgress) return;
  progressLastFrameAt = performance.now();
  const tick = (now) => {
    progressAnimationRaf = null;
    if (!pending || !activeProgress) {
      progressLastFrameAt = 0;
      return;
    }
    const estimate = progressEstimate(activeProgress, now);
    progressLastFrameAt = now;
    const nextPercent = estimate.visualPercent;
    const nextRemaining = estimate.remainingLabel;
    const nextCurrentPercent = estimate.currentPercent;
    const forcePaint = !progressLastPaintTickAt || (now - progressLastPaintTickAt) >= PROGRESS_FORCE_PAINT_MS;
    const shouldPaint = Math.abs(nextPercent - progressVisualPercent) >= 0.12
      || Math.abs(nextCurrentPercent - Number(activeProgress.current_percent || 0)) >= 0.45
      || nextRemaining !== activeProgress.remaining
      || Math.round(Number(activeProgress.estimate_percent || 0)) !== Math.round(nextPercent)
      || forcePaint;
    progressVisualPercent = nextPercent;
    activeProgress = {
      ...(activeProgress || {}),
      elapsed_seconds: estimate.elapsed,
      estimate_percent: nextPercent,
      current_percent: nextCurrentPercent,
      remaining: nextRemaining,
      eta_seconds: estimate.etaSeconds,
    };
    if (shouldPaint) {
      progressLastPaintTickAt = now;
      schedulePaint();
    }
    progressAnimationRaf = requestAnimationFrame(tick);
  };
  progressAnimationRaf = requestAnimationFrame(tick);
}

function beginProgressTracking() {
  stopProgressAnimation();
  progressStartedAt = performance.now();
  progressFirstReplyAt = 0;
  progressVisualPercent = PROGRESS_VISUAL_FLOOR;
  progressStats = readProgressStats(currentProjectId);
  ensureProgressAnimation();
}

function recordProgressCompletion(progress = {}) {
  if (!progressStartedAt) return;
  const totalSeconds = Math.max(Number(progress.elapsed_seconds || 0), (performance.now() - progressStartedAt) / 1000);
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0.5) return;
  const firstReplySeconds = progressFirstReplyAt && progressStartedAt
    ? Math.max(0.25, (progressFirstReplyAt - progressStartedAt) / 1000)
    : totalSeconds;
  const existing = progressStats || readProgressStats(currentProjectId) || {};
  const sampleCount = Math.max(0, Number(existing.sample_count || 0));
  const alpha = sampleCount < 4 ? 0.35 : 0.18;
  const totalAvg = Number(existing.total_avg_seconds || 0) || totalSeconds;
  const firstReplyAvg = Number(existing.first_reply_avg_seconds || 0) || firstReplySeconds;
  const next = {
    sample_count: Math.min(sampleCount + 1, 50),
    total_avg_seconds: totalAvg + ((totalSeconds - totalAvg) * alpha),
    first_reply_avg_seconds: firstReplyAvg + ((firstReplySeconds - firstReplyAvg) * alpha),
  };
  progressStats = next;
  writeProgressStats(currentProjectId, next);
}

// ── Rendering ───────────────────────────────────────────────────

function scrollToBottom() {
  const root = document.getElementById('chat-messages');
  if (root) root.scrollTop = root.scrollHeight;
}

function isNearBottom(root) {
  if (!root) return true;
  const distance = root.scrollHeight - root.clientHeight - root.scrollTop;
  return distance <= AUTO_SCROLL_BOTTOM_THRESHOLD_PX;
}

function settleScroll(root, shouldStickBottom) {
  if (!root || !shouldStickBottom) return;
  const target = Math.max(0, root.scrollHeight - root.clientHeight);
  if (Math.abs(target - root.scrollTop) < AUTO_SCROLL_MIN_DELTA_PX) return;
  root.scrollTop = target;
}

function restoreScroll(root) {
  if (!root || !pendingScrollRestore) return false;
  const restore = pendingScrollRestore;
  pendingScrollRestore = null;
  if (restore.atBottom) {
    settleScroll(root, true);
    return true;
  }
  const target = Math.max(0, Math.min(Number(restore.top || 0) || 0, Math.max(0, root.scrollHeight - root.clientHeight)));
  if (Math.abs(root.scrollTop - target) >= AUTO_SCROLL_MIN_DELTA_PX) root.scrollTop = target;
  return true;
}

function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => toast('Copied', { kind: 'success', timeout: 900 }),
      () => toast('Copy failed', { kind: 'error', timeout: 1500 }),
    );
  }
}

function messageBubble(msg, idx) {
  const el = document.createElement('div');
  el.className = `bb-msg bb-msg--${msg.role}`;
  el.dataset.idx = String(idx);
  if (msg.client_message_id) el.dataset.clientMessageId = String(msg.client_message_id);

  const head = document.createElement('div');
  head.className = 'bb-msg__head';
  const role = document.createElement('span');
  role.className = 'bb-msg__role';
  const providerLabel = msg.role === 'assistant' && msg.provider_id
    ? `Provider: ${msg.provider_id}`
    : msg.role;
  role.textContent = providerLabel;
  if (msg.role === 'assistant' && msg.provider_model) role.title = `Model: ${msg.provider_model}`;
  head.appendChild(role);

  // Copy button on every message (assistant + user) — visible on hover.
  if (msg.content) {
    const copy = document.createElement('button');
    copy.className = 'bb-msg__copy';
    copy.type = 'button';
    copy.title = 'Copy message';
    copy.textContent = '⧉';
    copy.addEventListener('click', (e) => {
      e.stopPropagation();
      copyToClipboard(msg.content);
    });
    head.appendChild(copy);
  }
  el.appendChild(head);

  const bodyHost = document.createElement('div');
  bodyHost.className = 'bb-msg__body';
  el.appendChild(bodyHost);
  const displayContent = msg.role === 'assistant' && msg.web_provenance
    ? String(msg.content || '').split('\n\nLive lookup provenance:')[0].trimEnd()
    : String(msg.content || '');
  const hasRenderableArtifacts = containsRenderableArtifacts(displayContent || '');
  const hasRenderableEmbeds = hasRenderableMessageEmbeds(displayContent || '', msg.images || []);
  if (msg.checkpoint) {
    bodyHost.appendChild(checkpointElement(msg.checkpoint));
  } else if (hasRenderableArtifacts) {
    renderArtifactsInto(bodyHost, displayContent || '');
    for (const image of normalizeMessageImages(msg.images || [])) appendMessageImage(bodyHost, image);
  } else if (hasRenderableEmbeds) {
    renderMessageRichContent(bodyHost, displayContent || '', msg.images || []);
  } else if (msg.role === 'assistant' && msg.raw) {
    bodyHost.style.whiteSpace = 'pre-wrap';
    bodyHost.textContent = displayContent || '';
  } else {
    bodyHost.style.whiteSpace = 'pre-wrap';
    bodyHost.textContent = displayContent;
  }
  if (msg.role === 'assistant' && msg.web_provenance) {
    const provenance = msg.web_provenance;
    const panel = document.createElement('div');
    panel.className = 'bb-msg__provenance';
    const panelHead = document.createElement('div');
    panelHead.className = 'bb-msg__provenance-head';
    const title = document.createElement('strong');
    title.textContent = provenance.success === false ? 'Live lookup attempted' : 'Live web lookup';
    const status = document.createElement('span');
    status.textContent = provenance.success === false ? 'No usable source returned' : 'Grounded';
    panelHead.append(title, status);
    panel.appendChild(panelHead);
    const chips = document.createElement('div');
    chips.className = 'bb-msg__provenance-chips';
    const addChip = (label, value) => {
      const safeValue = String(value || '').trim();
      if (!safeValue) return;
      const chip = document.createElement('span');
      chip.textContent = `${label}: ${safeValue}`;
      chips.appendChild(chip);
    };
    addChip('Tool', provenance.tool);
    addChip('Search', provenance.search_backend);
    if (Array.isArray(provenance.fetch_backends) && provenance.fetch_backends.length) addChip('Fetch', provenance.fetch_backends.join(', '));
    if (!provenance.success && Array.isArray(provenance.attempted_tools) && provenance.attempted_tools.length) addChip('Tried', provenance.attempted_tools.join(', '));
    if (chips.childNodes.length) panel.appendChild(chips);
    const sources = Array.isArray(provenance.sources) && provenance.sources.length
      ? provenance.sources
      : (Array.isArray(provenance.source_urls) ? provenance.source_urls.map((url) => ({ url })) : []);
    if (sources.length) {
      const list = document.createElement('div');
      list.className = 'bb-msg__provenance-sources';
      sources.slice(0, 4).forEach((item) => {
        const source = document.createElement(item.url ? 'a' : 'div');
        source.className = 'bb-msg__provenance-source';
        if (item.url) {
          source.href = item.url;
          source.target = '_blank';
          source.rel = 'noreferrer noopener';
        }
        const sourceTitle = document.createElement('strong');
        try {
          sourceTitle.textContent = String(item.title || new URL(String(item.url || '')).hostname.replace(/^www\./, '') || 'source').trim();
        } catch {
          sourceTitle.textContent = String(item.title || item.url || 'source').trim();
        }
        source.appendChild(sourceTitle);
        const meta = document.createElement('span');
        const metaParts = [];
        if (item.backend) metaParts.push(String(item.backend));
        if (item.url) metaParts.push(String(item.url));
        meta.textContent = metaParts.join(' · ');
        if (meta.textContent) source.appendChild(meta);
        list.appendChild(source);
      });
      panel.appendChild(list);
    }
    el.appendChild(panel);
  }
  if (msg.role === 'assistant' && msg.raw) {
    const details = document.createElement('details');
    details.className = 'bb-msg__raw';
    const summary = document.createElement('summary');
    summary.textContent = 'Show raw planner output';
    details.appendChild(summary);
    const pre = document.createElement('pre');
    pre.textContent = msg.raw;
    details.appendChild(pre);
    el.appendChild(details);
  }
  return el;
}

function ensureRenderHosts(root) {
  if (chatMessagesRoot === root && chatHistoryHost && chatTransientHost && root.contains(chatHistoryHost) && root.contains(chatTransientHost)) return;
  chatMessagesRoot = root;
  chatHistoryHost = document.createElement('div');
  chatTransientHost = document.createElement('div');
  root.replaceChildren(chatHistoryHost, chatTransientHost);
  renderedMessagesVersion = -1;
  renderedSurfaceMode = '';
  resetTransientProgressElements();
}

function resetTransientProgressElements() {
  pendingBubbleEl = null;
  pendingBodyEl = null;
  pendingProgressEl = null;
  pendingMode = '';
  closingProgressEl = null;
}

function renderHistoryState(mode) {
  if (!chatHistoryHost || renderedSurfaceMode === mode) return;
  if (mode === 'loading') {
    const note = document.createElement('div');
    note.className = 'bb-msg bb-msg--system';
    note.textContent = 'Loading conversation…';
    chatHistoryHost.replaceChildren(note);
  } else if (mode === 'empty') {
    const empty = document.createElement('div');
    empty.className = 'bb-sidebar__empty';
    empty.innerHTML = `
      <div style="font-size:24px;margin-bottom:8px">💬</div>
      <div><strong>New conversation</strong></div>
      <div style="margin-top:6px;line-height:1.5">
        Just ask a question, or say <em>"add cards for X, Y, Z"</em> to populate the board.
      </div>
    `;
    chatHistoryHost.replaceChildren(empty);
  }
  renderedSurfaceMode = mode;
}

function renderHistoryMessages() {
  if (!chatHistoryHost) return;
  if (renderedSurfaceMode === 'messages' && renderedMessagesVersion === messagesVersion) return;
  const fragment = document.createDocumentFragment();
  messages.forEach((msg, i) => fragment.appendChild(messageBubble(msg, i)));
  chatHistoryHost.replaceChildren(fragment);
  renderedMessagesVersion = messagesVersion;
  renderedSurfaceMode = 'messages';
}

function checkpointElement(payload) {
  const card = payload.card || {};
  const pendingQuestions = card.metadata?.pending_questions || {};
  const wrap = document.createElement('div');
  wrap.className = 'bb-chat-checkpoint';
  const title = document.createElement('strong');
  title.textContent = `Checkpoint: ${card.title || card.id || 'card'}`;
  wrap.appendChild(title);
  const reason = document.createElement('div');
  reason.textContent = pendingQuestions.reason || 'The agent needs a decision before continuing.';
  wrap.appendChild(reason);
  for (const question of pendingQuestions.questions || []) {
    const q = document.createElement('div');
    q.className = 'bb-chat-checkpoint__question';
    const prompt = document.createElement('div');
    prompt.textContent = question.prompt || 'Choose an option';
    q.appendChild(prompt);
    for (const option of question.options || []) {
      const btn = document.createElement('button');
      btn.className = 'bb-btn';
      btn.type = 'button';
      btn.textContent = option.label || option.value || option.id;
      btn.addEventListener('click', () => answerCheckpointFromChat(card, question, option));
      q.appendChild(btn);
    }
    wrap.appendChild(q);
  }
  return wrap;
}

function createProgressElement() {
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <div class="bb-chat-progress__top">
      <strong class="bb-chat-progress__title"></strong>
      <span class="bb-chat-progress__phase"></span>
    </div>
    <div class="bb-chat-progress__detail"></div>
    <div class="bb-chat-progress__stack">
      <div class="bb-chat-progress__row">
        <div class="bb-chat-progress__head">
          <span>Request progress</span>
          <strong></strong>
        </div>
        <div class="bb-chat-progress__bar bb-chat-progress__bar--overall" aria-label="Overall request progress" aria-valuemin="0" aria-valuemax="100">
          <span></span>
        </div>
      </div>
      <div class="bb-chat-progress__row">
        <div class="bb-chat-progress__head">
          <span>Current progress</span>
          <strong></strong>
        </div>
        <div class="bb-chat-progress__bar bb-chat-progress__bar--current" aria-label="Current phase progress" aria-valuemin="0" aria-valuemax="100">
          <span></span>
        </div>
      </div>
    </div>
    <div class="bb-chat-progress__meta">
      <span></span>
      <span></span>
      <span></span>
      <span></span>
    </div>
  `;
  wrap._titleEl = wrap.querySelector('.bb-chat-progress__top strong');
  wrap._phaseEl = wrap.querySelector('.bb-chat-progress__phase');
  wrap._detailEl = wrap.querySelector('.bb-chat-progress__detail');
  wrap._barEls = Array.from(wrap.querySelectorAll('.bb-chat-progress__bar'));
  wrap._fillEls = Array.from(wrap.querySelectorAll('.bb-chat-progress__bar span'));
  wrap._rowValueEls = Array.from(wrap.querySelectorAll('.bb-chat-progress__head strong'));
  wrap._metaEls = Array.from(wrap.querySelectorAll('.bb-chat-progress__meta span'));
  return wrap;
}

function updateProgressElement(wrap, progress = {}) {
  if (!wrap) return wrap;
  const estimate = progressEstimate(progress);
  const current = currentPhaseProgress(estimate);
  const phase = estimate.phase;
  const width = estimate.visualPercent;
  const phaseLabel = phase === 'bleeping' ? 'Replying' : 'Thinking';
  const titleText = phase === 'bleeping' ? 'Generating response' : 'Analyzing request';
  const defaultDetail = phase === 'bleeping' ? 'Streaming the assistant reply.' : 'Reasoning through the request.';
  const detailParts = [String(progress.detail || defaultDetail).trim()];
  if (estimate.sampleCount > 0) detailParts.push(`Based on ${estimate.sampleCount} learned run${estimate.sampleCount === 1 ? '' : 's'}`);
  wrap.className = `bb-chat-progress bb-chat-progress--phase-${phase}${progress.closing ? ' bb-chat-progress--closing' : ''}`;
  if (wrap._titleEl) wrap._titleEl.textContent = titleText;
  if (wrap._phaseEl) wrap._phaseEl.textContent = phaseLabel;
  if (wrap._detailEl) wrap._detailEl.textContent = detailParts.filter(Boolean).join(' · ');
  if (Array.isArray(wrap._barEls) && wrap._barEls.length >= 2) {
    wrap._barEls[0].setAttribute('aria-valuenow', String(Math.round(width)));
    wrap._barEls[1].setAttribute('aria-valuenow', String(Math.round(current.percent)));
  }
  if (Array.isArray(wrap._fillEls) && wrap._fillEls.length >= 2) {
    wrap._fillEls[0].style.width = `${width}%`;
    wrap._fillEls[1].style.width = `${current.percent}%`;
  }
  if (Array.isArray(wrap._rowValueEls) && wrap._rowValueEls.length >= 2) {
    wrap._rowValueEls[0].textContent = `${Math.round(width)}%`;
    wrap._rowValueEls[1].textContent = `${current.label} · ${Math.round(current.percent)}%`;
  }
  if (Array.isArray(wrap._metaEls) && wrap._metaEls.length >= 4) {
    wrap._metaEls[0].textContent = `session turns ${String(estimate.turns || '—')}`;
    wrap._metaEls[1].textContent = `thoughts ${String(estimate.thinkingTurns)}`;
    wrap._metaEls[2].textContent = `reply chunks ${String(estimate.replyChunks)}`;
    wrap._metaEls[3].textContent = estimate.remainingLabel || (progress.remaining || 'estimating duration');
  }
  return wrap;
}

function progressElement(progress = {}) {
  return updateProgressElement(createProgressElement(), progress);
}

function ensurePendingBubble() {
  if (pendingBubbleEl && pendingBodyEl && pendingProgressEl) return pendingBubbleEl;
  const el = document.createElement('div');
  el.className = 'bb-msg bb-msg--assistant bb-msg--pending';
  const head = document.createElement('div');
  head.className = 'bb-msg__head';
  const role = document.createElement('span');
  role.className = 'bb-msg__role';
  role.textContent = 'Provider';
  head.appendChild(role);
  const body = document.createElement('div');
  body.className = 'bb-msg__body bb-msg__thinking';
  const progress = createProgressElement();
  el.append(head, body, progress);
  pendingBubbleEl = el;
  pendingBodyEl = body;
  pendingProgressEl = progress;
  pendingMode = '';
  return pendingBubbleEl;
}

function renderPendingBody() {
  if (!pendingBodyEl) return;
  if (streamingReplyText) {
    if (pendingMode !== 'streaming') {
      pendingBodyEl.className = 'bb-msg__body';
      pendingBodyEl.replaceChildren();
      pendingBodyEl.style.whiteSpace = 'pre-wrap';
      pendingMode = 'streaming';
    }
    pendingBodyEl.textContent = streamingReplyText;
    return;
  }
  if (pendingMode === 'thinking') return;
  pendingBodyEl.className = 'bb-msg__body bb-msg__thinking';
  pendingBodyEl.style.whiteSpace = '';
  pendingBodyEl.replaceChildren(document.createElement('span'), document.createElement('span'), document.createElement('span'));
  pendingMode = 'thinking';
}

function showPendingBubble(progress = {}) {
  if (!chatTransientHost) return;
  const bubble = ensurePendingBubble();
  renderPendingBody();
  updateProgressElement(pendingProgressEl, progress);
  if (chatTransientHost.firstChild !== bubble || chatTransientHost.childNodes.length !== 1) {
    chatTransientHost.replaceChildren(bubble);
  }
}

function showClosingProgress(progress = {}) {
  if (!chatTransientHost || !progress || !Object.keys(progress).length) {
    if (chatTransientHost?.childNodes.length) chatTransientHost.replaceChildren();
    return;
  }
  if (!closingProgressEl) closingProgressEl = createProgressElement();
  updateProgressElement(closingProgressEl, progress);
  if (chatTransientHost.firstChild !== closingProgressEl || chatTransientHost.childNodes.length !== 1) {
    chatTransientHost.replaceChildren(closingProgressEl);
  }
}

function clearClosingProgress() {
  stopProgressAnimation();
  if (closingProgressTimer) {
    clearTimeout(closingProgressTimer);
    closingProgressTimer = null;
  }
  closingProgress = null;
}

function beginClosingProgress(progress = {}) {
  const next = { ...(progress || {}) };
  if (!Object.keys(next).length) return;
  stopProgressAnimation();
  if (closingProgressTimer) {
    clearTimeout(closingProgressTimer);
    closingProgressTimer = null;
  }
  closingProgress = { ...next, estimate_percent: 100, remaining: 'complete', closing: false };
  requestAnimationFrame(() => {
    if (!closingProgress) return;
    closingProgress = { ...closingProgress, closing: true };
    schedulePaint();
  });
  closingProgressTimer = setTimeout(() => {
    closingProgress = null;
    closingProgressTimer = null;
    schedulePaint(true);
  }, PROGRESS_FADE_MS + 40);
}

function paintMessages() {
  const root = document.getElementById('chat-messages');
  if (!root) return;
  ensureRenderHosts(root);
  const shouldStickBottom = forceBottomNextPaint || (isNearBottom(root) && shouldAutoFollowNextPaint);
  shouldAutoFollowNextPaint = false;
  forceBottomNextPaint = false;
  lastPaintAt = performance.now();
  if (loading) {
    renderHistoryState('loading');
    if (chatTransientHost.childNodes.length) chatTransientHost.replaceChildren();
    pendingMode = '';
    return;
  }
  if (!messages.length && !pending) {
    renderHistoryState('empty');
    if (chatTransientHost.childNodes.length) chatTransientHost.replaceChildren();
    pendingMode = '';
    return;
  }
  renderHistoryMessages();
  if (pending) {
    showPendingBubble(activeProgress || {});
  } else if (closingProgress) {
    showClosingProgress(closingProgress);
  } else {
    if (chatTransientHost.childNodes.length) chatTransientHost.replaceChildren();
  }
  requestAnimationFrame(() => {
    if (!shouldStickBottom && restoreScroll(root)) return;
    settleScroll(root, shouldStickBottom);
  });
}

function schedulePaint(immediate = false) {
  if (immediate) {
    if (paintTimer) {
      clearTimeout(paintTimer);
      paintTimer = null;
    }
    if (paintRaf) {
      cancelAnimationFrame(paintRaf);
      paintRaf = null;
    }
    paintMessages();
    return;
  }
  if (paintTimer || paintRaf) return;
  const elapsed = performance.now() - lastPaintAt;
  const wait = Math.max(0, PAINT_THROTTLE_MS - elapsed);
  paintTimer = setTimeout(() => {
    paintTimer = null;
    paintRaf = requestAnimationFrame(() => {
      paintRaf = null;
      paintMessages();
    });
  }, wait);
}

function updateSessionLabel() {
  const label = document.getElementById('chat-session-label');
  if (!label) return;
  if (!currentSessionId) { label.textContent = ''; return; }
  label.textContent = currentSessionId.slice(0, 14);
  label.title = `Session: ${currentSessionId}`;
}

// ── Session lifecycle ───────────────────────────────────────────

async function hydrateFromServer() {
  if (!currentProjectId || !currentSessionId) {
    replaceMessages([]);
    paintMessages();
    return;
  }
  loading = true;
  paintMessages();
  try {
    const data = await api.chatHistory(currentProjectId, currentSessionId, 200);
    const list = Array.isArray(data?.history) ? data.history : [];
    replaceMessages(list.map((m) => ({
      id: m.id,
      role: m.role || 'system',
      content: m.content || '',
      ts: m.ts || 0,
      raw: m.metadata?.raw ? String(m.metadata.raw).slice(0, 4000) : '',
      images: m.metadata?.images || [],
      web_provenance: m.metadata?.web_provenance || null,
      provider_id: m.metadata?.provider_id || '',
      provider_model: m.metadata?.provider_model || '',
    })));
  } catch (err) {
    console.warn('[chat] history load failed:', err);
    replaceMessages([{ role: 'system', content: `Couldn't load history: ${err.message}` }]);
  } finally {
    loading = false;
    paintMessages();
    updateSessionLabel();
  }
}

async function switchTo(projectId, sessionId) {
  persistCurrentScroll();
  currentProjectId = projectId || '';
  currentSessionId = sessionId || '';
  if (currentProjectId && currentSessionId) writePersistedSession(currentProjectId, currentSessionId);
  pendingScrollRestore = readPersistedScroll(currentProjectId, currentSessionId) || { top: 0, atBottom: true };
  await hydrateFromServer();
}

async function resolveSession(projectId) {
  if (!projectId) return '';
  const persisted = readPersistedSession(projectId);
  try {
    const { sessions } = await api.chatSessions(projectId);
    const list = Array.isArray(sessions) ? sessions : [];
    if (persisted && list.some((s) => s.session_id === persisted)) return persisted;
    const recent = list.find((s) => s.message_count > 0);
    if (recent) return recent.session_id;
  } catch (err) {
    console.warn('[chat] session list failed; falling back to persisted:', err);
    if (persisted) return persisted;
  }
  return newSessionId();
}

async function startNewSession() {
  if (!currentProjectId) return;
  const sid = newSessionId();
  await switchTo(currentProjectId, sid);
}

async function clearCurrentSession() {
  if (!currentProjectId || !currentSessionId) return;
  const ok = await confirmDialog({
    title: 'Clear conversation?',
    message: `This deletes all messages in session ${currentSessionId.slice(0, 14)} for this project. The session id stays the same.`,
    confirmLabel: 'Clear',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.chatClearHistory(currentProjectId, currentSessionId);
    replaceMessages([]);
    paintMessages();
    toast('Conversation cleared', { kind: 'success', timeout: 1200 });
  } catch (err) {
    toast(`Clear failed: ${err.message}`, { kind: 'error' });
  }
}

// ── Submit handler ──────────────────────────────────────────────

async function submit() {
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  if (!input || !send || pending || awaitingHttpClientMessageId) return;
  const text = input.value.trim();
  if (!text) return;
  if (!currentProjectId) {
    toast('Pick or create a project first.', { kind: 'warn' });
    return;
  }
  if (!currentSessionId) currentSessionId = newSessionId();
  const clientMessageId = newClientMessageId();
  activeClientMessageId = clientMessageId;
  wsRenderedClientMessageId = '';
  awaitingHttpClientMessageId = clientMessageId;
  streamingReplyText = '';
  clearClosingProgress();
  resetTransientProgressElements();
  beginProgressTracking();
  activeProgress = {
    phase: 'thinking',
    detail: 'Thinking through request...',
    elapsed_seconds: 0,
    thinking_turns: 0,
    reply_chunks: 0,
    remaining: 'ETA learning estimate',
    estimate_percent: progressVisualPercent,
    indeterminate: false,
  };
  writePersistedSession(currentProjectId, currentSessionId);

  // Optimistic user bubble + thinking placeholder.
  appendMessage({ role: 'user', content: text, client_message_id: clientMessageId });
  pending = true;
  ensureProgressAnimation();
  shouldAutoFollowNextPaint = true;
  forceBottomNextPaint = true;
  schedulePaint(true);
  input.value = '';
  resizeInput();
  send.disabled = true;

  try {
    const body = {
      project_id: currentProjectId,
      session_id: currentSessionId,
      message: text,
      client_message_id: clientMessageId,
    };
    let result;
    try {
      result = api.chatStream ? await api.chatStream(body) : await api.chat(body);
    } catch (streamErr) {
      if (wsRenderedClientMessageId === clientMessageId || streamingReplyText) throw streamErr;
      result = await api.chat(body);
    }

    const reply = result.reply || result.warning || '(empty reply)';
    const created = result.cards || [];
    const deleted = result.deleted_cards || [];
    if (wsRenderedClientMessageId !== clientMessageId) {
      appendMessage({
        role: result.warning ? 'system' : 'assistant',
        content: reply,
        raw: result.raw ? String(result.raw).slice(0, 4000) : '',
        images: result.images || [],
        web_provenance: result.web_provenance || null,
        provider_id: result.provider_id || '',
        provider_model: result.provider_model || '',
      });
      shouldAutoFollowNextPaint = true;
    }

    if (created.length || deleted.length) {
      try {
        const board = await api.board(currentProjectId);
        store.setBoard(board);
      } catch { /* board refresh is best-effort */ }
    }
  } catch (err) {
    if (wsRenderedClientMessageId !== clientMessageId) {
      appendMessage({ role: 'system', content: `Error: ${err.message}` });
      shouldAutoFollowNextPaint = true;
    }
  } finally {
    if (activeProgress) recordProgressCompletion(activeProgress);
    beginClosingProgress(activeProgress || {});
    pending = false;
    if (activeClientMessageId === clientMessageId) activeClientMessageId = '';
    if (awaitingHttpClientMessageId === clientMessageId) awaitingHttpClientMessageId = '';
    streamingReplyText = '';
    activeProgress = null;
    schedulePaint(true);
    updateSessionLabel();
    send.disabled = false;
    input.focus();
  }
}

// ── Auto-grow textarea ─────────────────────────────────────────

function resizeInput() {
  const input = document.getElementById('chat-input');
  if (!input) return;
  input.style.height = 'auto';
  const computed = window.getComputedStyle(input);
  const lineHeight = Math.max(16, Number.parseFloat(computed.lineHeight) || 22);
  const paddingTop = Number.parseFloat(computed.paddingTop) || 0;
  const paddingBottom = Number.parseFloat(computed.paddingBottom) || 0;
  const borderTop = Number.parseFloat(computed.borderTopWidth) || 0;
  const borderBottom = Number.parseFloat(computed.borderBottomWidth) || 0;
  const chrome = paddingTop + paddingBottom + borderTop + borderBottom;
  const mobile = window.matchMedia('(max-width: 900px)').matches;
  const minLines = mobile ? 4 : 3;
  const maxLines = mobile ? 8 : 12;
  const minHeight = Math.round((lineHeight * minLines) + chrome);
  const maxHeight = Math.round((lineHeight * maxLines) + chrome);
  const next = Math.min(maxHeight, input.scrollHeight + 2);
  input.style.height = `${Math.max(minHeight, next)}px`;
  input.style.overflowY = input.scrollHeight > maxHeight ? 'auto' : 'hidden';
}

function matchesActiveChat(payload, options = {}) {
  const allowWebsocketFallback = Boolean(options?.allowWebsocketFallback);
  if (awaitingHttpClientMessageId && payload?.client_message_id === awaitingHttpClientMessageId && !payload.__stream_local && !allowWebsocketFallback) return false;
  return (
    payload &&
    payload.project_id === currentProjectId &&
    payload.session_id === currentSessionId &&
    payload.client_message_id &&
    payload.client_message_id === activeClientMessageId
  );
}

function applyChatDone(payload) {
  if (!matchesActiveChat(payload, { allowWebsocketFallback: true })) return;
  if (wsRenderedClientMessageId === payload.client_message_id) return;
  wsRenderedClientMessageId = payload.client_message_id;
  recordProgressCompletion(activeProgress || payload);
  const streamedText = streamingReplyText.trim();
  beginClosingProgress({ ...(activeProgress || {}), ...payload, phase: streamedText ? 'bleeping' : ((activeProgress || {}).phase || 'thinking') });
  streamingReplyText = '';
  const reply = payload.reply || payload.warning || streamedText || '(empty reply)';
  appendMessage({
    role: payload.warning ? 'system' : 'assistant',
    content: reply,
    raw: payload.raw ? String(payload.raw).slice(0, 4000) : '',
    images: payload.images || [],
    web_provenance: payload.web_provenance || null,
    provider_id: payload.provider_id || '',
    provider_model: payload.provider_model || '',
  });
  shouldAutoFollowNextPaint = true;
  const created = Array.isArray(payload.cards) ? payload.cards : [];
  const deleted = Array.isArray(payload.deleted_cards) ? payload.deleted_cards : [];
  if ((created.length || deleted.length) && currentProjectId) {
    api.board(currentProjectId).then((board) => store.setBoard(board)).catch(() => {});
  }
  pending = false;
  activeClientMessageId = '';
  activeProgress = null;
  schedulePaint(true);
  updateSessionLabel();
}

function applyChatError(payload) {
  if (!matchesActiveChat(payload, { allowWebsocketFallback: true })) return;
  if (wsRenderedClientMessageId === payload.client_message_id) return;
  wsRenderedClientMessageId = payload.client_message_id;
  recordProgressCompletion(activeProgress || payload);
  beginClosingProgress({ ...(activeProgress || {}), ...payload, phase: 'thinking' });
  streamingReplyText = '';
  appendMessage({ role: 'system', content: `Error: ${payload.error || 'chat failed'}` });
  shouldAutoFollowNextPaint = true;
  pending = false;
  activeClientMessageId = '';
  activeProgress = null;
  schedulePaint(true);
  updateSessionLabel();
}

function applyChatToken(payload) {
  if (!matchesActiveChat(payload)) return;
  const token = payload.token || '';
  if (!token) return;
  if (!progressFirstReplyAt) progressFirstReplyAt = performance.now();
  streamingReplyText += token;
  shouldAutoFollowNextPaint = true;
  activeProgress = { ...(activeProgress || {}), ...payload, phase: 'bleeping', indeterminate: false };
  pending = true;
  ensureProgressAnimation();
  schedulePaint();
}

function applyChatProgress(payload) {
  if (!matchesActiveChat(payload, { allowWebsocketFallback: true })) return;
  activeProgress = { ...(activeProgress || {}), ...payload, indeterminate: false };
  pending = true;
  ensureProgressAnimation();
  schedulePaint();
}

function applyChatThinking(payload) {
  if (!matchesActiveChat(payload, { allowWebsocketFallback: true })) return;
  activeProgress = { ...(activeProgress || {}), ...payload, phase: 'thinking', indeterminate: false };
  pending = true;
  ensureProgressAnimation();
  schedulePaint();
}

function hasPendingQuestions(card) {
  const pendingQuestions = card?.metadata?.pending_questions;
  return Boolean(pendingQuestions && Array.isArray(pendingQuestions.questions) && pendingQuestions.questions.length);
}

function appendCheckpointMessages(snapshot) {
  const cardsByColumn = snapshot?.cards_by_column || {};
  const cards = Object.values(cardsByColumn).flat();
  for (const card of cards) {
    if (!hasPendingQuestions(card)) continue;
    const key = `checkpoint:${card.id}:${card.metadata.pending_questions.reason || ''}`;
    if (messages.some((m) => m.id === key)) continue;
    appendMessage({
      id: key,
      role: 'assistant',
      content: '',
      checkpoint: { card },
    });
    shouldAutoFollowNextPaint = true;
  }
  paintMessages();
}

async function answerCheckpointFromChat(card, question, option) {
  if (!currentProjectId || !card?.id) return;
  const metadata = { ...(card.metadata || {}) };
  const answers = Array.isArray(metadata.checkpoint_answers) ? [...metadata.checkpoint_answers] : [];
  answers.push({
    question_id: question.id,
    prompt: question.prompt || '',
    option_id: option.id,
    label: option.label || '',
    value: option.value || option.label || '',
    answered_at: Date.now() / 1000,
  });
  metadata.checkpoint_answers = answers;
  delete metadata.pending_questions;
  metadata.last_checkpoint_answer = answers[answers.length - 1];
  metadata.resume_from = {
    status: 'ready',
    previous_status: card.status,
    reason: 'checkpoint answered by user',
    moved_at: Date.now() / 1000,
  };
  try {
    await api.updateCard(currentProjectId, card.id, { metadata, status: 'ready', progress: Math.max(0, Number(card.progress || 0) - 10) });
    const board = await api.board(currentProjectId);
    store.setBoard(board);
    replaceMessages(messages.filter((msg) => !(msg.checkpoint?.card?.id === card.id)));
    toast('Checkpoint answered. Card moved back to ready.', { kind: 'success' });
    paintMessages();
  } catch (err) {
    toast(`Checkpoint answer failed: ${err.message}`, { kind: 'error' });
  }
}

// ── Public entry point ──────────────────────────────────────────

export function renderSidebar() {
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  const voiceBtn = document.getElementById('chat-voice');
  const wakeBtn = document.getElementById('chat-wake');
  if (!input || !send) return;
  const root = document.getElementById('chat-messages');
  if (root && root.dataset.scrollBound !== '1') {
    root.dataset.scrollBound = '1';
    root.addEventListener('scroll', () => {
      persistCurrentScroll(root);
    }, { passive: true });
  }

  // Inject session-control buttons into the existing header (idempotent).
  const header = document.querySelector('.bb-sidebar__header');
  if (header && !header.querySelector('.bb-sidebar__session-controls')) {
    const controls = document.createElement('span');
    controls.className = 'bb-sidebar__session-controls';
    controls.innerHTML = `
      <span class="bb-sidebar__session-id" id="chat-session-label" title=""></span>
      <button class="bb-sidebar__icon bb-sidebar__icon--new" id="chat-new" title="Start a new conversation" aria-label="New conversation"><span class="bb-icon-plus" aria-hidden="true"></span></button>
      <button class="bb-sidebar__icon bb-sidebar__icon--clear" id="chat-clear" title="Clear this conversation's history" aria-label="Clear conversation"><span class="bb-icon-erase" aria-hidden="true"></span></button>
    `;
    header.appendChild(controls);
    document.getElementById('chat-new').addEventListener('click', startNewSession);
    document.getElementById('chat-clear').addEventListener('click', clearCurrentSession);
  }

  send.addEventListener('click', submit);
  if (voiceBtn && voiceBtn.dataset.bound !== '1') {
    voiceBtn.dataset.bound = '1';
    voiceBtn.addEventListener('click', () => startSpeechRecognition('voice'));
  }
  if (wakeBtn && wakeBtn.dataset.bound !== '1') {
    wakeBtn.dataset.bound = '1';
    wakeBtn.addEventListener('click', () => startSpeechRecognition('wake'));
  }
  updateVoiceControls();

  // Plain Enter sends; Shift+Enter inserts a newline. Ctrl/Cmd+Enter still
  // works as an alias because muscle memory. IME composition is respected so
  // Enter while composing CJK input doesn't accidentally submit.
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.isComposing || e.keyCode === 229) return;
    if (e.shiftKey) return;            // newline
    e.preventDefault();
    submit();
  });
  input.addEventListener('input', resizeInput);
  // Initial size in case there's pre-fill from autofill or browser restore.
  resizeInput();
  // Update placeholder hint with the new key affordances.
  input.placeholder = 'Ask anything · "add cards for …" to populate the board · Enter to send · Shift+Enter for newline';

  // Hydrate when the active project changes — including the very first time
  // it gets set during boot.
  bus.on('store:active', async (projectId) => {
    if (!projectId) {
      currentProjectId = '';
      currentSessionId = '';
      replaceMessages([]);
      paintMessages();
      updateSessionLabel();
      return;
    }
    if (projectId === currentProjectId) return;
    const sid = await resolveSession(projectId);
    await switchTo(projectId, sid);
  });

  bus.on('ws:chat.done', applyChatDone);
  bus.on('ws:chat.error', applyChatError);
  bus.on('ws:chat.token', applyChatToken);
  bus.on('ws:chat.progress', applyChatProgress);
  bus.on('ws:chat.thinking', applyChatThinking);
  bus.on('store:board', appendCheckpointMessages);

  // If the store already had an active project before we wired the listener
  // (it does during normal boot), initialize from it now.
  const initialPid = store.get().activeProjectId;
  if (initialPid) {
    resolveSession(initialPid).then((sid) => switchTo(initialPid, sid));
  }
}
