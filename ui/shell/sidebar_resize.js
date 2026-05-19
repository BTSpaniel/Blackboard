// Sidebar resize controller — wires the chat sidebar's drag handle, persists
// the user's chosen width, and computes a content-aware "ideal" width hint
// based on the current board state.
//
// Patterns: Plauna's PanelLayout (drag-resize) + a Pretext-style DOM-free
// content measurement to right-size the panel without paint thrashing.

import { store } from '/ui/core/store.js';
import { makeResizable } from '/ui/core/resizable.js';
import { measureText, maxWidth } from '/ui/core/pretext.js';

const STORAGE_KEY = 'bb.sidebar.width';
const STORAGE_MANUAL_KEY = 'bb.sidebar.manual';
// Widened defaults per UX feedback — the previous 280–360 range felt cramped
// once card titles, pending counts and the textarea got into the panel.
const MIN_WIDTH = 340;
const MAX_WIDTH = 760;
const DEFAULT_WIDTH = 440;

// Style used for fitting card titles in the chat. Mirrors the chat message
// font so measurements line up with what the user actually sees.
const FIT_STYLE = { fontFamily: 'Inter, system-ui, sans-serif', fontSize: 13, fontWeight: 400 };

let controller = null;
let userManual = false;

function clamp(n) { return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, n)); }

/**
 * Compute the "ideal" sidebar width given the current board state.
 *
 * Heuristic:
 *   base 320px
 * + measured width of the longest card title (capped at 280px)
 * + bonus 20px per pending card beyond the first 3 (cap +80px)
 * + bonus 30px when there are active jobs (more streaming output to read)
 *
 * Plus side padding (~48px) for the chat bubble shell.
 */
export function computeIdealWidth(state) {
  const cards = collectCards(state);
  if (!cards.length) return DEFAULT_WIDTH;

  // Longest title — DOM-free measurement via pretext canvas cache.
  const titles = cards.map((c) => c.title || '').filter(Boolean);
  const longest = Math.min(280, maxWidth(titles, FIT_STYLE));

  // Pending count — anything not in 'done' or 'blocked'.
  const pending = cards.filter((c) => !['done', 'blocked'].includes(c.status)).length;
  const pendingBonus = Math.min(80, Math.max(0, (pending - 3) * 20));

  // Active jobs nudge — the store keeps the running list under `jobs`.
  const runningJobs = (state.jobs || []).filter((j) => {
    const s = String(j?.status || '').toLowerCase();
    return s === 'running' || s === 'queued' || s === 'pending';
  });
  const jobsBonus = runningJobs.length > 0 ? 30 : 0;

  // Base 380px so even a brand-new empty project gets a comfortable chat area.
  return clamp(Math.round(380 + longest + pendingBonus + jobsBonus));
}

function collectCards(state) {
  const board = state?.board;
  if (!board) return [];
  // Board may be either {cards_by_column: {col: [card,...]}} or {cards: [...]}.
  if (Array.isArray(board.cards)) return board.cards;
  if (board.cards_by_column) {
    const out = [];
    for (const list of Object.values(board.cards_by_column)) {
      if (Array.isArray(list)) out.push(...list);
    }
    return out;
  }
  return [];
}

/** Initialize the drag handle and restore the user's persisted width. */
export function initSidebarResize() {
  const handle = document.getElementById('sidebar-resize');
  if (!handle) return;

  // Was the current width set by an explicit user action (drag/click)?
  // We read this BEFORE makeResizable so the restoration step happens cleanly.
  try { userManual = localStorage.getItem(STORAGE_MANUAL_KEY) === '1'; } catch { userManual = false; }

  controller = makeResizable({
    cssVar: '--sidebar-w',
    handle,
    axis: 'h',
    min: MIN_WIDTH,
    max: MAX_WIDTH,
    storageKey: STORAGE_KEY,
    onResize: (v) => {
      // Any drag/double-click/keyboard nudge is treated as a manual choice;
      // we'll stop auto-suggesting different widths after this.
      userManual = true;
      try { localStorage.setItem(STORAGE_MANUAL_KEY, '1'); } catch { /* ignore */ }
      updateHintFor(v);
    },
  });

  // Wire the "fit to content" pill in the header.
  const hint = document.getElementById('chat-fit-hint');
  if (hint) {
    hint.addEventListener('click', () => {
      const ideal = computeIdealWidth(store.get());
      controller.set(ideal, true);
      // Re-mark as a deliberate user action.
      userManual = true;
      try { localStorage.setItem(STORAGE_MANUAL_KEY, '1'); } catch { /* */ }
    });
  }

  // Initial paint: if the user has never manually set a width, gently nudge
  // the panel toward the ideal size for current activity.
  if (!userManual) {
    const ideal = computeIdealWidth(store.get());
    controller.set(ideal, false);
  }
  refitSidebar();
}

/** Update the small hint pill in the sidebar header (size + mismatch arrow). */
export function refitSidebar() {
  const hint = document.getElementById('chat-fit-hint');
  if (!hint || !controller) return;
  const current = controller.get();
  const ideal = computeIdealWidth(store.get());
  updateHintFor(current, ideal);
}

function updateHintFor(current, ideal) {
  const hint = document.getElementById('chat-fit-hint');
  if (!hint) return;
  if (ideal === undefined) ideal = computeIdealWidth(store.get());
  if (Math.abs(ideal - current) < 16) {
    // Within tolerance — show only the size readout, no fit suggestion.
    hint.textContent = `${Math.round(current)}px`;
    hint.removeAttribute('data-suggest');
  } else {
    const arrow = ideal > current ? '→' : '←';
    hint.textContent = `${Math.round(current)}px ${arrow} ${Math.round(ideal)}`;
    hint.setAttribute('data-suggest', '1');
  }
}
