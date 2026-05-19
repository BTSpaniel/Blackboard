// Dialog primitive — replaces the card-modal hijack with a real reusable component.
//
// Usage:
//   const dlg = createDialog({ title: 'Foo', size: 'lg' });
//   dlg.body.innerHTML = '...';
//   dlg.setFooter([{ label: 'Save', primary: true, onClick: () => dlg.close() }]);
//   dlg.open();
//
// Sizes: sm (480px), md (640px), lg (920px), xl (1240px), full (95vw).
// Multiple dialogs can be open at once and stack via z-index. ESC closes the topmost.

const _OPEN_DIALOGS = [];

function _onKeydown(e) {
  if (e.key !== 'Escape') return;
  const top = _OPEN_DIALOGS[_OPEN_DIALOGS.length - 1];
  if (top && top.dismissable) top.close();
}
window.addEventListener('keydown', _onKeydown);

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

export function createDialog({ title = '', size = 'md', dismissable = true } = {}) {
  const overlay = document.createElement('div');
  overlay.className = `bb-dialog-overlay bb-dialog-overlay--${size}`;
  overlay.innerHTML = `
    <div class="bb-dialog" role="dialog" aria-modal="true">
      <header class="bb-dialog__header">
        <h2 class="bb-dialog__title"></h2>
        <button class="bb-dialog__close" aria-label="Close" type="button">✕</button>
      </header>
      <div class="bb-dialog__tabs"></div>
      <div class="bb-dialog__body"></div>
      <footer class="bb-dialog__footer"></footer>
    </div>
  `;
  document.body.appendChild(overlay);

  const titleEl = overlay.querySelector('.bb-dialog__title');
  const tabsEl  = overlay.querySelector('.bb-dialog__tabs');
  const bodyEl  = overlay.querySelector('.bb-dialog__body');
  const footEl  = overlay.querySelector('.bb-dialog__footer');
  const closeEl = overlay.querySelector('.bb-dialog__close');
  titleEl.textContent = title || '';
  if (!title) overlay.querySelector('.bb-dialog__header').style.display = 'none';

  const dlg = {
    overlay, titleEl, tabsEl, bodyEl, footEl, closeEl,
    body: bodyEl,
    dismissable,
    _onClose: null,

    setTitle(text) { titleEl.textContent = text || ''; overlay.querySelector('.bb-dialog__header').style.display = text ? '' : 'none'; },

    setTabs(tabs, activeKey) {
      tabsEl.innerHTML = '';
      if (!tabs || !tabs.length) { tabsEl.style.display = 'none'; return; }
      tabsEl.style.display = '';
      for (const t of tabs) {
        const btn = document.createElement('button');
        btn.className = 'bb-dialog__tab' + (t.key === activeKey ? ' bb-dialog__tab--active' : '');
        btn.textContent = t.label;
        btn.dataset.key = t.key;
        btn.addEventListener('click', () => {
          tabsEl.querySelectorAll('.bb-dialog__tab').forEach((b) => b.classList.toggle('bb-dialog__tab--active', b.dataset.key === t.key));
          if (typeof t.onActivate === 'function') t.onActivate();
        });
        tabsEl.appendChild(btn);
      }
    },

    setFooter(buttons) {
      footEl.innerHTML = '';
      if (!buttons || !buttons.length) { footEl.style.display = 'none'; return; }
      footEl.style.display = '';
      // Allow a leading element (e.g. status text) by making first item a {html} entry.
      for (let i = 0; i < buttons.length; i++) {
        const b = buttons[i];
        if (b.spacer) {
          const s = document.createElement('div'); s.style.flex = '1'; footEl.appendChild(s); continue;
        }
        if (b.html) {
          const w = document.createElement('span'); w.innerHTML = b.html; footEl.appendChild(w); continue;
        }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'bb-btn' + (b.primary ? ' bb-btn--primary' : '') + (b.danger ? ' bb-btn--danger' : '');
        btn.textContent = b.label;
        if (b.disabled) btn.disabled = true;
        btn.addEventListener('click', () => { try { b.onClick && b.onClick(dlg); } catch (err) { console.error(err); } });
        footEl.appendChild(btn);
      }
    },

    onClose(fn) { this._onClose = fn; },

    open() {
      requestAnimationFrame(() => overlay.classList.add('bb-dialog-overlay--open'));
      _OPEN_DIALOGS.push(dlg);
      return dlg;
    },

    close() {
      const idx = _OPEN_DIALOGS.indexOf(dlg);
      if (idx >= 0) _OPEN_DIALOGS.splice(idx, 1);
      overlay.classList.remove('bb-dialog-overlay--open');
      try { this._onClose && this._onClose(); } catch {}
      setTimeout(() => overlay.remove(), 180);
    },
  };

  closeEl.addEventListener('click', () => { if (dismissable) dlg.close(); });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay && dismissable) dlg.close();
  });

  return dlg;
}

