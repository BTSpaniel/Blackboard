// Pretext — DOM-free text measurement.
//
// Pattern borrowed from Plauna (C:\Coding\game\plauna\text). Uses an offscreen
// 2D canvas context to compute the rendered width of strings WITHOUT touching
// the DOM. This avoids layout thrashing when we want to size a panel based on
// content (e.g. "how wide should the chat sidebar be so the longest card title
// fits without truncation?").
//
// Results are cached by ``font|size|weight|text`` so repeated measurements are
// O(1). The cache is bounded so long-running sessions don't leak memory.

const _cache = new Map();   // key -> {width, ascent, descent}
const _CACHE_MAX = 2048;
let _ctx = null;            // shared 2D context

function _getContext() {
  if (_ctx) return _ctx;
  // OffscreenCanvas is the modern API — no DOM attachment, no reflow.
  if (typeof OffscreenCanvas !== 'undefined') {
    try { _ctx = new OffscreenCanvas(1, 1).getContext('2d'); return _ctx; } catch { /* fall through */ }
  }
  // Fallback: a detached <canvas> element. Works in older browsers and jsdom.
  if (typeof document !== 'undefined') {
    const c = document.createElement('canvas');
    _ctx = c.getContext('2d');
    return _ctx;
  }
  return null;
}

function _normalizeStyle(style = {}) {
  return {
    fontFamily: style.fontFamily || 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    fontSize:   style.fontSize   || 13,
    fontWeight: style.fontWeight || 400,
    fontStyle:  style.fontStyle  || 'normal',
  };
}

function _styleString(s) {
  return `${s.fontStyle} ${s.fontWeight} ${s.fontSize}px ${s.fontFamily}`;
}

function _cacheKey(text, s) {
  return `${s.fontStyle}|${s.fontWeight}|${s.fontSize}|${s.fontFamily}|${text}`;
}

/**
 * Measure the rendered width (and approx ascent/descent) of ``text`` in the
 * given style. Falls back to a heuristic width when no canvas API is available.
 *
 * @param {string} text
 * @param {object} [style]   { fontFamily, fontSize, fontWeight, fontStyle }
 * @returns {{width:number, ascent:number, descent:number, height:number}}
 */
export function measureText(text, style) {
  const s = _normalizeStyle(style);
  const key = _cacheKey(text || '', s);
  const cached = _cache.get(key);
  if (cached) return cached;

  let result;
  const ctx = _getContext();
  if (ctx) {
    ctx.font = _styleString(s);
    const m = ctx.measureText(text || '');
    const ascent = m.actualBoundingBoxAscent || s.fontSize * 0.8;
    const descent = m.actualBoundingBoxDescent || s.fontSize * 0.2;
    result = {
      width: m.width || 0,
      ascent,
      descent,
      height: ascent + descent,
    };
  } else {
    // Heuristic: average glyph width ≈ 0.55× fontSize for proportional fonts.
    const avg = s.fontSize * 0.55;
    result = {
      width: (text || '').length * avg,
      ascent: s.fontSize * 0.8,
      descent: s.fontSize * 0.2,
      height: s.fontSize,
    };
  }

  if (_cache.size >= _CACHE_MAX) {
    // Simple FIFO eviction — drop the oldest entry.
    const firstKey = _cache.keys().next().value;
    if (firstKey !== undefined) _cache.delete(firstKey);
  }
  _cache.set(key, result);
  return result;
}

/**
 * Return the maximum width across an array of strings in the given style.
 * Useful for sizing a column or panel to fit its longest entry.
 */
export function maxWidth(strings, style) {
  if (!strings || !strings.length) return 0;
  let max = 0;
  for (const t of strings) {
    const w = measureText(t || '', style).width;
    if (w > max) max = w;
  }
  return max;
}

/**
 * Estimate how many lines the text wraps to inside ``maxLineWidth`` pixels.
 * Splits on whitespace; an exact greedy word-wrap pass. Cheap and correct
 * enough for sidebar/card sizing decisions.
 */
export function estimateWrappedLines(text, maxLineWidth, style) {
  if (!text || maxLineWidth <= 0) return 0;
  const s = _normalizeStyle(style);
  const words = String(text).split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  let lines = 1;
  let lineWidth = 0;
  const spaceW = measureText(' ', s).width;
  for (const w of words) {
    const ww = measureText(w, s).width;
    if (lineWidth === 0) {
      lineWidth = ww;
      continue;
    }
    if (lineWidth + spaceW + ww <= maxLineWidth) {
      lineWidth += spaceW + ww;
    } else {
      lines += 1;
      lineWidth = ww;
    }
  }
  return lines;
}

/** Clear the measurement cache (call after font swaps or bulk style changes). */
export function clearCache() {
  _cache.clear();
}

/** Internal: expose cache size for tests. */
export function _cacheSize() { return _cache.size; }
