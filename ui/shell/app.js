// Blackboard UI boot.
import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import '/ui/core/ws.js';

import { renderSidebar } from '/ui/shell/sidebar.js';
import { openNewCardDialog, renderBoard } from '/ui/shell/board.js';
import { renderExecution } from '/ui/shell/execution.js';
import { renderToolbar } from '/ui/shell/toolbar.js';
import { openCardModal } from '/ui/shell/card_modal.js';
import { openDiffModal } from '/ui/shell/diff.js';
import { openAuditPanel } from '/ui/shell/audit.js';
import { openSettingsPanel } from '/ui/shell/settings.js';
import { renderArtifactsInto } from '/ui/shell/artifacts.js';
import { openPreviewPanel } from '/ui/shell/preview.js';
import { openTerminalPanel } from '/ui/shell/terminal.js';
import { openHistoryPanel } from '/ui/shell/history.js';
import { renderWorkspace } from '/ui/shell/workspace.js';
import { initSidebarResize, refitSidebar } from '/ui/shell/sidebar_resize.js';

const MOBILE_BREAKPOINT_PX = 900;
let mobilePanel = 'chat';

function appShell() {
  return document.getElementById('app');
}

function isMobileViewport() {
  return window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`).matches;
}

function applyMobilePanel(panel = 'chat') {
  mobilePanel = panel;
  const app = appShell();
  if (app) app.dataset.mobilePanel = panel;
  if (isMobileViewport()) {
    const sidebar = document.querySelector('.bb-sidebar');
    const workspace = document.querySelector('.bb-workspace');
    const exec = document.querySelector('.bb-exec');
    if (sidebar) sidebar.style.display = panel === 'chat' ? 'flex' : 'none';
    if (workspace) workspace.style.display = (panel === 'board' || panel === 'artifacts') ? 'flex' : 'none';
    if (exec) exec.style.display = panel === 'console' ? 'flex' : 'none';
  }
  for (const button of Array.from(document.querySelectorAll('[data-mobile-nav]'))) {
    const active = button.dataset.mobileNav === panel;
    button.classList.toggle('bb-mobile__nav-item--active', active);
    button.classList.toggle('bb-mobile__drawer-link--active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
}

function setMobileDrawerOpen(open) {
  const app = appShell();
  const drawer = document.getElementById('mobile-drawer');
  const scrim = document.getElementById('mobile-drawer-scrim');
  const toggle = document.getElementById('mobile-menu-toggle');
  const expanded = Boolean(open);
  if (app) {
    if (expanded) app.dataset.mobileDrawer = '1';
    else delete app.dataset.mobileDrawer;
  }
  if (drawer) drawer.setAttribute('aria-hidden', expanded ? 'false' : 'true');
  if (scrim) scrim.hidden = !expanded;
  if (toggle) toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
}

async function focusMobilePanel(panel) {
  if (panel === 'chat') {
    applyMobilePanel('chat');
    const input = document.getElementById('chat-input');
    window.setTimeout(() => input?.focus(), 20);
    return;
  }
  if (panel === 'board') {
    applyMobilePanel('board');
    await window.Blackboard?.openWorkspaceView?.('board');
    return;
  }
  if (panel === 'artifacts') {
    applyMobilePanel('artifacts');
    await window.Blackboard?.openWorkspaceView?.('studio');
    return;
  }
  if (panel === 'console') {
    applyMobilePanel('console');
    const collapsed = document.getElementById('app')?.dataset?.execCollapsed === '1';
    if (collapsed) {
      document.querySelector('[data-exec-toggle="1"]')?.click();
    }
  }
}

async function routeView(view) {
  if (view === 'board') {
    if (isMobileViewport()) {
      await focusMobilePanel('board');
    } else {
      await window.Blackboard?.openWorkspaceView?.('board');
    }
    return;
  }
  if (view === 'preview') {
    openPreviewPanel();
    return;
  }
  if (view === 'terminal') {
    openTerminalPanel();
    return;
  }
  if (view === 'audit') {
    openAuditPanel();
    return;
  }
  if (view === 'history') {
    openHistoryPanel();
    return;
  }
  if (view === 'settings') {
    openSettingsPanel();
  }
}

function bindMobileShell() {
  const toggle = document.getElementById('mobile-menu-toggle');
  const close = document.getElementById('mobile-drawer-close');
  const scrim = document.getElementById('mobile-drawer-scrim');
  const newCard = document.getElementById('mobile-new-card');
  const focusChat = document.getElementById('mobile-focus-chat');

  if (toggle && toggle.dataset.bound !== '1') {
    toggle.dataset.bound = '1';
    toggle.addEventListener('click', () => {
      const open = appShell()?.dataset?.mobileDrawer === '1';
      setMobileDrawerOpen(!open);
    });
  }
  if (close && close.dataset.bound !== '1') {
    close.dataset.bound = '1';
    close.addEventListener('click', () => setMobileDrawerOpen(false));
  }
  if (scrim && scrim.dataset.bound !== '1') {
    scrim.dataset.bound = '1';
    scrim.addEventListener('click', () => setMobileDrawerOpen(false));
  }
  if (newCard && newCard.dataset.bound !== '1') {
    newCard.dataset.bound = '1';
    newCard.addEventListener('click', () => {
      setMobileDrawerOpen(false);
      openNewCardDialog();
    });
  }
  if (focusChat && focusChat.dataset.bound !== '1') {
    focusChat.dataset.bound = '1';
    focusChat.addEventListener('click', async () => {
      setMobileDrawerOpen(false);
      await focusMobilePanel('chat');
    });
  }
  for (const button of Array.from(document.querySelectorAll('[data-mobile-nav]'))) {
    if (button.dataset.bound === '1') continue;
    button.dataset.bound = '1';
    button.addEventListener('click', async () => {
      setMobileDrawerOpen(false);
      await focusMobilePanel(button.dataset.mobileNav || 'chat');
    });
  }
  for (const button of Array.from(document.querySelectorAll('#mobile-drawer [data-view]'))) {
    if (button.dataset.bound === '1') continue;
    button.dataset.bound = '1';
    button.addEventListener('click', async () => {
      setMobileDrawerOpen(false);
      await routeView(button.dataset.view || 'board');
    });
  }
}

function syncViewportMode() {
  const app = appShell();
  if (!app) return;
  if (isMobileViewport()) {
    if (!app.dataset.mobileMode) {
      const activeWorkspace = document.getElementById('workspace-shell')?.dataset?.activeWorkspaceView || '';
      if ((activeWorkspace === 'board' || activeWorkspace === 'studio') && mobilePanel === 'chat') {
        mobilePanel = activeWorkspace === 'studio' ? 'artifacts' : 'board';
      }
    }
    app.dataset.mobileMode = '1';
    applyMobilePanel(mobilePanel || 'chat');
  } else {
    delete app.dataset.mobileMode;
    delete app.dataset.mobilePanel;
    for (const selector of ['.bb-sidebar', '.bb-workspace', '.bb-exec']) {
      const element = document.querySelector(selector);
      if (element) element.style.display = '';
    }
    setMobileDrawerOpen(false);
  }
}

function bindViewportModeSync() {
  if (window.__bbMobileViewportSyncBound === true) return;
  window.__bbMobileViewportSyncBound = true;
  const sync = () => syncViewportMode();
  window.addEventListener('resize', sync);
  window.addEventListener('orientationchange', sync);
  window.visualViewport?.addEventListener?.('resize', sync);
}

async function bootstrap() {
  renderToolbar();
  renderSidebar();
  renderWorkspace();
  renderExecution();
  initSidebarResize();
  bindMobileShell();
  bindViewportModeSync();
  syncViewportMode();

  // Load providers + projects in parallel.
  try {
    const [providers, projects, active] = await Promise.all([
      api.providers(),
      api.projects(),
      api.activeProject(),
    ]);
    store.setProviders(providers);
    store.setProjects(projects);

    let activeId = active.project_id;
    if (!activeId && projects.length > 0) {
      activeId = projects[0].project_id;
      await api.switchProject(activeId);
    }
    if (activeId) {
      store.setActive(activeId);
      await loadBoardFor(activeId);
    } else {
      // No projects yet — auto-create a sandbox project.
      const created = await api.createProject({ name: 'Sandbox', root: '.' });
      store.setProjects(await api.projects());
      store.setActive(created.project_id);
      await api.switchProject(created.project_id);
      await loadBoardFor(created.project_id);
    }
    await refreshJobs();
  } catch (err) {
    console.error('[boot] failed:', err);
    bus.emit('app:toast', { kind: 'error', text: String(err) });
  }
}

async function loadBoardFor(projectId) {
  const snapshot = await api.board(projectId);
  store.setBoard(snapshot);
  renderBoard();
  if (isMobileViewport()) applyMobilePanel(mobilePanel || 'board');
}

async function refreshJobs() {
  try {
    const jobs = await api.listJobs();
    store.setJobs(jobs);
    renderExecution();
    if (isMobileViewport()) applyMobilePanel(mobilePanel || 'board');
  } catch (err) {
    // non-fatal during boot if coding service is unavailable
  }
}

// WS event glue.
bus.on('ws:board:card.created', () => reloadCurrentBoard());
bus.on('ws:board:card.updated', () => reloadCurrentBoard());
bus.on('ws:board:card.deleted', () => reloadCurrentBoard());
bus.on('ws:coding:job.created',   refreshJobs);
bus.on('ws:coding:job.started',   refreshJobs);
bus.on('ws:coding:job.paused',    refreshJobs);
bus.on('ws:coding:job.reviewing', refreshJobs);
bus.on('ws:coding:job.completed', refreshJobs);
bus.on('ws:coding:job.failed',    refreshJobs);
bus.on('ws:coding:job.merged',    refreshJobs);

bus.on('store:active', (id) => { if (id) loadBoardFor(id); });

async function reloadCurrentBoard() {
  const id = store.get().activeProjectId;
  if (id) await loadBoardFor(id);
}

window.Blackboard = window.Blackboard || {};
window.Blackboard.openCardModal = openCardModal;
window.Blackboard.openDiffModal = openDiffModal;
window.Blackboard.openAuditPanel = openAuditPanel;
window.Blackboard.openSettingsPanel = openSettingsPanel;
window.Blackboard.renderArtifactsInto = renderArtifactsInto;
window.Blackboard.reload = reloadCurrentBoard;

// Wire rail buttons.
document.querySelectorAll('.bb-rail__item').forEach((btn) => {
  const view = btn.dataset.view;
  btn.addEventListener('click', async () => {
    document.querySelectorAll('.bb-rail__item').forEach((b) => b.classList.remove('bb-rail__item--active'));
    btn.classList.add('bb-rail__item--active');
    await routeView(view || 'board');
  });
});

// Banner toasts on important bus events.
bus.on('ws:card:receipt.written', (p) => {
  console.log(`[blackboard] receipt written for card ${p?.card_id}`);
});

// Recompute the chat panel's "ideal width" hint whenever the board or jobs
// change. The hint pill in the sidebar header lets the user click to apply it;
// the sidebar never auto-resizes against the user's manual choice.
bus.on('store:board', () => refitSidebar());
bus.on('store:jobs',  () => refitSidebar());
bus.on('ws:board:card.created', () => refitSidebar());
bus.on('ws:board:card.updated', () => refitSidebar());
bus.on('ws:board:card.deleted', () => refitSidebar());
window.addEventListener('resize', syncViewportMode);

// Track previous provider health so we only log STATE TRANSITIONS — not every
// 15-second poll. Without this we get a flood of identical "unhealthy" warnings
// in the browser console for any provider that's chronically down (e.g. local
// llama server not running, claude CLI not installed).
const _providerHealthState = new Map();  // id -> bool ok
bus.on('ws:providers:health', (payload) => {
  for (const [id, h] of Object.entries(payload || {})) {
    const ok = !!h?.ok;
    const prev = _providerHealthState.get(id);
    if (prev === undefined) {
      // First sample for this provider — record but stay silent. The toolbar
      // already shows a red dot, so the user already knows.
      _providerHealthState.set(id, ok);
      continue;
    }
    if (prev === ok) continue;  // no transition
    _providerHealthState.set(id, ok);
    if (ok) console.info(`[provider:${id}] recovered`);
    else console.warn(`[provider:${id}] became unhealthy: ${h?.error || ''}`);
  }
});

bootstrap();
