import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { toast } from '/ui/shell/dialog.js';

const MAP_CARD_LIMIT = 8;
const INDENT_GUIDE_SPACES = 2;
const EXEC_ACTIVITY_LIMIT = 40;
const EXEC_TIMELINE_LIMIT = 8;
const EXEC_TARGET_FILE_LIMIT = 12;

const state = {
  view: 'board',
  studioTab: 'preview',
  studioSidebarTab: 'artifacts',
  studioMobilePane: 'browser',
  artifacts: [],
  artifactSort: 'updated_desc',
  artifactFiles: [],
  currentFilePath: '',
  currentContent: '',
  lastSavedContent: '',
  currentArtifact: null,
  executionLive: {
    jobs: {},
    feed: [],
  },
};

function escapeHtml(text) {
  return String(text || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function activeProject() {
  const { activeProjectId, projects } = store.get();
  return (projects || []).find((project) => project.project_id === activeProjectId) || null;
}

function workspaceShell() {
  return document.getElementById('workspace-shell');
}

function workspaceButtons() {
  return Array.from(document.querySelectorAll('[data-workspace-switch]'));
}

function workspaceViews() {
  return Array.from(document.querySelectorAll('.bb-workspace__surface > [data-workspace-view]'));
}

function isCompactStudioViewport() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function studioRootEl() {
  return document.querySelector('.bb-studio');
}

function studioTitleEl() {
  return document.getElementById('studio-title');
}

function studioSubtitleEl() {
  return document.getElementById('studio-subtitle');
}

function studioPathEl() {
  return document.getElementById('studio-doc-path');
}

function studioCodeEl() {
  return document.getElementById('studio-code-editor');
}

function studioFrameEl() {
  return document.getElementById('studio-preview-frame');
}

function studioArtifactListEl() {
  return document.getElementById('studio-artifact-list');
}

function studioArtifactCountEl() {
  return document.getElementById('studio-artifact-count');
}

function studioArtifactSortEl() {
  return document.getElementById('studio-artifact-sort');
}

function studioFileListEl() {
  return document.getElementById('studio-file-list');
}

function studioFileCountEl() {
  return document.getElementById('studio-file-count');
}

function studioCodeHighlightEl() {
  return document.getElementById('studio-code-highlight');
}

function studioCodeGutterEl() {
  return document.getElementById('studio-code-gutter');
}

function studioCodeLanguageEl() {
  return document.getElementById('studio-code-language');
}

function studioCodeLinesEl() {
  return document.getElementById('studio-code-lines');
}

function studioCodeCursorEl() {
  return document.getElementById('studio-code-cursor');
}

function studioCodeSelectionEl() {
  return document.getElementById('studio-code-selection');
}

function studioPreviewTitleEl() {
  return document.getElementById('studio-preview-title');
}

function studioPreviewMetaEl() {
  return document.getElementById('studio-preview-meta');
}

function studioPreviewBadgeEl() {
  return document.getElementById('studio-preview-badge');
}

function artifactDisplayTitle(artifact) {
  return String(artifact?.display_title || artifact?.title || artifact?.artifact_id || 'Artifact');
}

function currentDocLabel() {
  if (state.currentArtifact) {
    const suffix = state.currentFilePath || state.currentArtifact.artifact_id || 'artifact';
    return `${artifactDisplayTitle(state.currentArtifact)} · ${suffix}`;
  }
  return 'No artifact selected';
}

function truncateText(value, max = 240) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function normalizeExecPath(value) {
  return String(value || '').trim().replace(/\\/g, '/');
}

function relativeTimeLabel(value) {
  const stamp = Number(value || 0) || 0;
  if (!stamp) return 'just now';
  const delta = Math.max(0, Date.now() - stamp);
  if (delta < 5_000) return 'just now';
  if (delta < 60_000) return `${Math.floor(delta / 1000)}s ago`;
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  return `${Math.floor(delta / 3_600_000)}h ago`;
}

function activeProjectId() {
  return String(store.get().activeProjectId || '').trim();
}

function matchesExecFile(left, right) {
  const a = normalizeExecPath(left).toLowerCase();
  const b = normalizeExecPath(right).toLowerCase();
  if (!a || !b) return false;
  return a === b || a.endsWith(`/${b}`) || b.endsWith(`/${a}`);
}

function ensureExecutionJob(jobId) {
  const key = String(jobId || '').trim();
  if (!key) return null;
  if (!state.executionLive.jobs[key]) {
    state.executionLive.jobs[key] = {
      updatedAt: 0,
      currentFile: '',
      note: '',
      previewText: '',
      kind: '',
      attempt: 0,
      files: [],
      provider: '',
      timeline: [],
    };
  }
  return state.executionLive.jobs[key];
}

function pushExecutionTimeline(target, entry) {
  if (!target || !entry?.text) return;
  const timeline = Array.isArray(target.timeline) ? target.timeline : [];
  const next = {
    at: Number(entry.at || Date.now()) || Date.now(),
    label: String(entry.label || 'step'),
    text: truncateText(entry.text, 220),
    file: normalizeExecPath(entry.file || ''),
  };
  const previous = timeline[0];
  if (previous && previous.label === next.label && previous.text === next.text && previous.file === next.file) {
    previous.at = next.at;
    target.timeline = timeline;
    return;
  }
  timeline.unshift(next);
  if (timeline.length > EXEC_TIMELINE_LIMIT) timeline.length = EXEC_TIMELINE_LIMIT;
  target.timeline = timeline;
}

function pushExecutionFeed(entry) {
  if (!entry?.text) return;
  const feed = Array.isArray(state.executionLive.feed) ? state.executionLive.feed : [];
  const next = {
    at: Number(entry.at || Date.now()) || Date.now(),
    label: String(entry.label || 'event'),
    text: truncateText(entry.text, 280),
    kind: String(entry.kind || 'event'),
  };
  const previous = feed[0];
  if (previous && previous.label === next.label && previous.text === next.text && previous.kind === next.kind) {
    previous.at = next.at;
    state.executionLive.feed = feed;
    return;
  }
  feed.unshift(next);
  if (feed.length > EXEC_ACTIVITY_LIMIT) feed.length = EXEC_ACTIVITY_LIMIT;
  state.executionLive.feed = feed;
}

function syncExecutionProgress(payload = {}) {
  const activeId = activeProjectId();
  const projectId = String(payload.project_id || '').trim();
  if (activeId && projectId && activeId !== projectId) return;
  const record = ensureExecutionJob(payload.job_id);
  if (!record) return;
  record.updatedAt = Date.now();
  record.currentFile = normalizeExecPath(payload.current_file || record.currentFile);
  record.note = String(payload.note || record.note || '').trim();
  record.previewText = String(payload.preview_text || record.previewText || '').trim();
  record.kind = String(payload.kind || record.kind || '').trim();
  record.attempt = Math.max(0, Number(payload.attempt || record.attempt || 0) || 0);
  if (Array.isArray(payload.files) && payload.files.length) {
    record.files = payload.files.map((item) => normalizeExecPath(item)).filter(Boolean).slice(0, EXEC_TARGET_FILE_LIMIT);
  }
  if (record.note || record.currentFile) {
    pushExecutionTimeline(record, {
      at: record.updatedAt,
      label: record.kind || 'progress',
      text: record.note || `focused ${record.currentFile}`,
      file: record.currentFile,
    });
    pushExecutionFeed({
      at: record.updatedAt,
      label: payload.job_id || 'job',
      kind: 'progress',
      text: `${record.currentFile || 'job'} · ${record.note || 'working'}`,
    });
  }
}

function syncExecutionLifecycle(topic, payload = {}) {
  const record = ensureExecutionJob(payload.job_id);
  if (!record) return;
  const status = String(payload.status || topic.split('.').pop() || 'job').trim();
  const file = normalizeExecPath(payload.current_file || record.currentFile);
  const text = String(
    payload.reason
    || payload.error
    || payload.summary
    || (status === 'started' ? `started in ${payload.cwd || 'workspace'}` : status)
  ).trim();
  record.updatedAt = Date.now();
  if (file) record.currentFile = file;
  if (text) record.note = text;
  pushExecutionTimeline(record, {
    at: record.updatedAt,
    label: status,
    text: text || status,
    file,
  });
  pushExecutionFeed({
    at: record.updatedAt,
    label: payload.job_id || status,
    kind: ['failed', 'paused'].includes(status) ? 'warn' : 'event',
    text: `${status} · ${text || payload.job_id || 'job update'}`,
  });
}

function syncExecutionTranscript(payload = {}) {
  const text = String(payload.text || '').replace(/\s+/g, ' ').trim();
  if (!text) return;
  pushExecutionFeed({
    at: Date.now(),
    label: `${payload.provider || 'cli'}:${payload.kind || 'stream'}`,
    kind: 'cli',
    text,
  });
}

function executionFilesForJob(job, live) {
  const files = [];
  for (const item of [...(job?.task?.files || []), ...(live?.files || []), live?.currentFile || '']) {
    const normalized = normalizeExecPath(item);
    if (!normalized) continue;
    if (files.some((existing) => matchesExecFile(existing, normalized))) continue;
    files.push(normalized);
    if (files.length >= EXEC_TARGET_FILE_LIMIT) break;
  }
  return files;
}

function setStudioMobilePane(pane = 'browser') {
  state.studioMobilePane = pane === 'viewer' ? 'viewer' : 'browser';
  renderStudioMobilePane();
}

function renderStudioMobilePane() {
  const root = studioRootEl();
  const compact = isCompactStudioViewport();
  if (root) {
    root.dataset.mobileLayout = compact ? '1' : '0';
    root.dataset.mobilePane = compact ? state.studioMobilePane : 'viewer';
  }
  const back = document.getElementById('studio-mobile-back');
  if (back) back.hidden = !(compact && state.studioMobilePane === 'viewer');
}

function isDirty() {
  return state.currentContent !== state.lastSavedContent;
}

function extname(path) {
  const value = String(path || '');
  const idx = value.lastIndexOf('.');
  return idx === -1 ? '' : value.slice(idx + 1).toLowerCase();
}

function normalizeArtifactType(value) {
  const kind = String(value || '').trim().toLowerCase();
  return kind || 'text';
}

async function reusableArtifactFor(projectId, payload = {}) {
  if (!projectId) return null;
  const explicitId = String(payload.artifactId || '').trim();
  if (explicitId) {
    return { artifact_id: explicitId };
  }
  const resolved = await api.artifactResolve(projectId, {
    title: String(payload.title || ''),
    source: String(payload.source || ''),
    type: normalizeArtifactType(payload.type || 'html'),
  });
  return resolved?.artifact || null;
}

function labelForArtifactType(value) {
  const kind = normalizeArtifactType(value);
  return {
    html: 'HTML',
    markdown: 'Markdown',
    json: 'JSON',
    javascript: 'JavaScript',
    css: 'CSS',
    'line-chart': 'Line Chart',
    'bar-chart': 'Bar Chart',
    table: 'Table',
    text: 'Text',
  }[kind] || kind.replace(/[-_]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
}

function currentSourcePath() {
  return state.currentFilePath || state.currentArtifact?.entry_file || state.currentArtifact?.source_path || '';
}

function detectLanguage(content, path = '', artifactType = '') {
  const kind = normalizeArtifactType(artifactType);
  if (kind === 'html') return 'html';
  if (kind === 'markdown') return 'markdown';
  if (kind === 'json' || kind === 'line-chart' || kind === 'bar-chart' || kind === 'table') return 'json';
  if (kind === 'javascript') return 'javascript';
  if (kind === 'css') return 'css';
  const ext = extname(path);
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'js' || ext === 'mjs' || ext === 'cjs' || ext === 'jsx' || ext === 'ts' || ext === 'tsx') return 'javascript';
  if (ext === 'json') return 'json';
  if (ext === 'css') return 'css';
  if (ext === 'md') return 'markdown';
  if (ext === 'py') return 'python';
  if (looksLikeHtml(content, path)) return 'html';
  const text = String(content || '').trim();
  if ((text.startsWith('{') && text.endsWith('}')) || (text.startsWith('[') && text.endsWith(']'))) return 'json';
  if (/^#{1,6}\s/m.test(text) || /^[-*+]\s/m.test(text)) return 'markdown';
  if (/^(def|class|from|import)\s/m.test(text)) return 'python';
  if (/\b(function|const|let|var|import|export)\b/.test(text)) return 'javascript';
  if (/^[.#@\w\s-]+\{[\s\S]*\}$/m.test(text)) return 'css';
  return 'text';
}

function currentLanguage() {
  return detectLanguage(state.currentContent, currentSourcePath(), state.currentArtifact?.type || '');
}

function languageLabel(language) {
  return {
    html: 'HTML',
    javascript: 'JavaScript',
    json: 'JSON',
    css: 'CSS',
    markdown: 'Markdown',
    python: 'Python',
    text: 'Text',
  }[String(language || '').toLowerCase()] || 'Text';
}

function looksLikeHtml(content, path = '') {
  const ext = extname(path);
  if (ext === 'html' || ext === 'htm') return true;
  const text = String(content || '').trim().toLowerCase();
  return text.startsWith('<!doctype html') || text.startsWith('<html') || text.includes('<body') || text.includes('<div');
}

function artifactPreviewBaseUrl(artifact) {
  if (!artifact?.project_id || !artifact?.artifact_id) return '';
  return `/api/artifacts/${encodeURIComponent(artifact.project_id)}/library/${encodeURIComponent(artifact.artifact_id)}/preview/`;
}

function artifactPreviewUrl(artifact) {
  if (!artifact?.project_id || !artifact?.artifact_id) return '';
  const stamp = Number(artifact.updated_at || artifact.last_seen_at || artifact.created_at || 0);
  return `/api/artifacts/${encodeURIComponent(artifact.project_id)}/library/${encodeURIComponent(artifact.artifact_id)}/preview?v=${encodeURIComponent(String(stamp || Date.now()))}`;
}

function buildPreviewDoc(source, options = {}) {
  const text = String(source || '');
  const baseHref = String(options.baseHref || '').trim();
  const baseTag = baseHref ? `<base href="${escapeHtml(baseHref)}">` : '';
  if (!text.trim()) {
    return `<!doctype html><html><head>${baseTag}</head><body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#0b0d10;color:#aeb8c7;font-family:Segoe UI,sans-serif">Nothing to preview yet.</body></html>`;
  }
  if (/<!doctype html|<html[\s>]/i.test(text)) {
    if (!baseTag) return text;
    if (/<head[\s>]/i.test(text)) return text.replace(/<head([^>]*)>/i, `<head$1>${baseTag}`);
    return text.replace(/<html([^>]*)>/i, `<html$1><head>${baseTag}</head>`);
  }
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${baseTag}</head><body>${text}</body></html>`;
}

function formatArtifactTime(value) {
  const time = Number(value) || 0;
  if (!time) return 'just now';
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - time));
  if (delta < 60) return 'just now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  if (delta < 604800) return `${Math.floor(delta / 86400)}d ago`;
  return new Date(time * 1000).toLocaleDateString();
}

function formatArtifactDateTime(value) {
  const time = Number(value) || 0;
  if (!time) return 'just now';
  return new Date(time * 1000).toLocaleString();
}

function artifactSortLabel(value) {
  return {
    updated_desc: 'Recently updated',
    created_desc: 'Recently created',
    title_asc: 'Title A–Z',
    type_asc: 'Type',
  }[String(value || '')] || 'Recently updated';
}

function artifactSortValue(artifact, mode = state.artifactSort) {
  if (mode === 'created_desc') return Number(artifact?.created_at || artifact?.updated_at || artifact?.last_seen_at || 0);
  return Number(artifact?.updated_at || artifact?.last_seen_at || artifact?.created_at || 0);
}

function sortArtifacts(items, mode = state.artifactSort) {
  const artifacts = Array.isArray(items) ? items.slice() : [];
  if (mode === 'title_asc') {
    return artifacts.sort((left, right) => String(left?.title || left?.artifact_id || '').localeCompare(String(right?.title || right?.artifact_id || '')));
  }
  if (mode === 'type_asc') {
    return artifacts.sort((left, right) => {
      const byType = labelForArtifactType(left?.type).localeCompare(labelForArtifactType(right?.type));
      if (byType) return byType;
      return artifactSortValue(right, 'updated_desc') - artifactSortValue(left, 'updated_desc');
    });
  }
  return artifacts.sort((left, right) => artifactSortValue(right, mode) - artifactSortValue(left, mode));
}

function highlightMarkup(text) {
  let html = escapeHtml(text).replace(/\t/g, '  ');
  html = html.replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="bb-studio__token bb-studio__token--comment">$1</span>');
  html = html.replace(/(&lt;!doctype[^&]*?&gt;)/gi, '<span class="bb-studio__token bb-studio__token--keyword">$1</span>');
  html = html.replace(/(&lt;\/?)([A-Za-z][A-Za-z0-9:-]*)/g, '$1<span class="bb-studio__token bb-studio__token--keyword">$2</span>');
  html = html.replace(/\b([A-Za-z_:][A-Za-z0-9:._-]*)(=)/g, '<span class="bb-studio__token bb-studio__token--property">$1</span>$2');
  html = html.replace(/(&quot;.*?&quot;|&#39;.*?&#39;)/g, '<span class="bb-studio__token bb-studio__token--string">$1</span>');
  return html;
}

function highlightJson(text) {
  let html = escapeHtml(text).replace(/\t/g, '  ');
  html = html.replace(/(&quot;(?:\\.|[^&])*?&quot;)(\s*:)/g, '<span class="bb-studio__token bb-studio__token--property">$1</span>$2');
  html = html.replace(/(:\s*)(&quot;(?:\\.|[^&])*?&quot;)/g, '$1<span class="bb-studio__token bb-studio__token--string">$2</span>');
  html = html.replace(/\b(true|false|null)\b/g, '<span class="bb-studio__token bb-studio__token--keyword">$1</span>');
  html = html.replace(/(-?\b\d+(?:\.\d+)?\b)/g, '<span class="bb-studio__token bb-studio__token--number">$1</span>');
  return html;
}

function highlightScript(text, language = 'javascript') {
  let html = escapeHtml(text).replace(/\t/g, '  ');
  if (language === 'python') {
    html = html.replace(/(^|\n)(\s*#.*?)(?=\n|$)/g, '$1<span class="bb-studio__token bb-studio__token--comment">$2</span>');
    html = html.replace(/\b(def|class|from|import|return|if|elif|else|for|while|try|except|with|as|pass|raise|async|await|True|False|None)\b/g, '<span class="bb-studio__token bb-studio__token--keyword">$1</span>');
  } else {
    html = html.replace(/(\/\/.*?$|\/\*[\s\S]*?\*\/)/gm, '<span class="bb-studio__token bb-studio__token--comment">$1</span>');
    html = html.replace(/\b(function|const|let|var|return|if|else|for|while|class|import|export|from|async|await|new|try|catch|finally|switch|case|break|continue|throw)\b/g, '<span class="bb-studio__token bb-studio__token--keyword">$1</span>');
  }
  html = html.replace(/(&quot;.*?&quot;|&#39;.*?&#39;|`.*?`)/g, '<span class="bb-studio__token bb-studio__token--string">$1</span>');
  html = html.replace(/(-?\b\d+(?:\.\d+)?\b)/g, '<span class="bb-studio__token bb-studio__token--number">$1</span>');
  return html;
}

function highlightCss(text) {
  let html = escapeHtml(text).replace(/\t/g, '  ');
  html = html.replace(/(\/\*[\s\S]*?\*\/)/g, '<span class="bb-studio__token bb-studio__token--comment">$1</span>');
  html = html.replace(/(^|\n)([^\n{]+)(\s*\{)/g, '$1<span class="bb-studio__token bb-studio__token--keyword">$2</span>$3');
  html = html.replace(/\b([A-Za-z-]+)(\s*:)/g, '<span class="bb-studio__token bb-studio__token--property">$1</span>$2');
  html = html.replace(/(&quot;.*?&quot;|&#39;.*?&#39;)/g, '<span class="bb-studio__token bb-studio__token--string">$1</span>');
  html = html.replace(/(-?\b\d+(?:\.\d+)?(?:px|rem|em|vh|vw|%)?\b)/g, '<span class="bb-studio__token bb-studio__token--number">$1</span>');
  return html;
}

function highlightMarkdown(text) {
  let html = escapeHtml(text).replace(/\t/g, '  ');
  html = html.replace(/(^|\n)(#{1,6}\s.*?$)/gm, '$1<span class="bb-studio__token bb-studio__token--keyword">$2</span>');
  html = html.replace(/(^|\n)(\s*[-*+]\s.*?$)/gm, '$1<span class="bb-studio__token bb-studio__token--property">$2</span>');
  html = html.replace(/(```[\s\S]*?```|`[^`]+`)/g, '<span class="bb-studio__token bb-studio__token--string">$1</span>');
  return html;
}

function highlightCodeFragment(text, language) {
  const source = String(text || '');
  if (!source) return '&nbsp;';
  if (language === 'html') return highlightMarkup(source);
  if (language === 'json') return highlightJson(source);
  if (language === 'javascript' || language === 'python') return highlightScript(source, language);
  if (language === 'css') return highlightCss(source);
  if (language === 'markdown') return highlightMarkdown(source);
  return escapeHtml(source).replace(/\t/g, '  ');
}

function indentColumns(line) {
  const leading = (String(line || '').match(/^[\t ]*/) || [''])[0];
  let columns = 0;
  for (const char of leading) columns += char === '\t' ? INDENT_GUIDE_SPACES : 1;
  return columns;
}

function indentDepthForLine(line, fallbackDepth = 0) {
  if (!String(line || '').trim()) return fallbackDepth;
  return Math.floor(indentColumns(line) / INDENT_GUIDE_SPACES);
}

function indentGuideColor(depthIndex, depthCount) {
  const hue = 220 + Math.min(depthIndex * 18, 120);
  const alpha = depthIndex === depthCount - 1 ? 0.34 : Math.min(0.14 + depthIndex * 0.03, 0.28);
  return `hsla(${hue}, 88%, 72%, ${alpha})`;
}

function renderIndentedHighlight(text, language) {
  const lines = String(text || '').split('\n');
  let previousDepth = 0;
  return lines.map((line, index) => {
    const depth = indentDepthForLine(line, previousDepth);
    previousDepth = depth;
    const guides = Array.from({ length: depth }, (_, guideIndex) => {
      const left = guideIndex * INDENT_GUIDE_SPACES + 1;
      return `<span class="bb-studio__indent-guide" style="left:${left}ch;--guide-color:${indentGuideColor(guideIndex, depth)}"></span>`;
    }).join('');
    const content = highlightCodeFragment(line, language) || '&nbsp;';
    return `<span class="bb-studio__code-line" data-line="${index + 1}">${guides}<span class="bb-studio__code-line-content">${content}</span></span>`;
  }).join('');
}

function syncEditorOverlay() {
  const editor = studioCodeEl();
  const highlight = studioCodeHighlightEl();
  const gutter = studioCodeGutterEl();
  if (!editor || !highlight) return;
  highlight.scrollTop = editor.scrollTop;
  highlight.scrollLeft = editor.scrollLeft;
  if (gutter) gutter.scrollTop = editor.scrollTop;
}

function lineCountForText(text) {
  return String(text || '').split('\n').length;
}

function lineStartIndex(text, index) {
  const value = String(text || '');
  const bounded = Math.max(0, Math.min(Number(index) || 0, value.length));
  const marker = value.lastIndexOf('\n', Math.max(0, bounded - 1));
  return marker === -1 ? 0 : marker + 1;
}

function selectionSummary(text, start, end) {
  const value = String(text || '');
  const from = Math.max(0, Math.min(Number(start) || 0, value.length));
  const to = Math.max(from, Math.min(Number(end) || 0, value.length));
  const selected = value.slice(from, to);
  return {
    chars: selected.length,
    lines: selected ? selected.split('\n').length : 0,
  };
}

function renderCodeChrome() {
  const editor = studioCodeEl();
  const gutter = studioCodeGutterEl();
  const linesEl = studioCodeLinesEl();
  const cursorEl = studioCodeCursorEl();
  const selectionEl = studioCodeSelectionEl();
  const value = editor ? String(editor.value || '') : String(state.currentContent || '');
  const lineCount = lineCountForText(value);
  if (gutter) {
    gutter.textContent = Array.from({ length: lineCount }, (_, index) => String(index + 1)).join('\n');
  }
  if (linesEl) linesEl.textContent = `${lineCount} line${lineCount === 1 ? '' : 's'}`;
  const selectionStart = editor ? editor.selectionStart : 0;
  const selectionEnd = editor ? editor.selectionEnd : 0;
  const currentLine = value.slice(0, Math.max(0, selectionStart)).split('\n').length;
  const currentColumn = selectionStart - lineStartIndex(value, selectionStart) + 1;
  if (cursorEl) cursorEl.textContent = `Ln ${currentLine}, Col ${currentColumn}`;
  const selection = selectionSummary(value, selectionStart, selectionEnd);
  if (selectionEl) {
    selectionEl.textContent = selection.lines > 1 ? `Sel ${selection.chars} (${selection.lines} lines)` : `Sel ${selection.chars}`;
  }
}

function renderStudioHeader() {
  const title = studioTitleEl();
  const subtitle = studioSubtitleEl();
  const path = studioPathEl();
  if (title) title.textContent = state.currentArtifact ? artifactDisplayTitle(state.currentArtifact) : 'Artifact Studio';
  if (subtitle) {
    if (state.currentArtifact) subtitle.textContent = isDirty() ? 'editing artifact project · unsaved changes' : 'artifact project loaded';
    else subtitle.textContent = 'open an artifact from chat or pick one from recent artifacts';
  }
  if (path) path.textContent = currentDocLabel();
}

function renderArtifactFiles() {
  const root = studioFileListEl();
  const count = studioFileCountEl();
  if (!root) return;
  const files = Array.isArray(state.currentArtifact?.files) ? state.currentArtifact.files : [];
  if (!state.currentArtifact) {
    if (count) count.textContent = 'open an artifact to browse files';
    root.innerHTML = '<div class="bb-studio__empty">Select an artifact to browse its files.</div>';
    return;
  }
  const fileCount = files.filter((item) => item.type === 'file').length;
  if (count) count.textContent = `${fileCount} file${fileCount === 1 ? '' : 's'} · entry ${state.currentArtifact.entry_file || '—'}`;
  if (!files.length) {
    root.innerHTML = '<div class="bb-studio__empty">No files yet. Create one to start building this artifact.</div>';
    return;
  }
  root.innerHTML = files.map((entry) => {
    const active = entry.type === 'file' && entry.path === state.currentFilePath ? ' bb-studio__file--active' : '';
    const icon = entry.type === 'dir' ? '▸' : '•';
    const meta = entry.type === 'dir' ? 'folder' : `${Math.max(0, Number(entry.size) || 0)} bytes`;
    return `
      <button class="bb-studio__file bb-studio__project-file${active}" type="button" data-artifact-path="${escapeHtml(entry.path)}" data-artifact-path-type="${escapeHtml(entry.type)}">
        <span class="bb-studio__file-icon">${icon}</span>
        <span class="bb-studio__artifact-body">
          <span class="bb-studio__artifact-line">
            <span class="bb-studio__file-name">${escapeHtml(entry.path)}</span>
            ${entry.path === state.currentArtifact.entry_file ? '<span class="bb-studio__artifact-badge">Entry</span>' : ''}
          </span>
          <span class="bb-studio__artifact-meta">${escapeHtml(meta)}</span>
        </span>
      </button>
    `;
  }).join('');
  for (const button of Array.from(root.querySelectorAll('[data-artifact-path]'))) {
    button.addEventListener('click', async () => {
      if (button.dataset.artifactPathType !== 'file') return;
      const path = button.dataset.artifactPath || '';
      if (!path) return;
      await openArtifactFile(path);
    });
  }
}

function renderWorkspaceTabs() {
  for (const button of workspaceButtons()) {
    const active = button.dataset.workspaceSwitch === state.view;
    button.classList.toggle('bb-workspace__tab--active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
  for (const view of workspaceViews()) {
    view.classList.toggle('bb-workspace__view--active', view.dataset.workspaceView === state.view);
  }
  const shell = workspaceShell();
  if (shell) shell.dataset.activeWorkspaceView = state.view;
}

function renderStudioTabs() {
  const buttons = Array.from(document.querySelectorAll('[data-studio-switch]'));
  const panels = Array.from(document.querySelectorAll('[data-studio-panel]'));
  for (const button of buttons) {
    const active = button.dataset.studioSwitch === state.studioTab;
    button.classList.toggle('bb-studio__switch--active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
  for (const panel of panels) {
    panel.classList.toggle('bb-studio__panel--active', panel.dataset.studioPanel === state.studioTab);
  }
}

function renderStudioSidebarTabs() {
  const sidebar = document.querySelector('.bb-studio__sidebar');
  if (!sidebar) return;
  const activeTab = state.studioSidebarTab === 'files' ? 'files' : 'artifacts';
  sidebar.dataset.mobileSidebarTab = activeTab;
  sidebar.dataset.mobileSidebarEnabled = isCompactStudioViewport() ? '1' : '0';
  for (const button of Array.from(document.querySelectorAll('[data-studio-sidebar-tab]'))) {
    const active = button.dataset.studioSidebarTab === activeTab;
    button.classList.toggle('bb-studio__mobile-sidebar-tab--active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
  renderStudioMobilePane();
}

async function loadArtifactBrowser() {
  const project = activeProject();
  if (!project?.project_id) {
    state.artifacts = [];
    renderArtifactList();
    return;
  }
  try {
    const data = await api.artifactList(project.project_id);
    state.artifacts = sortArtifacts(Array.isArray(data?.artifacts) ? data.artifacts : []);
    renderArtifactList();
  } catch (err) {
    state.artifacts = [];
    renderArtifactList();
    toast(`Artifact browser failed: ${err.message}`, { kind: 'error' });
  }
}

function renderPreview() {
  const frame = studioFrameEl();
  const title = studioPreviewTitleEl();
  const meta = studioPreviewMetaEl();
  const badge = studioPreviewBadgeEl();
  if (!frame) return;
  if (badge) badge.textContent = state.studioTab === 'preview' ? 'Live preview' : 'Preview';
  if (!state.currentArtifact) {
    frame.removeAttribute('src');
    frame.srcdoc = buildPreviewDoc('');
    if (title) title.textContent = 'Select an artifact to preview';
    if (meta) meta.textContent = `Sorted by ${artifactSortLabel(state.artifactSort)} so the latest work is easier to find.`;
    return;
  }
  const entryFile = String(state.currentArtifact.entry_file || '');
  const activePath = String(state.currentFilePath || entryFile || '');
  const source = state.currentContent || state.currentArtifact?.source || '';
  const previewBase = artifactPreviewBaseUrl(state.currentArtifact);
  const previewUrl = artifactPreviewUrl(state.currentArtifact);
  const entryLooksHtml = looksLikeHtml(state.currentArtifact?.source || '', entryFile);
  if (entryLooksHtml) {
    if (activePath && activePath === entryFile && looksLikeHtml(source, activePath)) {
      frame.removeAttribute('src');
      frame.srcdoc = buildPreviewDoc(source, { baseHref: previewBase });
    } else {
      frame.srcdoc = '';
      frame.src = previewUrl;
    }
  } else {
    frame.removeAttribute('src');
    frame.srcdoc = buildPreviewDoc(source);
  }
  if (title) title.textContent = artifactDisplayTitle(state.currentArtifact);
  if (meta) {
    const bits = [
      labelForArtifactType(state.currentArtifact.type),
      activePath ? `editing ${activePath}` : '',
      `updated ${formatArtifactDateTime(state.currentArtifact.updated_at || state.currentArtifact.last_seen_at || state.currentArtifact.created_at)}`,
      state.currentArtifact.artifact_id,
    ].filter(Boolean);
    meta.textContent = bits.join(' · ');
  }
}

function renderEditor() {
  const editor = studioCodeEl();
  const highlight = studioCodeHighlightEl();
  const badge = studioCodeLanguageEl();
  if (!editor) return;
  if (editor.value !== state.currentContent) editor.value = state.currentContent;
  const language = currentLanguage();
  editor.dataset.language = language;
  if (badge) badge.textContent = languageLabel(language);
  if (highlight) highlight.innerHTML = renderIndentedHighlight(state.currentContent, language);
  renderCodeChrome();
  syncEditorOverlay();
}

function renderArtifactList() {
  const root = studioArtifactListEl();
  const count = studioArtifactCountEl();
  const sort = studioArtifactSortEl();
  if (!root) return;
  const artifacts = state.artifacts;
  if (sort && sort.value !== state.artifactSort) sort.value = state.artifactSort;
  if (count) count.textContent = state.artifacts.length ? `${state.artifacts.length} artifact${state.artifacts.length === 1 ? '' : 's'} · ${artifactSortLabel(state.artifactSort)}` : 'no recent artifacts';
  if (!state.artifacts.length) {
    root.innerHTML = '<div class="bb-studio__empty">No saved artifacts yet. Open one from chat to persist it.</div>';
    return;
  }
  root.innerHTML = artifacts.map((artifact) => {
    const active = artifact.artifact_id === state.currentArtifact?.artifact_id ? ' bb-studio__file--active' : '';
    return `
      <div class="bb-studio__artifact-row">
        <button class="bb-studio__file bb-studio__artifact-card${active}" type="button" data-artifact-id="${escapeHtml(artifact.artifact_id)}">
          <span class="bb-studio__file-icon">◆</span>
          <span class="bb-studio__artifact-body">
            <span class="bb-studio__artifact-line">
              <span class="bb-studio__file-name">${escapeHtml(artifactDisplayTitle(artifact))}</span>
              <span class="bb-studio__artifact-badge">${escapeHtml(labelForArtifactType(artifact.type))}</span>
            </span>
            <span class="bb-studio__artifact-meta">${escapeHtml(formatArtifactTime(artifact.updated_at || artifact.last_seen_at || artifact.created_at))} · ${escapeHtml(String(artifact.file_count || 0))} files</span>
            <span class="bb-studio__artifact-meta">${escapeHtml(artifact.artifact_id)}</span>
          </span>
        </button>
        <div class="bb-studio__artifact-actions">
          <button class="bb-studio__mini-action" type="button" data-artifact-edit="${escapeHtml(artifact.artifact_id)}">Edit</button>
          <button class="bb-studio__mini-action" type="button" data-artifact-rename="${escapeHtml(artifact.artifact_id)}">Rename</button>
          <button class="bb-studio__mini-action bb-studio__mini-action--danger" type="button" data-artifact-delete="${escapeHtml(artifact.artifact_id)}">Delete</button>
        </div>
      </div>
    `;
  }).join('');
  for (const button of Array.from(root.querySelectorAll('[data-artifact-id]'))) {
    button.addEventListener('click', async () => {
      const artifactId = button.dataset.artifactId || '';
      if (!artifactId) return;
      await openSavedArtifact(artifactId);
    });
  }
  for (const button of Array.from(root.querySelectorAll('[data-artifact-edit]'))) {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      const artifactId = button.dataset.artifactEdit || '';
      if (!artifactId) return;
      await openSavedArtifact(artifactId);
    });
  }
  for (const button of Array.from(root.querySelectorAll('[data-artifact-rename]'))) {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      const artifactId = button.dataset.artifactRename || '';
      const artifact = state.artifacts.find((item) => item.artifact_id === artifactId);
      if (!artifact) return;
      await renameArtifact(artifact);
    });
  }
  for (const button of Array.from(root.querySelectorAll('[data-artifact-delete]'))) {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      const artifactId = button.dataset.artifactDelete || '';
      const artifact = state.artifacts.find((item) => item.artifact_id === artifactId);
      if (!artifact) return;
      await deleteArtifactRecord(artifact);
    });
  }
}

function renderSoftwareMap() {
  const host = document.getElementById('workspace-software-map');
  if (!host) return;
  const cardsByColumn = store.get().board?.cards_by_column || {};
  const statuses = Object.entries(cardsByColumn);
  const total = statuses.reduce((sum, [, cards]) => sum + (cards || []).length, 0);
  host.innerHTML = `
    <div class="bb-map__hero">
      <strong>Software map</strong>
      <span>${total} cards across ${statuses.length || 0} lanes</span>
    </div>
    <div class="bb-map__grid">
      ${statuses.map(([status, cards]) => `
        <section class="bb-map__lane" data-status="${escapeHtml(status)}">
          <header>
            <strong>${escapeHtml(status)}</strong>
            <span>${(cards || []).length}</span>
          </header>
          <div class="bb-map__lane-body">
            ${(cards || []).slice(0, MAP_CARD_LIMIT).map((card) => `<div class="bb-map__node">${escapeHtml(card.title)}</div>`).join('') || '<div class="bb-map__empty">No cards</div>'}
          </div>
        </section>
      `).join('')}
    </div>
  `;
}

function renderExecutionMap() {
  const host = document.getElementById('workspace-execution-map');
  if (!host) return;
  const jobs = store.get().jobs || [];
  const activeJobs = jobs.filter((job) => ['pending', 'running', 'merging', 'paused'].includes(job.status));
  const feed = (state.executionLive.feed || []).slice(0, 14);
  host.innerHTML = `
    <div class="bb-map__hero">
      <strong>Execution map</strong>
      <span>${activeJobs.length} active jobs · ${jobs.length} total tracked · live file focus and stream preview</span>
    </div>
    <section class="bb-map__monitor">
      <header class="bb-map__monitor-head">
        <strong>Live agent feed</strong>
        <span>${feed.length ? `${feed.length} recent updates` : 'waiting for live execution events'}</span>
      </header>
      <div class="bb-map__monitor-feed">
        ${feed.length ? feed.map((entry) => `
          <div class="bb-map__monitor-line bb-map__monitor-line--${escapeHtml(entry.kind || 'event')}">
            <span class="bb-map__monitor-time">${escapeHtml(relativeTimeLabel(entry.at))}</span>
            <span class="bb-map__monitor-label">${escapeHtml(entry.label || 'event')}</span>
            <span class="bb-map__monitor-text">${escapeHtml(entry.text || '')}</span>
          </div>
        `).join('') : '<div class="bb-map__empty">No live execution events yet. Start a coding job to see per-file progress here.</div>'}
      </div>
    </section>
    <div class="bb-map__timeline">
      ${activeJobs.length ? activeJobs.map((job) => {
        const live = state.executionLive.jobs[String(job.job_id || '')] || {};
        const files = executionFilesForJob(job, live);
        const currentFile = normalizeExecPath(live.currentFile || files[0] || '');
        const lastNote = String(live.note || job.progress_note || '').trim();
        const previewText = String(live.previewText || '').trim();
        const timeline = Array.isArray(live.timeline) ? live.timeline.slice(0, EXEC_TIMELINE_LIMIT) : [];
        return `
        <article class="bb-map__event bb-map__event--${escapeHtml(job.status || 'pending')} bb-map__event--detailed">
          <div class="bb-map__event-head">
            <div class="bb-map__event-title-wrap">
              <strong>${escapeHtml(job.task?.objective || job.job_id || 'job')}</strong>
              <div class="bb-map__event-meta">
                <code>${escapeHtml(job.job_id || '')}</code>
                <span>${escapeHtml(job.worktree_branch || '(no branch)')}</span>
                ${job.task?.card_id ? `<span>card ${escapeHtml(job.task.card_id)}</span>` : ''}
              </div>
            </div>
            <span class="bb-map__status-pill bb-map__status-pill--${escapeHtml(job.status || 'pending')}">${escapeHtml(job.status || 'pending')}</span>
          </div>
          <div class="bb-map__event-live-grid">
            <div class="bb-map__event-live-box">
              <span class="bb-map__event-live-label">Current file</span>
              <code class="bb-map__event-live-value">${escapeHtml(currentFile || 'waiting for file focus')}</code>
            </div>
            <div class="bb-map__event-live-box">
              <span class="bb-map__event-live-label">Last update</span>
              <span class="bb-map__event-live-value">${escapeHtml(relativeTimeLabel(live.updatedAt || 0))}</span>
            </div>
            <div class="bb-map__event-live-box">
              <span class="bb-map__event-live-label">Attempt</span>
              <span class="bb-map__event-live-value">${escapeHtml(String(live.attempt || 1))}</span>
            </div>
          </div>
          <div class="bb-map__event-note">${escapeHtml(lastNote || 'Waiting for the next tool step or transcript chunk…')}</div>
          ${previewText ? `<pre class="bb-map__event-preview">${escapeHtml(previewText)}</pre>` : ''}
          <div class="bb-map__file-strip">
            ${files.length ? files.map((file) => `
              <span class="bb-map__file-chip${matchesExecFile(file, currentFile) ? ' bb-map__file-chip--active' : ''}">${escapeHtml(file)}</span>
            `).join('') : '<span class="bb-map__file-chip">No target files declared</span>'}
          </div>
          <div class="bb-map__step-list">
            ${timeline.length ? timeline.map((entry) => `
              <div class="bb-map__step">
                <span class="bb-map__step-time">${escapeHtml(relativeTimeLabel(entry.at))}</span>
                <span class="bb-map__step-label">${escapeHtml(entry.label || 'step')}</span>
                <span class="bb-map__step-text">${escapeHtml(entry.text || '')}</span>
              </div>
            `).join('') : '<div class="bb-map__empty">No live step history yet for this job.</div>'}
          </div>
        </article>
      `; }).join('') : '<div class="bb-studio__empty">No active jobs right now.</div>'}
    </div>
  `;
}

async function openSavedArtifact(artifactId) {
  const project = activeProject();
  if (!project?.project_id) return;
  try {
    const data = await api.artifactGet(project.project_id, artifactId);
    state.currentArtifact = data;
    state.artifactFiles = Array.isArray(data.files) ? data.files : [];
    state.currentFilePath = String(data.entry_file || '');
    state.currentContent = data.source || '';
    state.lastSavedContent = state.currentContent;
    state.studioSidebarTab = 'files';
    state.studioTab = looksLikeHtml(state.currentContent, state.currentFilePath || data.source_path || '') ? 'preview' : 'code';
    renderStudioHeader();
    renderStudioTabs();
    renderStudioSidebarTabs();
    renderEditor();
    renderPreview();
    renderArtifactList();
    renderArtifactFiles();
    if (isCompactStudioViewport()) setStudioMobilePane('viewer');
    switchView('studio');
  } catch (err) {
    toast(`Artifact load failed: ${err.message}`, { kind: 'error' });
  }
}

async function openArtifactFile(path) {
  const project = activeProject();
  if (!project?.project_id || !state.currentArtifact?.artifact_id) return;
  try {
    const data = await api.artifactFileGet(project.project_id, state.currentArtifact.artifact_id, path);
    state.currentFilePath = data.path || path;
    state.currentContent = data.content || '';
    state.lastSavedContent = state.currentContent;
    state.studioSidebarTab = 'files';
    state.studioTab = looksLikeHtml(state.currentContent, state.currentFilePath) ? 'preview' : 'code';
    renderStudioHeader();
    renderStudioTabs();
    renderStudioSidebarTabs();
    renderEditor();
    renderPreview();
    renderArtifactFiles();
    if (isCompactStudioViewport()) setStudioMobilePane('viewer');
  } catch (err) {
    toast(`Artifact file failed to open: ${err.message}`, { kind: 'error' });
  }
}

async function createArtifactFile() {
  const project = activeProject();
  if (!project?.project_id || !state.currentArtifact?.artifact_id) {
    toast('Open an artifact first to add a file.', { kind: 'warn' });
    return;
  }
  const nextPath = window.prompt('New artifact file path', state.currentArtifact.entry_file || 'index.html');
  if (nextPath === null) return;
  const path = String(nextPath || '').trim();
  if (!path) {
    toast('File path cannot be empty.', { kind: 'warn' });
    return;
  }
  try {
    const updated = await api.artifactFilePut(project.project_id, state.currentArtifact.artifact_id, { path, content: '' });
    state.currentArtifact = updated;
    state.artifactFiles = Array.isArray(updated.files) ? updated.files : [];
    state.studioSidebarTab = 'files';
    await loadArtifactBrowser();
    await openArtifactFile(path);
    toast('Artifact file created', { kind: 'success' });
  } catch (err) {
    toast(`Artifact file create failed: ${err.message}`, { kind: 'error' });
  }
}

async function createArtifactFolder() {
  const project = activeProject();
  if (!project?.project_id || !state.currentArtifact?.artifact_id) {
    toast('Open an artifact first to add a folder.', { kind: 'warn' });
    return;
  }
  const nextPath = window.prompt('New artifact folder path', 'assets');
  if (nextPath === null) return;
  const path = String(nextPath || '').trim();
  if (!path) {
    toast('Folder path cannot be empty.', { kind: 'warn' });
    return;
  }
  try {
    const updated = await api.artifactFolderCreate(project.project_id, state.currentArtifact.artifact_id, { path });
    state.currentArtifact = updated;
    state.artifactFiles = Array.isArray(updated.files) ? updated.files : [];
    state.studioSidebarTab = 'files';
    renderStudioHeader();
    renderStudioSidebarTabs();
    renderArtifactFiles();
    await loadArtifactBrowser();
    toast('Artifact folder created', { kind: 'success' });
  } catch (err) {
    toast(`Artifact folder create failed: ${err.message}`, { kind: 'error' });
  }
}

async function renameArtifact(target = state.currentArtifact) {
  const project = activeProject();
  if (!project?.project_id || !target?.artifact_id) {
    toast('Open an artifact first to rename it.', { kind: 'warn' });
    return;
  }
  const nextTitle = window.prompt('Rename artifact', artifactDisplayTitle(target));
  if (nextTitle === null) return;
  const title = String(nextTitle || '').trim();
  if (!title) {
    toast('Artifact name cannot be empty.', { kind: 'warn' });
    return;
  }
  try {
    const updated = await api.artifactUpdate(project.project_id, target.artifact_id, { title });
    if (state.currentArtifact?.artifact_id === target.artifact_id) {
      state.currentArtifact = updated;
      renderStudioHeader();
      renderEditor();
    }
    await loadArtifactBrowser();
    toast('Artifact renamed', { kind: 'success' });
  } catch (err) {
    toast(`Artifact rename failed: ${err.message}`, { kind: 'error' });
  }
}

async function deleteArtifactRecord(target = state.currentArtifact) {
  const project = activeProject();
  if (!project?.project_id || !target?.artifact_id) {
    toast('Open an artifact first to delete it.', { kind: 'warn' });
    return;
  }
  const okay = window.confirm(`Delete artifact "${artifactDisplayTitle(target)}"?`);
  if (!okay) return;
  try {
    await api.artifactDelete(project.project_id, target.artifact_id);
    const deletedCurrent = state.currentArtifact?.artifact_id === target.artifact_id;
    if (deletedCurrent) {
      state.currentArtifact = null;
      state.artifactFiles = [];
      state.currentFilePath = '';
      state.currentContent = '';
      state.lastSavedContent = '';
      state.studioSidebarTab = 'artifacts';
      state.studioTab = 'preview';
      renderStudioHeader();
      renderStudioTabs();
      renderStudioSidebarTabs();
      renderEditor();
      renderPreview();
      renderArtifactFiles();
    }
    await loadArtifactBrowser();
    toast('Artifact deleted', { kind: 'success' });
  } catch (err) {
    toast(`Artifact delete failed: ${err.message}`, { kind: 'error' });
  }
}

async function saveCurrentFile() {
  const project = activeProject();
  if (!state.currentArtifact?.artifact_id || !project?.project_id) {
    toast('Open an artifact first to save changes.', { kind: 'warn' });
    return;
  }
  try {
    const artifact = await api.artifactFilePut(project.project_id, state.currentArtifact.artifact_id, {
      path: state.currentFilePath || state.currentArtifact.entry_file || 'index.html',
      content: state.currentContent,
    });
    state.currentArtifact = artifact;
    state.artifactFiles = Array.isArray(artifact.files) ? artifact.files : [];
    state.lastSavedContent = state.currentContent;
    renderStudioHeader();
    renderPreview();
    renderArtifactFiles();
    await loadArtifactBrowser();
    toast('Artifact saved', { kind: 'success' });
  } catch (err) {
    toast(`Artifact save failed: ${err.message}`, { kind: 'error' });
  }
}

function applyEditorToPreview() {
  const editor = studioCodeEl();
  if (!editor) return;
  state.currentContent = editor.value;
  renderStudioHeader();
  renderEditor();
  renderPreview();
}

function restoreArtifactSource() {
  if (!state.currentArtifact) return;
  state.currentFilePath = state.currentArtifact.entry_file || '';
  state.currentContent = state.currentArtifact.source || '';
  state.lastSavedContent = state.currentContent;
  state.studioTab = looksLikeHtml(state.currentContent, state.currentFilePath || state.currentArtifact.source_path || '') ? 'preview' : 'code';
  renderStudioHeader();
  renderStudioTabs();
  renderEditor();
  renderPreview();
  renderArtifactList();
  renderArtifactFiles();
}

function popOutPreview() {
  if (state.currentArtifact?.artifact_id && looksLikeHtml(state.currentArtifact?.source || '', state.currentArtifact?.entry_file || '')) {
    const url = artifactPreviewUrl(state.currentArtifact);
    if (url) {
      window.open(url, '_blank', 'noopener');
      return;
    }
  }
  const doc = buildPreviewDoc(state.currentContent || state.currentArtifact?.source || '');
  const blob = new Blob([doc], { type: 'text/html' });
  const url = URL.createObjectURL(blob);
  window.open(url, '_blank', 'noopener');
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function switchView(view) {
  state.view = view;
  if (view === 'studio' && isCompactStudioViewport()) {
    state.studioMobilePane = state.currentArtifact ? 'viewer' : 'browser';
  }
  renderWorkspaceTabs();
  renderStudioSidebarTabs();
  if (view === 'software-map') renderSoftwareMap();
  if (view === 'execution-map') renderExecutionMap();
}

function switchStudioTab(tab) {
  state.studioTab = tab;
  renderStudioTabs();
}

function switchStudioSidebarTab(tab) {
  state.studioSidebarTab = tab === 'files' ? 'files' : 'artifacts';
  renderStudioSidebarTabs();
}

function bindWorkspaceControls() {
  for (const button of workspaceButtons()) {
    if (button.dataset.bound === '1') continue;
    button.dataset.bound = '1';
    button.addEventListener('click', async () => {
      const view = button.dataset.workspaceSwitch || 'board';
      if (view === 'studio') await loadArtifactBrowser();
      switchView(view);
    });
  }
  for (const button of Array.from(document.querySelectorAll('[data-studio-switch]'))) {
    if (button.dataset.bound === '1') continue;
    button.dataset.bound = '1';
    button.addEventListener('click', () => switchStudioTab(button.dataset.studioSwitch || 'preview'));
  }
  for (const button of Array.from(document.querySelectorAll('[data-studio-sidebar-tab]'))) {
    if (button.dataset.bound === '1') continue;
    button.dataset.bound = '1';
    button.addEventListener('click', () => switchStudioSidebarTab(button.dataset.studioSidebarTab || 'artifacts'));
  }
  const editor = studioCodeEl();
  if (editor && editor.dataset.bound !== '1') {
    editor.dataset.bound = '1';
    editor.addEventListener('input', () => {
      state.currentContent = editor.value;
      renderStudioHeader();
      renderEditor();
    });
    editor.addEventListener('scroll', syncEditorOverlay);
    editor.addEventListener('click', renderCodeChrome);
    editor.addEventListener('keyup', renderCodeChrome);
    editor.addEventListener('select', renderCodeChrome);
    editor.addEventListener('keydown', (event) => {
      if (event.key !== 'Tab' || event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      const value = editor.value;
      editor.value = `${value.slice(0, start)}  ${value.slice(end)}`;
      editor.selectionStart = editor.selectionEnd = start + 2;
      state.currentContent = editor.value;
      renderStudioHeader();
      renderEditor();
    });
  }
  const artifactRefresh = document.getElementById('studio-artifacts-refresh');
  if (artifactRefresh && artifactRefresh.dataset.bound !== '1') {
    artifactRefresh.dataset.bound = '1';
    artifactRefresh.addEventListener('click', loadArtifactBrowser);
  }
  const newArtifactFile = document.getElementById('studio-file-new');
  if (newArtifactFile && newArtifactFile.dataset.bound !== '1') {
    newArtifactFile.dataset.bound = '1';
    newArtifactFile.addEventListener('click', createArtifactFile);
  }
  const newArtifactFolder = document.getElementById('studio-folder-new');
  if (newArtifactFolder && newArtifactFolder.dataset.bound !== '1') {
    newArtifactFolder.dataset.bound = '1';
    newArtifactFolder.addEventListener('click', createArtifactFolder);
  }
  const artifactSort = studioArtifactSortEl();
  if (artifactSort && artifactSort.dataset.bound !== '1') {
    artifactSort.dataset.bound = '1';
    artifactSort.value = state.artifactSort;
    artifactSort.addEventListener('change', () => {
      state.artifactSort = artifactSort.value || 'updated_desc';
      state.artifacts = sortArtifacts(state.artifacts, state.artifactSort);
      renderArtifactList();
      renderPreview();
    });
  }
  const apply = document.getElementById('studio-apply');
  if (apply && apply.dataset.bound !== '1') {
    apply.dataset.bound = '1';
    apply.addEventListener('click', () => {
      applyEditorToPreview();
      toast('Preview updated', { kind: 'success', timeout: 900 });
    });
  }
  const save = document.getElementById('studio-save');
  if (save && save.dataset.bound !== '1') {
    save.dataset.bound = '1';
    save.addEventListener('click', saveCurrentFile);
  }
  const artifact = document.getElementById('studio-artifact-source');
  if (artifact && artifact.dataset.bound !== '1') {
    artifact.dataset.bound = '1';
    artifact.addEventListener('click', restoreArtifactSource);
  }
  const artifactRename = document.getElementById('studio-artifact-rename');
  if (artifactRename && artifactRename.dataset.bound !== '1') {
    artifactRename.dataset.bound = '1';
    artifactRename.addEventListener('click', () => renameArtifact());
  }
  const artifactDelete = document.getElementById('studio-artifact-delete');
  if (artifactDelete && artifactDelete.dataset.bound !== '1') {
    artifactDelete.dataset.bound = '1';
    artifactDelete.addEventListener('click', () => deleteArtifactRecord());
  }
  const popout = document.getElementById('studio-popout');
  if (popout && popout.dataset.bound !== '1') {
    popout.dataset.bound = '1';
    popout.addEventListener('click', popOutPreview);
  }
  const mobileBack = document.getElementById('studio-mobile-back');
  if (mobileBack && mobileBack.dataset.bound !== '1') {
    mobileBack.dataset.bound = '1';
    mobileBack.addEventListener('click', () => setStudioMobilePane('browser'));
  }
}

export async function openArtifactStudio(payload = {}) {
  const project = activeProject();
  if (!project?.project_id) {
    toast('No active project selected', { kind: 'warn' });
    return;
  }
  const artifactId = String(payload.artifactId || '').trim();
  if (artifactId) {
    await openSavedArtifact(artifactId);
    return state.currentArtifact;
  }
  try {
    const reusable = await reusableArtifactFor(project.project_id, payload);
    if (reusable?.artifact_id) {
      await openSavedArtifact(reusable.artifact_id);
      return state.currentArtifact;
    }
  } catch (err) {
    console.warn('[studio] artifact reuse lookup failed:', err);
  }
  try {
    state.currentArtifact = await api.artifactCreate(project.project_id, {
      title: String(payload.title || 'Artifact Studio'),
      source: String(payload.source || ''),
      type: String(payload.type || 'html'),
    });
  } catch (err) {
    toast(`Artifact save failed: ${err.message}`, { kind: 'error' });
    return;
  }
  state.artifactFiles = Array.isArray(state.currentArtifact.files) ? state.currentArtifact.files : [];
  state.currentFilePath = state.currentArtifact.entry_file || '';
  state.currentContent = state.currentArtifact.source;
  state.lastSavedContent = state.currentContent;
  state.studioSidebarTab = 'files';
  state.studioTab = looksLikeHtml(state.currentContent, state.currentFilePath || state.currentArtifact.source_path || '') ? 'preview' : 'code';
  await loadArtifactBrowser();
  renderStudioHeader();
  renderStudioTabs();
  renderStudioSidebarTabs();
  renderEditor();
  renderPreview();
  renderArtifactList();
  renderArtifactFiles();
  switchView('studio');
  return state.currentArtifact;
}

export async function openWorkspaceView(view = 'board') {
  if (view === 'studio') await loadArtifactBrowser();
  switchView(view);
}

export function renderWorkspace() {
  bindWorkspaceControls();
  renderWorkspaceTabs();
  renderStudioTabs();
  renderStudioSidebarTabs();
  renderStudioHeader();
  renderArtifactList();
  renderArtifactFiles();
  renderSoftwareMap();
  renderExecutionMap();
  bus.on('store:board', () => {
    renderSoftwareMap();
    renderExecutionMap();
  });
  bus.on('store:jobs', renderExecutionMap);
  if (window.__bbExecutionMapLiveBound !== true) {
    window.__bbExecutionMapLiveBound = true;
    bus.on('ws:coding:job.progress', (payload) => {
      syncExecutionProgress(payload);
      renderExecutionMap();
    });
    bus.on('ws:coding:cli.transcript', (payload) => {
      syncExecutionTranscript(payload);
      renderExecutionMap();
    });
    for (const topic of ['ws:coding:job.created', 'ws:coding:job.started', 'ws:coding:job.paused', 'ws:coding:job.reviewing', 'ws:coding:job.completed', 'ws:coding:job.failed', 'ws:coding:job.merged']) {
      bus.on(topic, (payload) => {
        syncExecutionLifecycle(topic.replace(/^ws:/, ''), payload);
        renderExecutionMap();
      });
    }
  }
  if (window.__bbWorkspaceResizeBound !== true) {
    window.__bbWorkspaceResizeBound = true;
    window.addEventListener('resize', () => {
      if (!isCompactStudioViewport()) state.studioMobilePane = 'viewer';
      else if (state.view === 'studio' && !state.currentArtifact) state.studioMobilePane = 'browser';
      renderStudioSidebarTabs();
    });
  }
  bus.on('store:active', async () => {
    state.studioMobilePane = 'browser';
    state.executionLive.jobs = {};
    state.executionLive.feed = [];
    state.artifacts = [];
    state.artifactFiles = [];
    state.currentArtifact = null;
    state.currentFilePath = '';
    state.currentContent = '';
    state.lastSavedContent = state.currentContent;
    state.studioSidebarTab = 'artifacts';
    await loadArtifactBrowser();
    renderStudioHeader();
    renderStudioSidebarTabs();
    renderEditor();
    renderPreview();
    renderArtifactList();
    renderArtifactFiles();
  });
  window.Blackboard = window.Blackboard || {};
  window.Blackboard.openArtifactStudio = openArtifactStudio;
  window.Blackboard.openWorkspaceView = openWorkspaceView;
}
