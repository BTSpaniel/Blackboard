// Toolbar — project switcher, search, and a tiny pulse indicator that summarizes
// provider health. No "Refresh" button; the toolbar listens to the live
// `providers:snapshot` and `store:projects` events to repaint itself.
import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { openSettingsPanel } from '/ui/shell/settings.js';
import { promptDialog, toast } from '/ui/shell/dialog.js';
import { pickDirectory } from '/ui/shell/directory_picker.js';

function slugifyProjectName(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'project';
}

async function defaultProjectRoot(name) {
  try {
    const home = await api.fsHome();
    const workspace = home?.default_workspace || '';
    if (!workspace) return '';
    const sep = workspace.includes('\\') ? '\\' : '/';
    return workspace.endsWith(sep) ? `${workspace}${slugifyProjectName(name)}` : `${workspace}${sep}${slugifyProjectName(name)}`;
  } catch {
    return '';
  }
}

export function renderToolbar() {
  const select = document.getElementById('project-select');
  const newBtn = document.getElementById('new-project');
  const search = document.getElementById('board-search');
  const pulse = document.getElementById('provider-pulse');
  const pulseLabel = document.getElementById('provider-pulse-label');

  function paintProjects(projects) {
    const activeId = store.get().activeProjectId;
    select.innerHTML = '';
    for (const p of projects) {
      const opt = document.createElement('option');
      opt.value = p.project_id;
      opt.textContent = p.name;
      if (p.project_id === activeId) opt.selected = true;
      select.appendChild(opt);
    }
  }

  function paintHealth(profiles) {
    const total = profiles.length;
    const healthy = profiles.filter((p) => p.ok === true).length;
    const down = profiles.filter((p) => p.ok === false).length;
    const unknown = total - healthy - down;
    pulseLabel.textContent = `${healthy}/${total}`;
    pulse.classList.remove('bb-pulse--ok', 'bb-pulse--warn', 'bb-pulse--err', 'bb-pulse--unknown');
    if (total === 0) pulse.classList.add('bb-pulse--unknown');
    else if (down === 0 && unknown === 0) pulse.classList.add('bb-pulse--ok');
    else if (down > 0 && healthy > 0)     pulse.classList.add('bb-pulse--warn');
    else if (down > 0)                    pulse.classList.add('bb-pulse--err');
    else                                  pulse.classList.add('bb-pulse--unknown');
    pulse.title = `Providers: ${healthy} healthy · ${down} down · ${unknown} unknown — click to open Settings`;
  }

  // Click pulse to open the Settings dialog (which auto-refreshes while open).
  pulse.addEventListener('click', () => openSettingsPanel());

  select.addEventListener('change', async (e) => {
    const id = e.target.value;
    try {
      await api.switchProject(id);
      store.setActive(id);
    } catch (err) {
      toast(`Failed to switch project: ${err.message}`, { kind: 'error' });
    }
  });

  newBtn.addEventListener('click', async () => {
    const name = await promptDialog({ title: 'New project', label: 'Project name', placeholder: 'My Project' });
    if (!name) return;
    const suggestedRoot = await defaultProjectRoot(name);
    const root = await pickDirectory({
      initial: suggestedRoot,
      title: 'Choose project root',
      confirmLabel: 'Use this directory',
    });
    if (!root) return;
    try {
      const created = await api.createProject({ name, root });
      const projects = await api.projects();
      store.setProjects(projects);
      await api.switchProject(created.project_id);
      store.setActive(created.project_id);
      toast(`Project created: ${name}`, { kind: 'success' });
    } catch (err) {
      toast(`Failed to create project: ${err.message}`, { kind: 'error' });
    }
  });

  search.addEventListener('input', (e) => {
    bus.emit('board:filter', { query: e.target.value.toLowerCase() });
  });

  bus.on('store:projects', paintProjects);
  bus.on('store:providers', (payload) => paintHealth(payload.profiles || []));
  // Live updates from server health pings (no manual refresh needed).
  bus.on('ws:providers:snapshot', (payload) => {
    if (payload?.profiles) {
      store.setProviders(payload);  // also rebroadcasts via store:providers
      paintHealth(payload.profiles);
    }
  });
  bus.on('ws:providers:health', (payload) => {
    // Compact health-only update — fold into existing profiles list.
    const cur = store.get().providers?.profiles || [];
    if (!cur.length) return;
    for (const p of cur) {
      if (payload && payload[p.id]) {
        p.ok = payload[p.id].ok;
        p.latency_ms = payload[p.id].latency_ms;
        p.error = payload[p.id].error || '';
      }
    }
    store.setProviders({ ...(store.get().providers || {}), profiles: cur });
    paintHealth(cur);
  });
}
