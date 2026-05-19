// Terminal panel — Dialog primitive + dir picker for cwd + better keybindings.
import { api } from '/ui/core/api.js';
import { createDialog, toast } from '/ui/shell/dialog.js';
import { pickDirectory } from '/ui/shell/directory_picker.js';
import { resolvePreferredWorkingDir, saveWorkingDir } from '/ui/shell/working_directory.js';

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

export async function openTerminalPanel() {
  const cwd = await resolvePreferredWorkingDir();

  let term;
  try { term = await api.terminalCreate({ cwd }); }
  catch (err) { toast(`Spawn failed: ${err.message}`, { kind: 'error' }); return; }

  const dlg = createDialog({ title: `Terminal · ${term.id}`, size: 'xl' });
  dlg.body.innerHTML = `
    <div class="bb-term">
      <pre id="term-output" class="bb-term__output"></pre>
      <div class="bb-term__inputbar">
        <span class="bb-term__prompt">$</span>
        <input id="term-input" class="bb-input bb-term__input" placeholder="Type a command and press Enter…" />
      </div>
    </div>
  `;
  dlg.setFooter([
    { html: `<span style="color:var(--fg-3);font-size:11px">cwd: ${escapeHtml(term.cwd)} · shell: ${escapeHtml(term.shell)}</span>` },
    { spacer: true },
    { label: 'Change cwd…', onClick: () => changeCwd() },
    { label: 'Kill', danger: true, onClick: () => close(true) },
    { label: 'Close', onClick: () => close(false) },
  ]);
  dlg.onClose(() => close(false));
  dlg.open();

  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${window.location.host}${term.ws}`);
  const output = dlg.body.querySelector('#term-output');
  const input = dlg.body.querySelector('#term-input');
  const history = []; let histIdx = -1;

  ws.addEventListener('message', (event) => {
    try {
      const msg = JSON.parse(event.data);
      const text = msg.text || '';
      const stream = msg.stream || 'stdout';
      const colorClass = stream === 'stderr' ? 'bb-term__line--err'
                        : stream === 'system' ? 'bb-term__line--sys'
                        : 'bb-term__line--out';
      output.insertAdjacentHTML('beforeend', `<span class="${colorClass}">${escapeHtml(text)}</span>`);
      output.scrollTop = output.scrollHeight;
    } catch {}
  });
  ws.addEventListener('error', () => { output.insertAdjacentHTML('beforeend', '<span class="bb-term__line--err">[ws error]</span>'); });

  function send() {
    const cmd = input.value;
    if (!cmd && cmd !== '') return;
    history.unshift(cmd); histIdx = -1;
    ws.send(cmd + '\n');
    input.value = '';
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); send(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); histIdx = Math.min(histIdx + 1, history.length - 1); if (history[histIdx] != null) input.value = history[histIdx]; }
    else if (e.key === 'ArrowDown') { e.preventDefault(); histIdx = Math.max(histIdx - 1, -1); input.value = histIdx < 0 ? '' : (history[histIdx] || ''); }
    else if (e.key === 'l' && e.ctrlKey) { e.preventDefault(); output.innerHTML = ''; }
  });
  setTimeout(() => input.focus(), 50);

  let closed = false;
  async function changeCwd() {
    const picked = await pickDirectory({
      initial: term.cwd,
      title: 'Choose terminal working directory',
      confirmLabel: 'Use next time',
    });
    if (!picked) return;
    saveWorkingDir(picked);
    toast(`Working directory saved. New terminals will open in ${picked}`, { kind: 'success', timeout: 2400 });
  }
  async function close(killProcess) {
    if (closed) return; closed = true;
    try { ws.close(); } catch {}
    if (killProcess) { try { await api.terminalClose(term.id); } catch {} }
    dlg.close();
  }
}
