// Preview panel — luna-code style: URL bar + reload + open-in-tab + cwd dir-picker.
import { api } from '/ui/core/api.js';
import { store } from '/ui/core/store.js';
import { createDialog } from '/ui/shell/dialog.js';
import { pickDirectory } from '/ui/shell/directory_picker.js';
import { toast } from '/ui/shell/dialog.js';
import { resolvePreferredWorkingDir, saveWorkingDir } from '/ui/shell/working_directory.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

const RUNNERS = [
  { value: 'python', label: 'python -m http.server (built-in static)' },
  { value: 'vite',   label: 'npx vite (Vite dev server)' },
  { value: 'next',   label: 'npx next dev (Next.js)' },
  { value: 'node',   label: 'npm run dev (package.json scripts.dev)' },
];

/** Sanitize a project name into a safe directory segment for filesystem lookups. */
function slugifyProjectName(name) {
  if (!name) return '';
  return String(name).trim().replace(/[\\/:*?"<>|]/g, '').replace(/\s+/g, '-').slice(0, 64);
}

/**
 * Resolve a sensible default working directory for a project preview.
 *
 * Priority:
 *   1. The project's own ``root`` if it's a real, non-trivial path
 *   2. ``<workspace>/<project-name>`` if that folder exists (project-scoped dir)
 *   3. The server's default workspace directory
 *   4. ``.`` as last resort (will fail at start, but keeps the UI safe)
 *
 * Returns ``{ path, label }`` where ``label`` describes which branch was used so
 * the UI can display a small "(project root)" / "(workspace)" hint.
 */
async function resolveProjectCwd(project) {
  const raw = project?.root || '';
  const trivial = !raw || raw === '.' || raw === './' || raw === '/';
  if (!trivial) return { path: raw, label: 'project root' };

  // Need workspace info from the server.
  let home;
  try { home = await api.fsHome(); }
  catch { return { path: '.', label: '' }; }

  const ws = home?.default_workspace || '';
  if (!ws) return { path: '.', label: '' };

  // Check whether <workspace>/<slug> exists — that's the project-scoped folder.
  const slug = slugifyProjectName(project?.name);
  if (slug) {
    const sep = ws.includes('\\') ? '\\' : '/';
    const candidate = ws.endsWith(sep) ? `${ws}${slug}` : `${ws}${sep}${slug}`;
    try {
      const probe = await api.fsProbe(candidate);
      if (probe?.exists && probe?.is_dir) return { path: probe.path || candidate, label: 'project folder' };
    } catch { /* fall through to plain workspace */ }
  }
  return { path: ws, label: 'workspace' };
}

export async function openPreviewPanel() {
  const projectId = store.get().activeProjectId;
  if (!projectId) { toast('No active project', { kind: 'warn' }); return; }
  const project = store.get().projects.find((p) => p.project_id === projectId);

  // Resolve a sensible working dir BEFORE rendering so the input shows a useful
  // path on first paint instead of '.'
  const defaultCwd = await resolvePreferredWorkingDir();

  const dlg = createDialog({ title: `Preview · ${project?.name || projectId}`, size: 'xl' });
  dlg.body.innerHTML = `
    <div class="bb-preview">
      <div class="bb-preview__toolbar">
        <button class="bb-btn" id="prv-reload" title="Reload">↻</button>
        <input type="text" class="bb-input bb-preview__url" id="prv-url" placeholder="http://127.0.0.1:5101" />
        <button class="bb-btn" id="prv-open" title="Open in new tab">↗</button>
        <span class="bb-preview__status" id="prv-status">stopped</span>
      </div>
      <div class="bb-preview__settings">
        <div class="bb-preview__cwd">
          <label class="bb-label">Working dir <span id="prv-cwd-source" class="bb-preview__cwd-hint"></span></label>
          <div class="bb-input-group">
            <input type="text" class="bb-input" id="prv-cwd" value="${escapeHtml(defaultCwd)}" />
            <button class="bb-btn" id="prv-cwd-pick">Browse…</button>
          </div>
        </div>
        <div class="bb-preview__runner">
          <label class="bb-label">Runner</label>
          <select class="bb-input" id="prv-runner">
            ${RUNNERS.map((r) => `<option value="${r.value}">${escapeHtml(r.label)}</option>`).join('')}
          </select>
        </div>
        <div class="bb-preview__actions">
          <button class="bb-btn bb-btn--primary" id="prv-start">Start</button>
          <button class="bb-btn bb-btn--danger" id="prv-stop">Stop</button>
        </div>
      </div>
      <div class="bb-preview__frame-host">
        <iframe id="prv-frame" class="bb-preview__frame" sandbox="allow-scripts allow-forms allow-popups"></iframe>
        <div class="bb-preview__placeholder" id="prv-placeholder">No preview running. Choose a runner and click Start.</div>
      </div>
      <details class="bb-preview__logs">
        <summary>Server logs</summary>
        <pre id="prv-logs"></pre>
      </details>
    </div>
  `;
  dlg.setFooter([
    { html: '<span style="color:var(--fg-3);font-size:11px">Tip: click ↻ to reload, ↗ to pop-out, or use the browse dialog to change directories.</span>' },
    { spacer: true },
    { label: 'Close', primary: true, onClick: () => dlg.close() },
  ]);
  dlg.open();

  const urlInput = dlg.body.querySelector('#prv-url');
  const cwdInput = dlg.body.querySelector('#prv-cwd');
  const runnerSel = dlg.body.querySelector('#prv-runner');
  const statusEl  = dlg.body.querySelector('#prv-status');
  const frame     = dlg.body.querySelector('#prv-frame');
  const placeholder = dlg.body.querySelector('#prv-placeholder');
  const logs      = dlg.body.querySelector('#prv-logs');
  const cwdSource = dlg.body.querySelector('#prv-cwd-source');
  if (cwdSource) cwdSource.textContent = '(saved/default)';

  function setRunning(url, runner) {
    statusEl.textContent = 'running';
    statusEl.className = 'bb-preview__status bb-preview__status--running';
    urlInput.value = url || '';
    if (url) { frame.src = url; placeholder.style.display = 'none'; }
  }
  function setStopped() {
    statusEl.textContent = 'stopped';
    statusEl.className = 'bb-preview__status';
    frame.src = 'about:blank';
    placeholder.style.display = '';
    placeholder.textContent = 'No preview running. Choose a runner and click Start.';
  }
  function setPreviewError(message) {
    statusEl.textContent = 'error';
    statusEl.className = 'bb-preview__status';
    statusEl.title = message || '';
    frame.src = 'about:blank';
    placeholder.style.display = '';
    placeholder.textContent = message || 'Preview failed to start.';
  }

  async function refresh() {
    try {
      const data = await api.previewStatus(projectId);
      if (data.running) {
        setRunning(data.url, data.runner);
        if (data.runner) runnerSel.value = data.runner;
        if (data.cwd)    cwdInput.value = data.cwd;
        if (data.log_tail) logs.textContent = data.log_tail.join('\n');
      } else {
        setStopped();
      }
    } catch (err) {
      statusEl.textContent = `error`;
      statusEl.title = err.message;
    }
  }

  dlg.body.querySelector('#prv-cwd-pick').addEventListener('click', async () => {
    const picked = await pickDirectory({
      initial: cwdInput.value,
      title: `Working directory · ${project?.name || 'preview'}`,
    });
    if (picked) {
      cwdInput.value = picked;
      if (cwdSource) cwdSource.textContent = '(custom)';
      saveWorkingDir(picked);
    }
  });
  dlg.body.querySelector('#prv-start').addEventListener('click', async () => {
    try {
      saveWorkingDir(cwdInput.value);
      const data = await api.previewStart(projectId, { cwd: cwdInput.value, runner: runnerSel.value });
      setRunning(data.url, data.runner);
      toast(`Preview running at ${data.url}`, { kind: 'success' });
    } catch (err) {
      const message = `Start failed: ${err.message}`;
      setPreviewError(message);
      logs.textContent = message;
      toast(message, { kind: 'error', timeout: 5000 });
    }
  });
  dlg.body.querySelector('#prv-stop').addEventListener('click', async () => {
    try { await api.previewStop(projectId); setStopped(); toast('Preview stopped'); }
    catch (err) { toast(`Stop failed: ${err.message}`, { kind: 'error' }); }
  });
  dlg.body.querySelector('#prv-reload').addEventListener('click', () => {
    if (urlInput.value) frame.src = urlInput.value;
  });
  dlg.body.querySelector('#prv-open').addEventListener('click', () => {
    if (urlInput.value) window.open(urlInput.value, '_blank');
  });
  urlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); frame.src = urlInput.value; placeholder.style.display = 'none'; }
  });

  await refresh();
}
