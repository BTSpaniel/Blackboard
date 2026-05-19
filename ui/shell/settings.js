// Settings panel — autodetect-first controls for Models and provider Order.
// Auto-refreshes via WebSocket `providers:snapshot` events while open.
// No manual "Refresh" button; live data only.
import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { createDialog, toast } from '/ui/shell/dialog.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function healthBadge(p) {
  if (p.ok === true) return '<span class="bb-chip bb-chip--green">healthy</span>';
  if (p.ok === false) return '<span class="bb-chip bb-chip--red">down</span>';
  return '<span class="bb-chip">unknown</span>';
}

function keyChip(s) {
  if (!s) return '<span class="bb-key bb-key--none">—</span>';
  if (!s.required) return '<span class="bb-key bb-key--none" title="No API key required">no key</span>';
  if (s.has_value) {
    const sourceTag =
      s.source === 'inline'       ? 'inline (key_overrides.json)' :
      s.source === 'env'          ? `env: ${s.env}` :
      s.source === 'keyring'      ? 'OS keyring' :
      s.source === 'fallback_env' ? 'env (fallback)' : 'set';
    const label = s.source === 'inline' ? 'inline' : (s.env || 'set');
    return `<span class="bb-key bb-key--ok" title="Key source: ${escapeHtml(sourceTag)}">🔑 <code>${escapeHtml(label)}</code></span>`;
  }
  return `<span class="bb-key bb-key--miss" title="Set ${escapeHtml(s.env || s.secret_id)} to enable this provider">⚠ <code>${escapeHtml(s.env || s.secret_id)}</code></span>`;
}

async function handleSetModel(profileId, model) {
  const value = String(model || '').trim();
  if (!value) {
    toast('Model is required', { kind: 'warn' });
    return;
  }
  try {
    const profile = (store.get().providers?.profiles || []).find((p) => p.id === profileId);
    const models = Array.from(new Set([value, ...((profile?.models || []))].filter(Boolean)));
    await api.setProviderModel(profileId, value, models);
    toast(`Model set for ${profileId}`, { kind: 'success' });
  } catch (err) {
    toast(`Model update failed: ${err.message}`, { kind: 'error', timeout: 5000 });
  }
}

