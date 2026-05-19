// Tiny store with subscribe.
import { bus } from './bus.js';

const state = {
  projects: [],
  activeProjectId: null,
  board: null,
  providers: { profiles: [], roles: {} },
  jobs: [],
  ws: { connected: false },
};

export const store = {
  get() { return state; },
  set(patch) {
    Object.assign(state, patch);
    bus.emit('store:update', state);
  },
  setBoard(snapshot) {
    state.board = snapshot;
    bus.emit('store:board', snapshot);
  },
  setProjects(list) {
    state.projects = list;
    bus.emit('store:projects', list);
  },
  setActive(id) {
    state.activeProjectId = id;
    bus.emit('store:active', id);
  },
  setProviders(payload) {
    state.providers = payload;
    bus.emit('store:providers', payload);
  },
  setJobs(list) {
    state.jobs = list;
    bus.emit('store:jobs', list);
  },
  setWs(connected) {
    state.ws.connected = connected;
    bus.emit('store:ws', state.ws);
  },
};

window.Blackboard = window.Blackboard || {};
window.Blackboard.store = store;
