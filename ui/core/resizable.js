// Resizable panel primitive — generic drag-handle with bounds, persistence,
// and pointer-capture. Used by the chat sidebar today; reusable for any panel
// that wants drag-to-resize behavior (e.g. future right-side info pane).
//
// Inspired by Plauna's Workspace/PanelLayout pattern (C:\Coding\game\plauna\workspace)
// but stripped down to vanilla DOM since Blackboard isn't using ECS.

/**
 * Make a CSS variable resizable via a drag handle.
 *
 * @param {object} opts
 * @param {string}      opts.cssVar              CSS custom-property name to drive (e.g. ``--sidebar-w``)
 * @param {HTMLElement} [opts.target=document.documentElement]   Element to set the var on
 * @param {HTMLElement} opts.handle              The grab-handle element
 * @param {'h'|'v'}     [opts.axis='h']          'h' = horizontal width, 'v' = vertical height
 * @param {number}      [opts.min=200]
 * @param {number}      [opts.max=800]
 * @param {string}      [opts.storageKey]        localStorage key for persistence
 * @param {(value:number) => void} [opts.onResize]
 * @returns {{ get: () => number, set: (n:number, persist?:boolean) => void, dispose: () => void }}
 */
export function makeResizable({
  cssVar,
  target = document.documentElement,
  handle,
  axis = 'h',
  min = 200,
  max = 800,
  storageKey = '',
  onResize,
}) {
  if (!cssVar || !handle) throw new Error('makeResizable: cssVar and handle are required');

  function clamp(n) { return Math.max(min, Math.min(max, n)); }
  function getCurrent() {
    const cs = getComputedStyle(target).getPropertyValue(cssVar).trim();
    const n = parseFloat(cs);
    return Number.isFinite(n) ? n : min;
  }
  function setValue(n, persist = true) {
    const v = clamp(n);
    target.style.setProperty(cssVar, `${v}px`);
    if (persist && storageKey) {
      try { localStorage.setItem(storageKey, String(v)); } catch { /* private mode */ }
    }
    if (onResize) {
      try { onResize(v); } catch { /* swallow */ }
    }
    return v;
  }

  // Restore persisted value on init.
  if (storageKey) {
    try {
      const raw = localStorage.getItem(storageKey);
      const parsed = raw ? parseFloat(raw) : NaN;
      if (Number.isFinite(parsed)) setValue(parsed, false);
    } catch { /* ignore */ }
  }

  // ── Drag interaction ─────────────────────────────────────
  let dragging = false;
  let startCoord = 0;
  let startSize = 0;

  function onPointerDown(e) {
    if (e.button !== 0) return;
    dragging = true;
    startCoord = axis === 'h' ? e.clientX : e.clientY;
    startSize = getCurrent();
    handle.setPointerCapture?.(e.pointerId);
    handle.classList.add('bb-resize--dragging');
    document.body.style.cursor = axis === 'h' ? 'col-resize' : 'row-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  }

  function onPointerMove(e) {
    if (!dragging) return;
    const cur = axis === 'h' ? e.clientX : e.clientY;
    const delta = cur - startCoord;
    setValue(startSize + delta);
  }

  function onPointerUp(e) {
    if (!dragging) return;
    dragging = false;
    handle.releasePointerCapture?.(e.pointerId);
    handle.classList.remove('bb-resize--dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  }

  function onDoubleClick() {
    // Reset to the midpoint between min and max — fast "centered" preset.
    setValue(Math.round((min + max) / 2));
  }

  handle.addEventListener('pointerdown', onPointerDown);
  handle.addEventListener('pointermove', onPointerMove);
  handle.addEventListener('pointerup', onPointerUp);
  handle.addEventListener('pointercancel', onPointerUp);
  handle.addEventListener('dblclick', onDoubleClick);

  // Make sure the cursor + aria role hint at resizability.
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', axis === 'h' ? 'vertical' : 'horizontal');
  handle.setAttribute('aria-valuemin', String(min));
  handle.setAttribute('aria-valuemax', String(max));
  handle.tabIndex = handle.tabIndex || 0;
  handle.style.cursor = axis === 'h' ? 'col-resize' : 'row-resize';

  // Keyboard nudge: arrow keys move by 16 px, with shift = 64 px.
  function onKey(e) {
    const big = e.shiftKey ? 64 : 16;
    let delta = 0;
    if (axis === 'h') {
      if (e.key === 'ArrowLeft')  delta = -big;
      else if (e.key === 'ArrowRight') delta = +big;
    } else {
      if (e.key === 'ArrowUp')   delta = -big;
      else if (e.key === 'ArrowDown') delta = +big;
    }
    if (!delta) return;
    e.preventDefault();
    setValue(getCurrent() + delta);
  }
  handle.addEventListener('keydown', onKey);

  return {
    get: getCurrent,
    set: setValue,
    dispose() {
      handle.removeEventListener('pointerdown', onPointerDown);
      handle.removeEventListener('pointermove', onPointerMove);
      handle.removeEventListener('pointerup', onPointerUp);
      handle.removeEventListener('pointercancel', onPointerUp);
      handle.removeEventListener('dblclick', onDoubleClick);
      handle.removeEventListener('keydown', onKey);
    },
  };
}
