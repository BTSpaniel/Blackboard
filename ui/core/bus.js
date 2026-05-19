// Tiny pub/sub bus for the UI.
const subs = new Map();

export const bus = {
  on(topic, handler) {
    if (!subs.has(topic)) subs.set(topic, new Set());
    subs.get(topic).add(handler);
    return () => subs.get(topic)?.delete(handler);
  },
  off(topic, handler) {
    subs.get(topic)?.delete(handler);
  },
  emit(topic, payload) {
    (subs.get(topic) || []).forEach((h) => {
      try { h(payload); } catch (err) { console.error(`[bus:${topic}]`, err); }
    });
    (subs.get('*') || []).forEach((h) => {
      try { h({ topic, payload }); } catch (err) { console.error('[bus:*]', err); }
    });
  },
};

window.Blackboard = window.Blackboard || {};
window.Blackboard.bus = bus;