// Convenience: confirm dialog (returns Promise<boolean>).
export function confirmDialog({ title = 'Confirm', message = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    const dlg = createDialog({ title, size: 'sm' });
    dlg.body.innerHTML = `<div style="padding:8px 4px;color:var(--fg-1)">${escapeHtml(message)}</div>`;
    dlg.setFooter([
      { label: cancelLabel, onClick: () => { dlg.close(); resolve(false); } },
      { spacer: true },
      { label: confirmLabel, primary: !danger, danger, onClick: () => { dlg.close(); resolve(true); } },
    ]);
    dlg.open();
  });
}

// Convenience: prompt dialog (returns Promise<string|null>).
// Pass type='password' to mask the input (useful for API keys).
export function promptDialog({
  title = 'Input', label = '', defaultValue = '', placeholder = '',
  confirmLabel = 'OK', type = 'text', help = '',
} = {}) {
  return new Promise((resolve) => {
    const dlg = createDialog({ title, size: 'sm' });
    const inputType = type === 'password' ? 'password' : 'text';
    dlg.body.innerHTML = `
      <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:6px">${escapeHtml(label)}</label>
      <div style="position:relative">
        <input type="${inputType}" class="bb-input" id="bb-prompt-input"
               value="${escapeHtml(defaultValue)}" placeholder="${escapeHtml(placeholder)}"
               style="width:100%;padding-right:${inputType === 'password' ? '34px' : '8px'}"
               autocomplete="off" spellcheck="false" />
        ${inputType === 'password' ? '<button type="button" id="bb-prompt-eye" title="Show/hide" style="position:absolute;right:4px;top:50%;transform:translateY(-50%);background:transparent;border:0;color:var(--fg-3);cursor:pointer;padding:4px 8px;font-size:14px">👁</button>' : ''}
      </div>
      ${help ? `<div style="margin-top:8px;font-size:11px;color:var(--fg-3)">${help}</div>` : ''}
    `;
    const input = dlg.body.querySelector('#bb-prompt-input');
    const eye = dlg.body.querySelector('#bb-prompt-eye');
    if (eye) eye.addEventListener('click', () => {
      input.type = input.type === 'password' ? 'text' : 'password';
    });
    setTimeout(() => { input.focus(); input.select(); }, 30);
    const submit = () => { dlg.close(); resolve(input.value); };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    dlg.setFooter([
      { label: 'Cancel', onClick: () => { dlg.close(); resolve(null); } },
      { spacer: true },
      { label: confirmLabel, primary: true, onClick: submit },
    ]);
    dlg.open();
  });
}

// Convenience: toast (auto-dismissing).
export function toast(message, { kind = 'info', timeout = 2400 } = {}) {
  const host = document.getElementById('bb-toast-host') || (() => {
    const h = document.createElement('div'); h.id = 'bb-toast-host'; document.body.appendChild(h); return h;
  })();
  const el = document.createElement('div');
  el.className = `bb-toast bb-toast--${kind}`;
  el.textContent = message;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('bb-toast--visible'));
  setTimeout(() => { el.classList.remove('bb-toast--visible'); setTimeout(() => el.remove(), 200); }, timeout);
}