async function handleLoadModels(profileId, button) {
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = 'Loading…';
  }
  try {
    const result = await api.refreshProviderModels(profileId);
    toast(`Loaded ${(result.models || []).length} model(s) for ${profileId}`, { kind: 'success' });
  } catch (err) {
    toast(`Load models failed: ${err.message}`, { kind: 'error', timeout: 6000 });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function accessRequest(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const response = await fetch(path, opts);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(String(text || `${method} ${path} failed`).slice(0, 500));
  }
  if (response.status === 204 || response.status === 205) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function buildAccessApi() {
  return {
    settingsAccess: () =>
      typeof api.settingsAccess === 'function'
        ? api.settingsAccess()
        : api.settings().then((payload) => payload?.server_access || null),
    updateSettingsAccess: (body) =>
      typeof api.updateSettingsAccess === 'function'
        ? api.updateSettingsAccess(body)
        : accessRequest('PUT', '/api/settings/access', body),
    setRemoteAccessToken: (token) =>
      typeof api.setRemoteAccessToken === 'function'
        ? api.setRemoteAccessToken(token)
        : accessRequest('POST', '/api/settings/access/remote-token', { token }),
    clearRemoteAccessToken: () =>
      typeof api.clearRemoteAccessToken === 'function'
        ? api.clearRemoteAccessToken()
        : accessRequest('DELETE', '/api/settings/access/remote-token'),
    enableRemoteShare: () =>
      typeof api.enableRemoteShare === 'function'
        ? api.enableRemoteShare()
        : accessRequest('POST', '/api/settings/access/share/enable'),
    disableRemoteShare: () =>
      typeof api.disableRemoteShare === 'function'
        ? api.disableRemoteShare()
        : accessRequest('POST', '/api/settings/access/share/disable'),
    listRemoteShareInvites: () =>
      typeof api.listRemoteShareInvites === 'function'
        ? api.listRemoteShareInvites()
        : accessRequest('GET', '/api/settings/access/share/invites'),
    listRemoteShareAudit: (limit = 50) =>
      typeof api.listRemoteShareAudit === 'function'
        ? api.listRemoteShareAudit(limit)
        : accessRequest('GET', `/api/settings/access/share/audit?limit=${encodeURIComponent(limit)}`),
    accessProtection: (limit = 20) =>
      typeof api.accessProtection === 'function'
        ? api.accessProtection(limit)
        : accessRequest('GET', `/api/settings/access/protection?limit=${encodeURIComponent(limit)}`),
    createRemoteShareInvite: (body) =>
      typeof api.createRemoteShareInvite === 'function'
        ? api.createRemoteShareInvite(body)
        : accessRequest('POST', '/api/settings/access/share/invites', body),
    revokeRemoteShareInvite: (tokenId) =>
      typeof api.revokeRemoteShareInvite === 'function'
        ? api.revokeRemoteShareInvite(tokenId)
        : accessRequest('DELETE', `/api/settings/access/share/invites/${encodeURIComponent(tokenId)}`),
  };
}

export async function openSettingsPanel() {
  const dlg = createDialog({ title: 'Settings', size: 'xl' });
  dlg.overlay.classList.add('bb-dialog-overlay--settings');
  dlg.body.classList.add('bb-settings-dialog__body');
  dlg.body.innerHTML = `<div id="settings-pane" class="bb-settings-pane">${settingsSkeleton('providers')}</div>`;
  dlg.setFooter([
    { html: '<span class="bb-live" id="settings-live">live · instant WebSocket sync</span>' },
    { spacer: true },
    { label: 'Close', primary: true, onClick: () => dlg.close() },
  ]);

  let activeTab = 'providers';
  let providersData = store.get().providers || { profiles: [], roles: {} };
  let accessData = null;
  let codingData = null;
  const accessApi = buildAccessApi();
  let skillsData = null;
  let wikiData = null;
  let governorsData = null;
  let loadingProviders = !(providersData.profiles || []).length;
  let autoSyncedRoles = false;
  let roleSyncInFlight = false;

  const hasUsableKey = (p) => !p?.secret_status?.required || p?.secret_status?.has_value;
  const isHealthy = (p) => p?.ok === true;
  const isUsable = (p) => !!p?.available && hasUsableKey(p) && isHealthy(p);

  function setLive(text = 'live · instant WebSocket sync', tone = '') {
    const el = document.getElementById('settings-live');
    if (!el) return;
    el.textContent = text;
    el.style.color = tone === 'error' ? 'var(--c-red)' : '';
  }

  function providersSummaryText() {
    const profiles = providersData.profiles || [];
    const usable = profiles.filter(isUsable).length;
    const unavailable = Math.max(0, profiles.length - usable);
    return `
        Blackboard shows every configured provider so keys, models, and health can be managed even when a provider is offline.
        <strong style="color:var(--fg-1)">${usable}</strong> usable provider(s); ${unavailable} offline, missing keys, unavailable, or unhealthy.
      `;
  }

  function providerRenderSignature(p) {
    const s = p?.secret_status || {};
    return JSON.stringify({
      id: String(p?.id || ''),
      adapter: String(p?.adapter || ''),
      model: String(p?.model || ''),
      models: Array.isArray(p?.models) ? p.models.map((m) => String(m || '')) : [],
      keyRequired: !!s.required,
      keyValue: !!s.has_value,
      keySource: String(s.source || ''),
      keyEnv: String(s.env || ''),
      keySecret: String(s.secret_id || ''),
    });
  }

  function roleChain(assignment) {
    return [assignment?.profile, ...((assignment?.fallbacks || []))].filter(Boolean);
  }

  function canPatchProvidersSnapshot(prevProfiles, nextProfiles) {
    const prev = Array.isArray(prevProfiles) ? prevProfiles : [];
    const next = Array.isArray(nextProfiles) ? nextProfiles : [];
    if (prev.length !== next.length) return false;
    for (let i = 0; i < prev.length; i += 1) {
      if (providerRenderSignature(prev[i]) !== providerRenderSignature(next[i])) return false;
    }
    return true;
  }

  function canPatchRolesSnapshot(prevRoles, nextRoles) {
    const prevEntries = Object.entries(prevRoles || {});
    const nextEntries = Object.entries(nextRoles || {});
    if (prevEntries.length !== nextEntries.length) return false;
    for (let i = 0; i < prevEntries.length; i += 1) {
      const [prevRole, prevAssignment] = prevEntries[i];
      const [nextRole, nextAssignment] = nextEntries[i] || [];
      if (prevRole !== nextRole) return false;
      if (JSON.stringify(roleChain(prevAssignment)) !== JSON.stringify(roleChain(nextAssignment))) return false;
    }
    return true;
  }

  function patchProvidersDom(root) {
    if (!root) return false;
    const profiles = providersData.profiles || [];
    const summary = root.querySelector('[data-settings-providers-summary]');
    if (summary) summary.innerHTML = providersSummaryText();
    let patched = false;
    for (const p of profiles) {
      const row = root.querySelector(`[data-provider-row="${CSS.escape(String(p.id || ''))}"]`);
      if (!row) continue;
      const usableEl = row.querySelector('[data-provider-usable]');
      if (usableEl) usableEl.innerHTML = isUsable(p) ? '<span class="bb-chip bb-chip--green">usable</span>' : '<span class="bb-chip">offline/config</span>';
      const modelEl = row.querySelector('.bb-provider-model');
      if (modelEl && modelEl.value !== String(p.model || '')) modelEl.value = String(p.model || '');
      const healthEl = row.querySelector('[data-provider-health]');
      if (healthEl) {
        healthEl.innerHTML = `${healthBadge(p)}${p.latency_ms != null && p.ok ? `<span style="margin-left:6px;color:var(--fg-3);font-size:11px">${p.latency_ms} ms</span>` : ''}`;
      }
      patched = true;
    }
    return patched;
  }

  function patchRolesDom(root) {
    if (!root) return false;
    const roles = providersData.roles || {};
    const profileById = Object.fromEntries((providersData.profiles || []).map((p) => [p.id, p]));
    let patched = false;
    for (const [role, assignment] of Object.entries(roles)) {
      const section = root.querySelector(`.bb-role[data-role="${CSS.escape(String(role || ''))}"]`);
      if (!section) continue;
      const chain = [assignment.profile, ...(assignment.fallbacks || [])].filter(Boolean);
      const disabledSet = new Set(assignment.disabled || []);
      const activeChain = chain.filter((pid) => !disabledSet.has(pid) && isUsable(profileById[pid]));
      const hint = section.querySelector('[data-role-hint]');
      if (hint) hint.innerHTML = `${activeChain.length} usable · primary: <code>${escapeHtml(activeChain[0] || 'none')}</code>`;
      section.querySelectorAll('.bb-role__row').forEach((row) => {
        const pid = String(row.dataset.pid || '');
        const index = Number(row.dataset.idx);
        const profile = profileById[pid];
        const off = disabledSet.has(pid) || !isUsable(profile);
        const noKey = profile?.secret_status?.required && !profile?.secret_status?.has_value;
        const unavailable = !profile?.available || profile?.ok === false;
        row.classList.toggle('bb-role__row--off', off);
        row.title = noKey ? 'Missing API key' : unavailable ? 'Offline or unavailable' : '';
        const rank = row.querySelector('.bb-role__rank');
        if (rank) rank.textContent = off ? 'OFF' : (activeChain[0] === pid ? 'PRIMARY' : `#${activeChain.indexOf(pid) + 1}`);
        const pill = row.querySelector('.bb-role__pill');
        if (pill) {
          pill.classList.toggle('bb-role__pill--primary', activeChain[0] === pid);
          pill.title = `${pid}${noKey ? ' · API key missing' : ''}`;
          const dot = pill.querySelector('.bb-role__dot');
          if (dot) dot.style.background = profile?.ok === true ? '#22c55e' : profile?.ok === false ? '#ef4444' : 'var(--fg-3)';
          const model = pill.querySelector('.bb-role__model');
          if (model) model.textContent = profile?.model ? String(profile.model) : '';
          if (model) model.style.display = profile?.model ? '' : 'none';
          const warn = pill.querySelector('.bb-role__warn');
          if (warn) warn.style.display = noKey ? '' : 'none';
        }
        const up = row.querySelector('[data-act="up"]');
        if (up) up.disabled = index === 0;
        const down = row.querySelector('[data-act="down"]');
        if (down) down.disabled = index === chain.length - 1;
      });
      patched = true;
    }
    return patched;
  }

  function patchActiveTabDom() {
    const root = dlg.body.querySelector('#settings-pane');
    if (!root) return false;
    if (activeTab === 'providers') return patchProvidersDom(root);
    if (activeTab === 'roles') return patchRolesDom(root);
    return false;
  }

  function renderTab() {
    const root = dlg.body.querySelector('#settings-pane');
    if (!root) return;
    if ((activeTab === 'providers' || activeTab === 'roles' || activeTab === 'health') && loadingProviders) {
      root.innerHTML = settingsSkeleton(activeTab);
      return;
    }
    if (activeTab === 'access')    return renderAccess(root);
    if (activeTab === 'providers') return renderProviders(root);
    if (activeTab === 'roles')     return renderRoles(root);
    if (activeTab === 'skills')    return renderSkills(root);
    if (activeTab === 'wiki')      return renderWiki(root);
    if (activeTab === 'governors') return renderGovernors(root);
  }

  function settingsSkeleton(tab) {
    const rows = tab === 'roles'
      ? '<div class="bb-skeleton-card"></div><div class="bb-skeleton-card"></div><div class="bb-skeleton-card"></div>'
      : '<div class="bb-skeleton-row"></div><div class="bb-skeleton-row"></div><div class="bb-skeleton-row"></div><div class="bb-skeleton-row"></div><div class="bb-skeleton-row"></div>';
    const label = tab === 'providers' ? 'models' : tab === 'roles' ? 'order' : tab === 'access' ? 'access' : tab;
    return `
      <div class="bb-settings-skeleton" aria-busy="true">
        <div class="bb-section-title">${escapeHtml(label)} loading</div>
        <div class="bb-skeleton-line"></div>
        ${rows}
      </div>
    `;
  }

  function accessCell(label, content) {
    return `<td data-label="${escapeHtml(label)}">${content}</td>`;
  }

  function accessEmptyRow(colspan, text) {
    return `<tr><td colspan="${colspan}" class="bb-settings-access-table__empty">${escapeHtml(text)}</td></tr>`;
  }

  async function renderAccess(root) {
    if (!accessData || !codingData) {
      root.innerHTML = settingsSkeleton('access');
      try {
        const [nextAccess, nextCoding] = await Promise.all([
          accessApi.settingsAccess(),
          typeof api.settingsCoding === 'function' ? api.settingsCoding() : api.settings().then((payload) => payload?.coding || null),
        ]);
        accessData = nextAccess;
        codingData = nextCoding;
      } catch (err) {
        root.innerHTML = `<div class="bb-section-title">Access</div><p style="color:var(--c-red)">Failed to load access settings: ${escapeHtml(err.message)}</p>`;
        return;
      }
    }
    const remoteReady = accessData.remote_token_ready
      ? `<span class="bb-chip bb-chip--green">token ready${accessData.remote_token_source ? ` · ${escapeHtml(accessData.remote_token_source)}` : ''}</span>`
      : '<span class="bb-chip">token missing</span>';
    const remoteShare = accessData.remote_share || {};
    let protection = accessData.protection || {};
    const upnp = remoteShare.upnp || {};
    let inviteRows = '';
    let auditRows = '';
    let protectionRows = '';
    let protectionEventRows = '';
    try {
      const invitesPayload = await accessApi.listRemoteShareInvites().catch(() => ({ invites: [] }));
      const invites = Array.isArray(invitesPayload?.invites) ? invitesPayload.invites : [];
      inviteRows = invites.map((invite) => `
        <tr>
          ${accessCell('Invite', `<strong>${escapeHtml(invite.name || invite.token_id || '')}</strong><div style="margin-top:4px;color:var(--fg-3);font-size:11px">${escapeHtml(invite.token_id || '')}</div>`)}
          ${accessCell('Expires', invite.expires_at ? escapeHtml(new Date(invite.expires_at * 1000).toLocaleString()) : 'never')}
          ${accessCell('Last IP', escapeHtml(invite.remote_ip || '') || '<span style="color:var(--fg-3)">unused</span>')}
          ${accessCell('Actions', `
            <div class="bb-settings-access-actions">
              ${invite.join_url ? `<button class="bb-btn bb-btn--xs" data-act="copy-invite" data-invite-url="${escapeHtml(invite.join_url)}">Copy URL</button>` : ''}
              <button class="bb-btn bb-btn--xs" data-act="revoke-invite" data-token-id="${escapeHtml(invite.token_id || '')}">Revoke</button>
            </div>
            ${invite.join_url ? `<div class="bb-settings-access-inline-code"><code>${escapeHtml(invite.join_url)}</code></div>` : ''}
          `)}
        </tr>
      `).join('');
    } catch (err) {
      inviteRows = '';
    }
    try {
      const auditPayload = await accessApi.listRemoteShareAudit(12).catch(() => ({ events: [] }));
      const events = Array.isArray(auditPayload?.events) ? auditPayload.events : [];
      auditRows = events.map((event) => {
        const payload = event?.payload || {};
        const label = payload.reason || payload.token_id || payload.name || '';
        return `
          <tr>
            ${accessCell('Event', escapeHtml(event.event || ''))}
            ${accessCell('When', event.ts ? escapeHtml(new Date(event.ts * 1000).toLocaleString()) : '')}
            ${accessCell('Detail', escapeHtml(label))}
            ${accessCell('Outcome', escapeHtml(event.outcome || ''))}
          </tr>
        `;
      }).join('');
    } catch (err) {
      auditRows = '';
    }
    try {
      const protectionPayload = await accessApi.accessProtection(12).catch(() => protection || {});
      const subjects = Array.isArray(protectionPayload?.subjects) ? protectionPayload.subjects : [];
      const events = Array.isArray(protectionPayload?.events) ? protectionPayload.events : [];
      protectionRows = subjects.map((subject) => `
        <tr>
          ${accessCell('Client IP', `<code>${escapeHtml(subject.client_ip || '')}</code>`)}
          ${accessCell('Strikes', escapeHtml(String(subject.strikes ?? 0)))}
          ${accessCell('Cooldown', subject.cooldown_remaining_s ? escapeHtml(`${Math.ceil(Number(subject.cooldown_remaining_s || 0))}s`) : '<span style="color:var(--fg-3)">none</span>')}
          ${accessCell('Revokes', escapeHtml(String(subject.revoked_count ?? 0)))}
          ${accessCell('Last reason', escapeHtml(subject.last_reason || ''))}
        </tr>
      `).join('');
      protectionEventRows = events.map((event) => `
        <tr>
          ${accessCell('Event', escapeHtml(event.kind || ''))}
          ${accessCell('When', event.ts ? escapeHtml(new Date(event.ts * 1000).toLocaleString()) : '')}
          ${accessCell('Client', escapeHtml(event.client_ip || ''))}
          ${accessCell('Detail', escapeHtml(event.reason || event.outcome || ''))}
        </tr>
      `).join('');
      protection = protectionPayload || {};
      accessData.protection = protectionPayload;
    } catch (err) {
      protectionRows = '';
      protectionEventRows = '';
    }
    root.innerHTML = `
      <div class="bb-settings-access">
      <div class="bb-section-title">Access</div>
      <p style="color:var(--fg-3);font-size:11px;margin:0 0 12px">
        LAN mode binds Blackboard on your local network. Remote mode also binds publicly. You can use either a static remote token or secure invite-based remote share sessions.
        Restart Blackboard after changing bind mode or port-related settings.
      </p>
      <div class="bb-role">
        <div class="bb-settings-access-toggle-grid">
          <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bb-access-lan" ${accessData.lan_enabled ? 'checked' : ''} /> Enable LAN access</label>
          <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bb-access-remote" ${accessData.remote_enabled ? 'checked' : ''} /> Enable remote access</label>
        </div>
        <div style="margin-top:12px;display:grid;gap:10px">
          <label>
            <div style="font-size:12px;color:var(--fg-3);margin-bottom:4px">Public base URL</div>
            <input class="bb-input" id="bb-access-public-url" value="${escapeHtml(accessData.public_base_url || '')}" placeholder="https://your.domain.example" />
          </label>
          <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bb-access-forwarded" ${accessData.trust_forwarded_for ? 'checked' : ''} /> Trust X-Forwarded-For headers from your reverse proxy</label>
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="bb-btn" id="bb-access-save">Save access settings</button>
          ${accessData.restart_required ? '<span class="bb-chip">restart required</span>' : '<span class="bb-chip bb-chip--green">runtime matches config</span>'}
        </div>
      </div>
      <div class="bb-role" style="margin-top:12px">
        <header class="bb-role__head"><h4 class="bb-role__title">Coding jobs</h4></header>
        <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">
          Limit how many coding jobs Blackboard may run concurrently. The current runtime is <code>${escapeHtml(String(codingData.runtime_max_concurrent ?? 1))}</code>.
        </p>
        <div style="display:grid;gap:10px;max-width:320px">
          <label>
            <div style="font-size:12px;color:var(--fg-3);margin-bottom:4px">Max concurrent jobs</div>
            <input class="bb-input" id="bb-coding-max-concurrent" type="number" min="1" max="32" step="1" value="${escapeHtml(String(codingData.max_concurrent ?? 4))}" />
          </label>
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="bb-btn" id="bb-coding-save">Save coding settings</button>
          ${codingData.restart_required ? '<span class="bb-chip">restart required</span>' : '<span class="bb-chip bb-chip--green">runtime matches config</span>'}
        </div>
      </div>
      <div class="bb-role" style="margin-top:12px">
        <header class="bb-role__head"><h4 class="bb-role__title">Remote token</h4></header>
        <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">
          Env variable: <code>${escapeHtml(accessData.remote_token_env || 'BLACKBOARD_REMOTE_ACCESS_TOKEN')}</code>. Override status: ${remoteReady}.
          Remote browser sessions use cookie <code>${escapeHtml(accessData.remote_cookie_name || 'bb_remote_access')}</code> after first token-authenticated request.
        </p>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="bb-btn" id="bb-access-set-token">Set token override</button>
          <button class="bb-btn" id="bb-access-clear-token">Clear token override</button>
        </div>
      </div>
      <div class="bb-role" style="margin-top:12px">
        <header class="bb-role__head"><h4 class="bb-role__title">Remote share</h4></header>
        <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">
          Share status: ${remoteShare.enabled ? '<span class="bb-chip bb-chip--green">enabled</span>' : '<span class="bb-chip">disabled</span>'}
          ${upnp.available ? `<span class="bb-chip ${upnp.mapped ? 'bb-chip--green' : ''}">UPnP ${upnp.mapped ? 'mapped' : 'available'}</span>` : '<span class="bb-chip">UPnP unavailable</span>'}
        </p>
        <div class="bb-settings-access-actions" style="margin-bottom:10px">
          <button class="bb-btn" id="bb-share-enable">Enable share</button>
          <button class="bb-btn" id="bb-share-disable">Disable share</button>
          ${remoteShare.public_url ? `<span class="bb-settings-access-inline-code"><code>${escapeHtml(remoteShare.public_url)}</code></span>` : '<span style="color:var(--fg-3)">No public URL yet</span>'}
        </div>
        <ul style="color:var(--fg-3);font-size:11px;margin:0 0 10px 18px">
          <li>Cookie name: <code>${escapeHtml(remoteShare.cookie_name || 'bb_remote_share')}</code></li>
          <li>Secure cookie preferred: <code>${escapeHtml(String(!!remoteShare.secure_cookie_preferred))}</code></li>
          <li>Invite count: <code>${escapeHtml(remoteShare.invite_count ?? 0)}</code></li>
          <li>Remote sessions: <code>${escapeHtml(remoteShare.session_count ?? 0)}</code></li>
          <li>External IP: <code>${escapeHtml(upnp.external_ip || '(unknown)')}</code></li>
          <li>Mapped port: <code>${escapeHtml(upnp.mapped_port || '') || '(not mapped)'}</code></li>
          <li>Transport mode: <code>${escapeHtml(remoteShare.transport_mode || 'local_only')}</code></li>
        </ul>
        ${Array.isArray(remoteShare.guidance) && remoteShare.guidance.length ? `
          <ul style="color:var(--fg-3);font-size:11px;margin:0 0 10px 18px">
            ${remoteShare.guidance.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
          </ul>
        ` : ''}
        <div class="bb-settings-access-actions" style="margin-bottom:10px">
          <button class="bb-btn" id="bb-share-create-invite">Create invite link</button>
        </div>
        <table class="bb-data-table bb-settings-access-table">
          <thead><tr><th>Invite</th><th>Expires</th><th>Last IP</th><th>Actions</th></tr></thead>
          <tbody>${inviteRows || accessEmptyRow(4, 'No active invites.')}</tbody>
        </table>
        <div style="margin-top:12px;color:var(--fg-3);font-size:11px">Recent remote share audit events${remoteShare.audit_path ? ` · <code>${escapeHtml(remoteShare.audit_path)}</code>` : ''}</div>
        <table class="bb-data-table bb-settings-access-table" style="margin-top:8px">
          <thead><tr><th>Event</th><th>When</th><th>Detail</th><th>Outcome</th></tr></thead>
          <tbody>${auditRows || accessEmptyRow(4, 'No audit events yet.')}</tbody>
        </table>
      </div>
      <div class="bb-role" style="margin-top:12px">
        <header class="bb-role__head"><h4 class="bb-role__title">Runtime</h4></header>
        <ul style="color:var(--fg-3);font-size:11px;margin:0 0 0 18px">
          <li>Configured host: <code>${escapeHtml(accessData.host || '127.0.0.1')}</code></li>
          <li>Configured port: <code>${escapeHtml(accessData.port || '')}</code></li>
          <li>Effective bind host: <code>${escapeHtml(accessData.bind_host || '127.0.0.1')}</code></li>
          <li>Current runtime bind host: <code>${escapeHtml(accessData.runtime_bind_host || '127.0.0.1')}</code></li>
          <li>Local URL: <code>${escapeHtml(accessData.local_url || '')}</code></li>
          <li>Public base URL: <code>${escapeHtml(accessData.public_base_url || '(not set)')}</code></li>
        </ul>
      </div>
      <div class="bb-role" style="margin-top:12px">
        <header class="bb-role__head"><h4 class="bb-role__title">Protection</h4></header>
        <ul style="color:var(--fg-3);font-size:11px;margin:0 0 10px 18px">
          <li>Active cooldowns: <code>${escapeHtml(String(protection.active_cooldowns ?? 0))}</code></li>
          <li>Total revokes observed: <code>${escapeHtml(String(protection.revoked_total ?? 0))}</code></li>
          <li>Window: <code>${escapeHtml(String(protection.window_s ?? 0))}</code> seconds</li>
          <li>Cooldown: <code>${escapeHtml(String(protection.cooldown_s ?? 0))}</code> seconds</li>
        </ul>
        <table class="bb-data-table bb-settings-access-table">
          <thead><tr><th>Client IP</th><th>Strikes</th><th>Cooldown</th><th>Revokes</th><th>Last reason</th></tr></thead>
          <tbody>${protectionRows || accessEmptyRow(5, 'No protected clients yet.')}</tbody>
        </table>
        <div style="margin-top:12px;color:var(--fg-3);font-size:11px">Recent protection events</div>
        <table class="bb-data-table bb-settings-access-table" style="margin-top:8px">
          <thead><tr><th>Event</th><th>When</th><th>Client</th><th>Detail</th></tr></thead>
          <tbody>${protectionEventRows || accessEmptyRow(4, 'No protection events yet.')}</tbody>
        </table>
      </div>
      </div>
    `;

    root.querySelector('#bb-access-save')?.addEventListener('click', async () => {
      try {
        accessData = await accessApi.updateSettingsAccess({
          lan_enabled: !!root.querySelector('#bb-access-lan')?.checked,
          remote_enabled: !!root.querySelector('#bb-access-remote')?.checked,
          public_base_url: String(root.querySelector('#bb-access-public-url')?.value || '').trim(),
          trust_forwarded_for: !!root.querySelector('#bb-access-forwarded')?.checked,
        });
        setLive('live · access settings updated');
        toast('Access settings updated', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Access update failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-coding-save')?.addEventListener('click', async () => {
      const rawValue = root.querySelector('#bb-coding-max-concurrent')?.value;
      const maxConcurrent = Number.parseInt(String(rawValue || ''), 10);
      if (!Number.isFinite(maxConcurrent) || maxConcurrent < 1) {
        toast('Max concurrent jobs must be at least 1', { kind: 'warn' });
        return;
      }
      try {
        codingData = await api.updateSettingsCoding({ max_concurrent: maxConcurrent });
        setLive('live · coding settings updated');
        toast('Coding settings updated', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Coding settings update failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-access-set-token')?.addEventListener('click', async () => {
      const value = window.prompt('Set remote access token override');
      if (value == null) return;
      const trimmed = String(value || '').trim();
      if (!trimmed) {
        toast('Remote token is required', { kind: 'warn' });
        return;
      }
      try {
        accessData = await accessApi.setRemoteAccessToken(trimmed);
        setLive('live · remote token updated');
        toast('Remote token override updated', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Remote token update failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-access-clear-token')?.addEventListener('click', async () => {
      if (!window.confirm('Clear the persisted remote token override?')) return;
      try {
        accessData = await accessApi.clearRemoteAccessToken();
        setLive('live · remote token cleared');
        toast('Remote token override cleared', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Remote token clear failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-share-enable')?.addEventListener('click', async () => {
      try {
        accessData = await accessApi.enableRemoteShare();
        setLive('live · remote share enabled');
        toast('Remote share enabled', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Remote share enable failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-share-disable')?.addEventListener('click', async () => {
      if (!window.confirm('Disable remote share and revoke all active invite sessions?')) return;
      try {
        accessData = await accessApi.disableRemoteShare();
        setLive('live · remote share disabled');
        toast('Remote share disabled', { kind: 'success' });
        renderAccess(root);
      } catch (err) {
        toast(`Remote share disable failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelector('#bb-share-create-invite')?.addEventListener('click', async () => {
      const name = window.prompt('Invite display name', 'Remote User');
      if (name == null) return;
      const expiresRaw = window.prompt('Invite expiration in hours', '24');
      if (expiresRaw == null) return;
      const expiresHours = Math.max(1, Number.parseInt(String(expiresRaw || '24'), 10) || 24);
      try {
        const payload = await accessApi.createRemoteShareInvite({ name: String(name || '').trim() || 'Remote User', expires_hours: expiresHours });
        const invite = payload?.invite || {};
        setLive('live · remote invite created');
        const inviteUrl = invite.url || invite.join_url || '';
        if (inviteUrl) {
          try { await navigator.clipboard.writeText(inviteUrl); } catch {}
          toast('Invite created and copied to clipboard', { kind: 'success' });
        } else {
          toast('Invite created', { kind: 'success' });
        }
        accessData = await accessApi.settingsAccess();
        renderAccess(root);
      } catch (err) {
        toast(`Invite creation failed: ${err.message}`, { kind: 'error', timeout: 6000 });
      }
    });

    root.querySelectorAll('[data-act="revoke-invite"]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        const tokenId = btn.dataset.tokenId;
        if (!tokenId) return;
        if (!window.confirm(`Revoke invite ${tokenId}?`)) return;
        try {
          await accessApi.revokeRemoteShareInvite(tokenId);
          setLive('live · invite revoked');
          toast('Invite revoked', { kind: 'success' });
          accessData = await accessApi.settingsAccess();
          renderAccess(root);
        } catch (err) {
          toast(`Invite revoke failed: ${err.message}`, { kind: 'error', timeout: 6000 });
        }
      })
    );

    root.querySelectorAll('[data-act="copy-invite"]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        const inviteUrl = String(btn.dataset.inviteUrl || '').trim();
        if (!inviteUrl) return;
        try {
          await navigator.clipboard.writeText(inviteUrl);
          toast('Invite URL copied', { kind: 'success' });
        } catch (err) {
          toast('Failed to copy invite URL', { kind: 'error' });
        }
      })
    );
  }

  function renderProviders(root) {
    const profiles = providersData.profiles || [];
    const usable = profiles.filter(isUsable);
    const unavailable = profiles.length - usable.length;
    const rows = profiles.map((p) => {
      const s = p.secret_status || {};
      const models = Array.from(new Set([p.model, ...(p.models || [])].filter(Boolean)));
      const modelControl = models.length > 1
        ? `<select class="bb-input bb-provider-model" data-act="set-model" data-pid="${escapeHtml(p.id)}">${models.map((m) => `<option value="${escapeHtml(m)}" ${m === p.model ? 'selected' : ''}>${escapeHtml(m)}</option>`).join('')}</select>`
        : `<input class="bb-input bb-provider-model" data-act="model-input" data-pid="${escapeHtml(p.id)}" value="${escapeHtml(p.model || '')}" placeholder="model id" />`;
      const status = isUsable(p) ? '<span class="bb-chip bb-chip--green">usable</span>' : '<span class="bb-chip">offline/config</span>';
      return `
        <tr data-provider-row="${escapeHtml(p.id)}">
          <td><strong>${escapeHtml(p.id)}</strong><div style="margin-top:4px" data-provider-usable>${status}</div></td>
          <td>
            <div style="display:flex;gap:4px;align-items:center;min-width:260px">
              ${modelControl}
              <button class="bb-btn bb-btn--xs" data-act="save-model" data-pid="${escapeHtml(p.id)}" title="Use this model">Use</button>
              <button class="bb-btn bb-btn--xs" data-act="load-models" data-pid="${escapeHtml(p.id)}" title="Load models from this provider">Load</button>
            </div>
          </td>
          <td>${escapeHtml(p.adapter || '')}</td>
          <td>
            <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
              ${keyChip(s)}
              ${s.required ? `<button class="bb-btn bb-btn--xs" data-act="set-key" data-pid="${escapeHtml(p.id)}">Set key</button>` : ''}
              ${s.required && s.has_value ? `<button class="bb-btn bb-btn--xs" data-act="clear-key" data-pid="${escapeHtml(p.id)}">Clear</button>` : ''}
            </div>
          </td>
          <td data-provider-health>${healthBadge(p)}${p.latency_ms != null && p.ok ? `<span style="margin-left:6px;color:var(--fg-3);font-size:11px">${p.latency_ms} ms</span>` : ''}</td>
        </tr>
      `;
    }).join('');
    root.innerHTML = `
      <div class="bb-section-title">Models</div>
      <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px" data-settings-providers-summary>
        Blackboard shows every configured provider so keys, models, and health can be managed even when a provider is offline.
        <strong style="color:var(--fg-1)">${usable.length}</strong> usable provider(s); ${unavailable} offline, missing keys, unavailable, or unhealthy.
      </p>
      <table class="bb-data-table">
        <thead><tr><th>Provider</th><th>Model</th><th>Adapter</th><th>Key</th><th>Health</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" style="color:var(--fg-3);text-align:center;padding:14px">No providers configured.</td></tr>'}</tbody>
      </table>
    `;
    root.querySelectorAll('[data-act="set-model"]').forEach((sel) =>
      sel.addEventListener('change', () => handleSetModel(sel.dataset.pid, sel.value))
    );
    root.querySelectorAll('[data-act="save-model"]').forEach((btn) =>
      btn.addEventListener('click', () => {
        const pid = btn.dataset.pid;
        const control = Array.from(root.querySelectorAll('[data-pid][data-act="set-model"], [data-pid][data-act="model-input"]'))
          .find((el) => el.dataset.pid === pid);
        if (control) handleSetModel(pid, control.value);
      })
    );
    root.querySelectorAll('[data-act="load-models"]').forEach((btn) =>
      btn.addEventListener('click', () => handleLoadModels(btn.dataset.pid, btn))
    );
    root.querySelectorAll('[data-act="set-key"]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        const pid = btn.dataset.pid;
        const value = window.prompt(`Paste API key for ${pid}`);
        if (value == null) return;
        const trimmed = value.trim();
        if (!trimmed) {
          toast('API key is required', { kind: 'warn' });
          return;
        }
        try {
          await api.setProviderKey(pid, trimmed);
          providersData = await api.providers();
          store.setProviders(providersData);
          setLive(`live · key updated for ${pid}`);
          renderTab();
          toast(`Key updated for ${pid}`, { kind: 'success' });
        } catch (err) {
          toast(`Key update failed: ${err.message}`, { kind: 'error', timeout: 6000 });
        }
      })
    );
    root.querySelectorAll('[data-act="clear-key"]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        const pid = btn.dataset.pid;
        if (!window.confirm(`Clear inline key for ${pid}? Environment and keyring keys may still apply.`)) return;
        try {
          await api.clearProviderKey(pid);
          providersData = await api.providers();
          store.setProviders(providersData);
          setLive(`live · key cleared for ${pid}`);
          renderTab();
          toast(`Key cleared for ${pid}`, { kind: 'success' });
        } catch (err) {
          toast(`Key clear failed: ${err.message}`, { kind: 'error', timeout: 6000 });
        }
      })
    );
  }

  function renderRoles(root) {
    const roles = providersData.roles || {};
    const profileById = Object.fromEntries((providersData.profiles || []).map((p) => [p.id, p]));

    function profileChip(pid, kind) {
      const p = profileById[pid];
      const ok = p?.ok;
      const okDot = ok === true ? '#22c55e' : ok === false ? '#ef4444' : 'var(--fg-3)';
      const keyMissing = p?.secret_status?.required && !p?.secret_status?.has_value;
      return `
        <span class="bb-role__pill ${kind === 'primary' ? 'bb-role__pill--primary' : ''}"
              data-pid="${escapeHtml(pid)}" title="${escapeHtml(pid)}${keyMissing ? ' · API key missing' : ''}">
          <span class="bb-role__dot" style="background:${okDot}"></span>
          <span class="bb-role__pid">${escapeHtml(pid)}</span>
          ${p?.model ? `<span class="bb-role__model">${escapeHtml(p.model)}</span>` : ''}
          ${keyMissing ? '<span class="bb-role__warn" title="API key missing">⚠</span>' : ''}
        </span>
      `;
    }

    function roleBlock(role, a) {
      const chain = [a.profile, ...(a.fallbacks || [])].filter(Boolean);
      const disabledSet = new Set(a.disabled || []);
      const activeChain = chain.filter((pid) => !disabledSet.has(pid) && isUsable(profileById[pid]));
      const items = chain.map((pid, i) => {
        const off = disabledSet.has(pid) || !isUsable(profileById[pid]);
        const p = profileById[pid];
        const noKey = p?.secret_status?.required && !p?.secret_status?.has_value;
        const unavailable = !p?.available || p?.ok === false;
        return `
          <li class="bb-role__row ${off ? 'bb-role__row--off' : ''}" data-pid="${escapeHtml(pid)}" data-idx="${i}" title="${noKey ? 'Missing API key' : unavailable ? 'Offline or unavailable' : ''}">
            <span class="bb-role__rank">${off ? 'OFF' : (activeChain[0] === pid ? 'PRIMARY' : `#${activeChain.indexOf(pid) + 1}`)}</span>
            ${profileChip(pid, activeChain[0] === pid ? 'primary' : 'fallback')}
            <span class="bb-role__actions">
              <button class="bb-role__btn" data-act="up"   ${i === 0 ? 'disabled' : ''} title="Move up">↑</button>
              <button class="bb-role__btn" data-act="down" ${i === chain.length - 1 ? 'disabled' : ''} title="Move down">↓</button>
            </span>
          </li>
        `;
      }).join('');
      return `
        <section class="bb-role" data-role="${escapeHtml(role)}">
          <header class="bb-role__head">
            <h4 class="bb-role__title">${escapeHtml(role)}</h4>
            <span class="bb-role__hint" data-role-hint>${activeChain.length} usable · primary: <code>${escapeHtml(activeChain[0] || 'none')}</code></span>
          </header>
          <ol class="bb-role__list">${items || '<li class="bb-role__empty">No providers in this role. Set a key and wait for a healthy probe.</li>'}</ol>
        </section>
      `;
    }

    const sections = Object.entries(roles).map(([r, a]) => roleBlock(r, a)).join('');
    root.innerHTML = `
      <div class="bb-section-title">Provider order</div>
      <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">
        Roles auto-sync to usable providers only: instantiated, key-ready, and healthy.
        Unusable providers are added/removed automatically. Use <strong>↑/↓</strong> only to change priority order.
      </p>
      ${sections || '<div style="color:var(--fg-3);padding:14px;text-align:center">No roles configured.</div>'}
    `;

    // ── Wire up the controls ───────────────────────────────────
    root.querySelectorAll('.bb-role').forEach((section) => {
      const role = section.dataset.role;
      const current = roles[role] || { profile: '', fallbacks: [], disabled: [] };
      const currentChain = [current.profile, ...(current.fallbacks || [])].filter(Boolean);

      async function commit(newChain, newDisabled) {
        const cleanChain = Array.from(new Set((newChain || []).filter((pid) => profileById[pid])));
        if (!cleanChain.length) return;
        const chainSet = new Set(cleanChain);
        const disabled = Array.from(new Set([
          ...(newDisabled || []),
          ...cleanChain.filter((pid) => !isUsable(profileById[pid])),
        ])).filter((d) => chainSet.has(d));
        const [profile, ...fallbacks] = cleanChain;
        try {
          await api.updateRole(role, { profile, fallbacks, disabled });
          toast(`Role ${role} updated`, { kind: 'success', timeout: 1500 });
        } catch (err) {
          toast(`Role update failed: ${err.message}`, { kind: 'error' });
          renderTab();
        }
      }

      section.querySelectorAll('.bb-role__row').forEach((row) => {
        row.querySelector('[data-act="up"]')?.addEventListener('click', () => {
          const idx = Number(row.dataset.idx);
          const chain = [...currentChain];
          if (idx <= 0) return;
          [chain[idx - 1], chain[idx]] = [chain[idx], chain[idx - 1]];
          commit(chain);
        });
        row.querySelector('[data-act="down"]')?.addEventListener('click', () => {
          const idx = Number(row.dataset.idx);
          const chain = [...currentChain];
          if (idx >= chain.length - 1) return;
          [chain[idx + 1], chain[idx]] = [chain[idx], chain[idx + 1]];
          commit(chain);
        });
      });
    });
  }

  async function renderSkills(root) {
    root.innerHTML = '<div class="bb-section-title">Skills</div><div class="bb-skeleton-row"></div>';
    try {
      skillsData = await api.skillsList();
      const rows = (skillsData.skills || []).map((s) => `
        <tr>
          <td><strong>${escapeHtml(s.name)}</strong><div style="color:var(--fg-3);font-size:11px">${escapeHtml(s.source || '')} · priority ${escapeHtml(s.priority || 0)}</div></td>
          <td>${escapeHtml(s.description || '')}</td>
          <td>${(s.tags || []).map((t) => `<span class="bb-chip">${escapeHtml(t)}</span>`).join(' ')}</td>
        </tr>
      `).join('');
      root.innerHTML = `
        <div class="bb-section-title">Skills</div>
        <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">Available SKILL.md workflows are injected as metadata and loaded on demand with <code>skill_invoke</code>.</p>
        <table class="bb-data-table">
          <thead><tr><th>Name</th><th>Description</th><th>Tags</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="3" style="color:var(--fg-3);text-align:center;padding:14px">No skills found.</td></tr>'}</tbody>
        </table>
      `;
    } catch (err) {
      root.innerHTML = `<div class="bb-section-title">Skills</div><p style="color:var(--c-red)">Failed to load skills: ${escapeHtml(err.message)}</p>`;
    }
  }

  async function renderWiki(root) {
    root.innerHTML = '<div class="bb-section-title">Wiki</div><div class="bb-skeleton-row"></div>';
    try {
      const [stats, health, pages] = await Promise.all([api.wikiStats(), api.wikiHealth(), api.wikiPages()]);
      wikiData = { stats, health, pages };
      const rows = (pages || []).map((p) => `
        <tr>
          <td><strong>${escapeHtml(p.name)}</strong><div style="color:var(--fg-3);font-size:11px">${escapeHtml(p.path || '')}</div></td>
          <td>${escapeHtml(p.summary || '(no summary)')}</td>
        </tr>
      `).join('');
      root.innerHTML = `
        <div class="bb-section-title">Wiki</div>
        <p style="color:var(--fg-3);font-size:11px;margin:0 0 10px">
          ${Number(stats.total_pages || 0)} page(s), ${Number(stats.log_entries || 0)} log entries.
          Health: ${(health.broken_links || []).length} broken link(s), ${(health.orphans || []).length} orphan page(s).
        </p>
        <table class="bb-data-table">
          <thead><tr><th>Page</th><th>Summary</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="2" style="color:var(--fg-3);text-align:center;padding:14px">No wiki pages found.</td></tr>'}</tbody>
        </table>
      `;
    } catch (err) {
      root.innerHTML = `<div class="bb-section-title">Wiki</div><p style="color:var(--c-red)">Failed to load wiki: ${escapeHtml(err.message)}</p>`;
    }
  }

  async function renderGovernors(root) {
    root.innerHTML = '<div class="bb-section-title">Governors</div><div class="bb-skeleton-row"></div>';
    try {
      const [budget, health, capabilities, toolPolicy, trust, usage] = await Promise.all([
        api.governorBudget(),
        api.governorHealth(),
        api.governorCapabilities(),
        api.governorToolPolicy(),
        api.governorTrust(),
        api.costUsage(),
      ]);
      governorsData = { budget, health, capabilities, toolPolicy, trust, usage };
      const deniedTools = Object.entries(toolPolicy.entries || {})
        .filter(([, entry]) => entry.permission === 'deny')
        .map(([name, entry]) => `<li><code>${escapeHtml(name)}</code> — ${escapeHtml(entry.reason || 'denied')}</li>`)
        .join('');
      const usageTotals = usage?.totals || {};
      const providerUsageRows = Object.values(usage?.providers || {})
        .sort((a, b) => Number(b?.calls || 0) - Number(a?.calls || 0) || Number(b?.total_tokens || 0) - Number(a?.total_tokens || 0))
        .slice(0, 6)
        .map((entry) => `<li><code>${escapeHtml(entry.profile_id || 'unknown')}</code> — ${Number(entry.calls || 0)} call(s) · ${Number(entry.total_tokens || 0)} tokens${entry.last_model ? ` · model ${escapeHtml(entry.last_model)}` : ''}</li>`)
        .join('');
      const providerBudgetRows = Object.values(budget?.providers || {})
        .sort((a, b) => Number(b?.total_calls || 0) - Number(a?.total_calls || 0) || Number(b?.total_tokens || 0) - Number(a?.total_tokens || 0))
        .slice(0, 6)
        .map((entry) => `<li><code>${escapeHtml(entry.provider_id || 'unknown')}</code> — ${Number(entry.total_calls || 0)} call(s) · ${Number(entry.total_tokens || 0)} tokens · $${Number(entry.total_cost_usd || 0).toFixed(4)}</li>`)
        .join('');
      root.innerHTML = `
        <div class="bb-section-title">Governors</div>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Usage</h4></header>
          <p style="color:var(--fg-3);font-size:11px;margin:0">
            ${Number(usageTotals.total_tokens || 0)} tokens · ${Number(usageTotals.provider_calls || 0)} provider call(s) · ${Number(usageTotals.tool_calls || 0)} tool call(s) · ${Number(usageTotals.provider_profiles || 0)} provider profile(s)
          </p>
          <ul style="color:var(--fg-3);font-size:11px;margin:8px 0 0 18px">${providerUsageRows || '<li>none</li>'}</ul>
        </section>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Budget</h4></header>
          <p style="color:var(--fg-3);font-size:11px;margin:0">
            ${budget.enabled ? 'enabled' : 'disabled'} · ${Number(budget.total_tokens || 0)} tokens · $${Number(budget.total_cost_usd || 0).toFixed(4)} · ${Number(budget.total_calls || 0)} calls
          </p>
          <ul style="color:var(--fg-3);font-size:11px;margin:8px 0 0 18px">${providerBudgetRows || '<li>none</li>'}</ul>
        </section>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Health</h4></header>
          <p style="color:var(--fg-3);font-size:11px;margin:0">
            ${escapeHtml(health.status || 'unknown')} · open circuits: ${(health.open_circuits || []).map(escapeHtml).join(', ') || 'none'}
          </p>
        </section>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Capabilities</h4></header>
          <p style="color:var(--fg-3);font-size:11px;margin:0">Disabled: ${(capabilities.disabled || []).map(escapeHtml).join(', ') || 'none'}</p>
        </section>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Trust</h4></header>
          <p style="color:var(--fg-3);font-size:11px;margin:0">
            default: ${escapeHtml(trust.default_level_name || 'low')} · records: ${(trust.records || []).length} · active step-up: ${(trust.active_elevations || []).length}
          </p>
        </section>
        <section class="bb-role">
          <header class="bb-role__head"><h4 class="bb-role__title">Tool policy denies</h4></header>
          <ul style="color:var(--fg-3);font-size:11px;margin:0 0 0 18px">${deniedTools || '<li>none</li>'}</ul>
        </section>
      `;
    } catch (err) {
      root.innerHTML = `<div class="bb-section-title">Governors</div><p style="color:var(--c-red)">Failed to load governors: ${escapeHtml(err.message)}</p>`;
    }
  }

  // ── Live updates ────────────────────────────────────────────
  const onSnapshot = (payload) => {
    if (payload?.profiles) {
      const nextProvidersData = { profiles: payload.profiles, roles: payload.roles || providersData.roles };
      const patchProviders = activeTab === 'providers' && canPatchProvidersSnapshot(providersData.profiles, nextProvidersData.profiles);
      const patchRoles = activeTab === 'roles'
        && canPatchProvidersSnapshot(providersData.profiles, nextProvidersData.profiles)
        && canPatchRolesSnapshot(providersData.roles, nextProvidersData.roles);
      providersData = nextProvidersData;
      loadingProviders = false;
      store.setProviders(providersData);
      setLive(`live · updated ${new Date().toLocaleTimeString()}`);
      if (activeTab === 'providers' && patchProviders) {
        if (!patchActiveTabDom()) renderTab();
        return;
      }
      if (activeTab === 'roles' && patchRoles) {
        if (!patchActiveTabDom()) renderTab();
        return;
      }
      if (activeTab === 'providers' || activeTab === 'roles') renderTab();
    }
  };
  const onHealth = (payload) => {
    if (!providersData?.profiles) return;
    let touched = false;
    for (const p of providersData.profiles) {
      if (payload && payload[p.id]) {
        p.ok = payload[p.id].ok;
        p.latency_ms = payload[p.id].latency_ms;
        p.error = payload[p.id].error || '';
        touched = true;
      }
    }
    if (touched) {
      store.setProviders(providersData);
      setLive(`live · updated ${new Date().toLocaleTimeString()}`);
      if (activeTab === 'providers' || activeTab === 'roles') {
        if (!patchActiveTabDom()) renderTab();
      }
    }
  };
  bus.on('ws:providers:snapshot', onSnapshot);
  bus.on('ws:providers:health', onHealth);

  async function autoSyncUsableRoles() {
    if (autoSyncedRoles || roleSyncInFlight) return;
    autoSyncedRoles = true;
    roleSyncInFlight = true;
    try {
      const r = await api.autoFillAllRoles();
      const results = r.results || {};
      const ok = Object.values(results).filter((v) => !v.error).length;
      if (ok) {
        providersData = await api.providers();
        store.setProviders(providersData);
        setLive(`live · roles synced to usable providers`);
        renderTab();
      }
    } catch (err) {
      setLive(`live · role sync skipped: ${err.message}`, 'error');
    } finally {
      roleSyncInFlight = false;
    }
  }

  async function initialLoad() {
    try {
      const [profiles, healthMap, access, coding] = await Promise.all([
        api.providers(),
        api.providerHealth().catch(() => null),
        api.settingsAccess().catch(() => null),
        (typeof api.settingsCoding === 'function' ? api.settingsCoding() : api.settings().then((payload) => payload?.coding || null)).catch(() => null),
      ]);
      providersData = profiles;
      accessData = access;
      codingData = coding;
      if (healthMap) {
        for (const p of providersData.profiles) {
          if (healthMap[p.id]) {
            p.ok = healthMap[p.id].ok;
            p.latency_ms = healthMap[p.id].latency_ms;
            p.error = healthMap[p.id].error || '';
          }
        }
      }
      loadingProviders = false;
      store.setProviders(providersData);
      setLive(`live · ready`);
      renderTab();
      await autoSyncUsableRoles();
    } catch (err) {
      loadingProviders = false;
      setLive(`Load failed: ${err.message}`, 'error');
      renderTab();
    }
  }

  dlg.onClose(() => {
    bus.off('ws:providers:snapshot', onSnapshot);
    bus.off('ws:providers:health', onHealth);
  });

  dlg.setTabs(
    [
      { key: 'access',    label: 'Access', onActivate: () => { activeTab = 'access'; renderTab(); } },
      { key: 'providers', label: 'Models', onActivate: () => { activeTab = 'providers'; renderTab(); } },
      { key: 'roles',     label: 'Order',  onActivate: () => { activeTab = 'roles'; renderTab(); } },
      { key: 'skills',    label: 'Skills', onActivate: () => { activeTab = 'skills'; renderTab(); } },
      { key: 'wiki',      label: 'Wiki',   onActivate: () => { activeTab = 'wiki'; renderTab(); } },
      { key: 'governors', label: 'Governors', onActivate: () => { activeTab = 'governors'; renderTab(); } },
    ],
    activeTab,
  );
  dlg.open();
  renderTab();
  initialLoad();
}
