import { bus } from './bus.js';

// Thin REST client.
function errorMessageFromResponse(method, path, status, text) {
  let detail = text;
  try {
    const parsed = JSON.parse(text || '{}');
    if (typeof parsed.detail === 'string') detail = parsed.detail;
    else if (parsed.detail) detail = JSON.stringify(parsed.detail);
    else if (parsed.error) detail = String(parsed.error);
  } catch {
    detail = text;
  }
  detail = String(detail || '').trim() || 'Request failed without details';
  return `${method} ${path} → ${status}: ${detail.slice(0, 500)}`;
}

async function request(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(errorMessageFromResponse(method, path, r.status, text));
  }
  // 204 No Content / 205 Reset Content — bodies are intentionally empty, so
  // skip parsing entirely. (Without this, DELETE endpoints throw a JSON parse
  // error like "failed JSON" because r.json() chokes on an empty body.)
  if (r.status === 204 || r.status === 205) return null;
  const text = await r.text();
  if (!text) return null;
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    try { return JSON.parse(text); }
    catch (e) {
      throw new Error(`${method} ${path} returned invalid JSON: ${e.message}`);
    }
  }
  return text;
}

function emitStreamEvent(type, payload = {}) {
  const message = {
    topic: `chat.${type}`,
    payload,
    type,
    event_type: type,
    ts: Date.now() / 1000,
  };
  bus.emit(`ws:chat.${type}`, payload);
  bus.emit(`chat.${type}`, payload);
  bus.emit('ws:any', message);
}

async function streamRequest(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(errorMessageFromResponse('POST', path, response.status, text));
  }
  if (!response.body) throw new Error('Streaming response body unavailable');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalData = null;

  function processBlock(block) {
    const lines = block.split('\n');
    let eventType = 'message';
    const dataLines = [];
    for (const line of lines) {
      if (line.startsWith('event:')) eventType = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    let payload = {};
    try { payload = JSON.parse(dataLines.join('\n')); }
    catch { payload = { content: dataLines.join('\n') }; }
    payload.__stream_local = true;
    emitStreamEvent(eventType, payload);
    if (eventType === 'done') finalData = payload;
    if (eventType === 'error') throw new Error(payload.error || 'Streaming chat failed');
  }

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary = buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);
      if (block) processBlock(block);
      boundary = buffer.indexOf('\n\n');
    }
    if (done) break;
  }
  if (buffer.trim()) processBlock(buffer.trim());
  return finalData || {};
}

