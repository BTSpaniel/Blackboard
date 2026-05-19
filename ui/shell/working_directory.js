import { api } from '/ui/core/api.js';
import { store } from '/ui/core/store.js';

const LS_WORKING_DIR = 'bb.execution.workingDir';

function isTrivial(path) {
  const value = String(path || '').trim();
  return !value || value === '.' || value === './';
}

function normalizePath(path) {
  return String(path || '').replace(/\\/g, '/').replace(/\/+$/g, '').toLowerCase();
}

function isWorkspaceParent(path, workspace) {
  const root = normalizePath(path);
  const ws = normalizePath(workspace);
  if (!root || !ws) return false;
  const idx = ws.lastIndexOf('/');
  if (idx < 0) return false;
  return root === ws.slice(0, idx);
}

async function validDir(path) {
  if (isTrivial(path)) return '';
  try {
    const probe = await api.fsProbe(path);
    if (probe?.exists && probe?.is_dir) return probe.path || path;
  } catch { }
  return '';
}

export function readSavedWorkingDir() {
  try { return localStorage.getItem(LS_WORKING_DIR) || ''; }
  catch { return ''; }
}

export function saveWorkingDir(path) {
  const value = String(path || '').trim();
  if (!value) return;
  try { localStorage.setItem(LS_WORKING_DIR, value); } catch { }
}

export async function resolvePreferredWorkingDir() {
  let workspace = '';
  try {
    const home = await api.fsHome();
    workspace = home?.default_workspace || '';
  } catch { }

  const activeProjectId = store.get().activeProjectId;
  const activeProject = (store.get().projects || []).find((p) => p.project_id === activeProjectId);
  const projectRoot = activeProject?.root || '';
  if (!isWorkspaceParent(projectRoot, workspace)) {
    const validProjectRoot = await validDir(projectRoot);
    if (validProjectRoot) return validProjectRoot;
  }

  const saved = readSavedWorkingDir();
  if (!isWorkspaceParent(saved, workspace)) {
    const validSaved = await validDir(saved);
    if (validSaved) return validSaved;
  }
  if (!isTrivial(workspace)) return workspace;
  return '.';
}