export const api = {
  providers:    () => request('GET', '/api/providers'),
  providerHealth: () => request('GET', '/api/providers/health'),
  settings:     () => request('GET', '/api/settings'),
  settingsCoding: () => request('GET', '/api/settings/coding'),
  updateSettingsCoding: (body) => request('PUT', '/api/settings/coding', body),
  settingsAccess: () => request('GET', '/api/settings/access'),
  updateSettingsAccess: (body) => request('PUT', '/api/settings/access', body),
  setRemoteAccessToken: (token) => request('POST', '/api/settings/access/remote-token', { token }),
  clearRemoteAccessToken: () => request('DELETE', '/api/settings/access/remote-token'),
  enableRemoteShare: () => request('POST', '/api/settings/access/share/enable'),
  disableRemoteShare: () => request('POST', '/api/settings/access/share/disable'),
  listRemoteShareInvites: () => request('GET', '/api/settings/access/share/invites'),
  listRemoteShareAudit: (limit = 50) => request('GET', `/api/settings/access/share/audit?limit=${encodeURIComponent(limit)}`),
  accessProtection: (limit = 20) => request('GET', `/api/settings/access/protection?limit=${encodeURIComponent(limit)}`),
  createRemoteShareInvite: (body) => request('POST', '/api/settings/access/share/invites', body),
  revokeRemoteShareInvite: (tokenId) => request('DELETE', `/api/settings/access/share/invites/${encodeURIComponent(tokenId)}`),

  projects:     () => request('GET', '/api/projects'),
  activeProject: () => request('GET', '/api/projects/active'),
  createProject: (body) => request('POST', '/api/projects', body),
  projectAgents: (id, { includeContent = false } = {}) =>
                   request('GET', `/api/projects/${id}/agents?include_content=${includeContent}`),
  switchProject: (id) => request('POST', `/api/projects/${id}/switch`),

  board:         (id) => request('GET',  `/api/board/${id}`),
  boardCards:    (id, { query = '', status = '', limit = 20 } = {}) =>
                    request('GET', `/api/board/${id}/cards?query=${encodeURIComponent(query)}&status=${encodeURIComponent(status)}&limit=${encodeURIComponent(limit)}`),
  boardCard:     (pid, cid) => request('GET', `/api/board/${pid}/cards/${cid}`),
  createCard:    (id, body) => request('POST', `/api/board/${id}/cards`, body),
  updateCard:    (pid, cid, body) => request('PUT', `/api/board/${pid}/cards/${cid}`, body),
  deleteCard:    (pid, cid) => request('DELETE', `/api/board/${pid}/cards/${cid}`),

  chat:          (body) => request('POST', '/api/chat', body),
  chatStream:    (body) => streamRequest('/api/chat/stream', body),
  registerChatAttachments: (projectId, sessionId, messageId, body) =>
                  request('POST', `/api/chat/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(messageId)}`, body),

  // Chat session reload / management (Luna-style).
  chatSessions:        (projectId) => request('GET', `/api/chat/${encodeURIComponent(projectId)}/sessions`),
  chatHistory:         (projectId, sessionId, limit = 200) =>
                          request('GET', `/api/chat/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/history?limit=${limit}`),
  chatDeleteSession:   (projectId, sessionId) =>
                          request('DELETE', `/api/chat/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`),
  chatClearHistory:    (projectId, sessionId) =>
                          request('DELETE', `/api/chat/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/history`),

  listJobs:      () => request('GET', '/api/coding/jobs'),
  submitJob:     (body) => request('POST', '/api/coding/jobs', body),
  cancelJob:     (id) => request('POST', `/api/coding/jobs/${id}/cancel`),
  resumeJob:     (id) => request('POST', `/api/coding/jobs/${id}/resume`),
  mergeJob:      (id, body) => request('POST', `/api/coding/jobs/${id}/merge`, body),
  executeSync:   (body) => request('POST', '/api/coding/execute', body),

  audit:         (pid, limit=100) => request('GET', `/api/audit/${encodeURIComponent(pid)}?limit=${limit}`),

  renderHtml:    (pid, body) => request('POST', `/api/artifacts/${pid}/render`, body),
  artifactCreate: (pid, body) => request('POST', `/api/artifacts/${encodeURIComponent(pid)}/library`, body),
  artifactResolve: (pid, body) => request('POST', `/api/artifacts/${encodeURIComponent(pid)}/library/resolve`, body),
  artifactList:   (pid) => request('GET', `/api/artifacts/${encodeURIComponent(pid)}/library`),
  artifactGet:    (pid, artifactId) => request('GET', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}`),
  artifactUpdate: (pid, artifactId, body) => request('PUT', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}`, body),
  artifactDelete: (pid, artifactId) => request('DELETE', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}`),
  artifactFiles:  (pid, artifactId) => request('GET', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}/files`),
  artifactFileGet: (pid, artifactId, path='') => request('GET', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}/files/content?path=${encodeURIComponent(path)}`),
  artifactFilePut: (pid, artifactId, body) => request('PUT', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}/files/content`, body),
  artifactFolderCreate: (pid, artifactId, body) => request('POST', `/api/artifacts/${encodeURIComponent(pid)}/library/${encodeURIComponent(artifactId)}/folders`, body),

  // Execution layer.
  terminalCreate: (body) => request('POST', '/api/terminal', body),
  terminalClose:  (id) => request('DELETE', `/api/terminal/${id}`),
  previewStatus:  (pid) => request('GET',  `/api/preview/${pid}`),
  previewStart:   (pid, body) => request('POST', `/api/preview/${pid}`, body),
  previewStop:    (pid) => request('DELETE', `/api/preview/${pid}`),
  screenshot:     (body) => request('POST', '/api/playwright/screenshot', body),

  costUsage:      () => request('GET', '/api/usage'),

  // File-system browse (used by directory picker).
  listDir:        (directory='', { onlyDirs=true, showHidden=false } = {}) =>
                   request('GET', `/api/files/list?directory=${encodeURIComponent(directory)}&only_dirs=${onlyDirs}&show_hidden=${showHidden}`),
  fsRead:         (path) => request('GET', `/api/files/read?path=${encodeURIComponent(path)}`),
  fsWrite:        (path, content) => request('POST', '/api/files/write', { path, content }),
  fsHome:         () => request('GET', '/api/files/home'),
  fsMkdir:        (parent, name) => request('POST', '/api/files/mkdir', { parent, name }),
  fsProbe:        (directory) => request('GET', `/api/files/probe?directory=${encodeURIComponent(directory)}`),
  fsFavorites:    () => request('GET', '/api/files/favorites'),
  fsAddFavorite:  (path, label='') => request('POST', '/api/files/favorites', { path, label }),
  fsRemoveFavorite: (path) => request('DELETE', `/api/files/favorites?path=${encodeURIComponent(path)}`),

  // Role priority editing.
  updateRole:     (role, body) => request('PUT', `/api/providers/roles/${encodeURIComponent(role)}`, body),
  autoFillRole:   (role) => request('POST', `/api/providers/roles/${encodeURIComponent(role)}/auto-fill`),
  autoFillAllRoles: () => request('POST', '/api/providers/roles/auto-fill-all'),
  resetRoleOverride: (role) => request('DELETE', `/api/providers/roles/${encodeURIComponent(role)}/override`),

  // Per-profile API key set/clear (no YAML edit, persisted to data/providers/key_overrides.json).
  setProviderKey:   (id, value) => request('POST', `/api/providers/${encodeURIComponent(id)}/key`, { value }),
  clearProviderKey: (id) => request('DELETE', `/api/providers/${encodeURIComponent(id)}/key`),
  setProviderModel: (id, model, models = []) => request('POST', `/api/providers/${encodeURIComponent(id)}/model`, { model, models }),
  refreshProviderModels: (id) => request('POST', `/api/providers/${encodeURIComponent(id)}/models/refresh`),

  // Skills and wiki management.
  skillsList:    () => request('GET', '/api/skills/list'),
  skillDetail:   (name) => request('GET', `/api/skills/detail/${encodeURIComponent(name)}`),
  skillsSuggest: (query, limit = 5) => request('POST', '/api/skills/suggest', { query, limit }),
  wikiStats:     () => request('GET', '/api/wiki/stats'),
  wikiHealth:    () => request('GET', '/api/wiki/health'),
  wikiPages:     () => request('GET', '/api/wiki/pages'),
  wikiRead:      (page) => request('GET', `/api/wiki/page/${encodeURIComponent(page)}`),
  wikiSearch:    (query, max_results = 6) => request('POST', '/api/wiki/search', { query, max_results }),
  wikiWrite:     (page, content, source = 'ui') => request('POST', '/api/wiki/page', { page, content, source }),
  governorBudget: () => request('GET', '/api/governors/budget'),
  governorHealth: () => request('GET', '/api/governors/health'),
  governorCapabilities: () => request('GET', '/api/governors/capabilities'),
  governorToolPolicy: () => request('GET', '/api/governors/tool-policy'),
  governorTrust: () => request('GET', '/api/governors/trust'),

  // Version control (data/.git timeline).
  vcsStatus:      ({ scope = 'data', projectId = '' } = {}) => request('GET', `/api/versioning/status?scope=${encodeURIComponent(scope)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ''}`),
  vcsHistory:     ({ path = '', limit = 100, scope = 'data', projectId = '' } = {}) => request('GET', `/api/versioning/history?limit=${limit}&scope=${encodeURIComponent(scope)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ''}${path ? `&path=${encodeURIComponent(path)}` : ''}`),
  vcsDiff:        (sha, { scope = 'data', projectId = '' } = {}) => request('GET', `/api/versioning/diff?sha=${encodeURIComponent(sha)}&scope=${encodeURIComponent(scope)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ''}`),
  vcsSyncCheckpoints: ({ limit = 100, projectId = '', cardId = '', cwd = '' } = {}) => request('GET', `/api/versioning/sync-checkpoints?limit=${limit}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ''}${cardId ? `&card_id=${encodeURIComponent(cardId)}` : ''}${cwd ? `&cwd=${encodeURIComponent(cwd)}` : ''}`),
  vcsRestoreCheckpoint: (checkpointId, { files = [], reason = '' } = {}) => request('POST', `/api/versioning/sync-checkpoints/${encodeURIComponent(checkpointId)}/restore`, { files, reason }),
  vcsRollback:    (sha, mode = 'revert', { scope = 'data', projectId = '' } = {}) => request('POST', '/api/versioning/rollback', { sha, mode, scope, project_id: projectId }),
  vcsCheckpoint:  (message, { scope = 'data', projectId = '', paths = [] } = {}) => request('POST', '/api/versioning/checkpoint', { message, scope, project_id: projectId, paths }),
  vcsTag:         (name, sha = null, message = '', { scope = 'data', projectId = '' } = {}) => request('POST', '/api/versioning/tag', { name, sha, message, scope, project_id: projectId }),
  vcsTags:        ({ scope = 'data', projectId = '' } = {}) => request('GET', `/api/versioning/tags?scope=${encodeURIComponent(scope)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ''}`),
};

window.Blackboard = window.Blackboard || {};
window.Blackboard.api = api;
