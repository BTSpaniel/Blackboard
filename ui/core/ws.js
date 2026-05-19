// WebSocket auto-reconnect.
import { bus } from './bus.js';
import { store } from './store.js';

let ws = null;
let attempts = 0;
let reconnectTimer = null;
let heartbeatTimer = null;
const buffer = [];
const noisyCounts = new Map();

function truncateText(value, max = 240) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}… (+${text.length - max} chars)` : text;
}

function compactValue(value, key = '') {
  if (typeof value === 'string') return truncateText(value, key === 'body' || key === 'objective' || key === 'raw' ? 320 : 220);
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) {
    const limit = key === 'models' ? 6 : 8;
    const items = value.slice(0, limit).map((item) => compactValue(item));
    if (value.length > limit) items.push(`… ${value.length - limit} more`);
    return items;
  }
  if (key === 'card') {
    return {
      id: value.id,
      title: truncateText(value.title, 140),
      status: value.status,
      progress: value.progress,
      job_id: value.job_id,
      files: compactValue(value.files || [], 'files'),
      last_job: value.metadata?.last_job ? compactValue(value.metadata.last_job) : undefined,
    };
  }
  const out = {};
  for (const [k, v] of Object.entries(value)) out[k] = compactValue(v, k);
  return out;
}

function compactPayload(topic, data) {
  const payload = data || {};
  if (topic === 'providers:snapshot') {
    return {
      profiles: (payload.profiles || []).map((p) => ({
        id: p.id,
        adapter: p.adapter,
        model: p.model,
        ok: p.ok,
        latency_ms: p.latency_ms,
        error: truncateText(p.error, 160),
        secret: p.secret_status ? { required: !!p.secret_status.required, has_value: !!p.secret_status.has_value } : undefined,
      })),
      roles: payload.roles || {},
    };
  }
  return compactValue(payload);
}

function labelSummary(topic, data) {
  const p = data || {};
  if (topic.startsWith('coding:job.')) return `${p.job_id || ''} ${p.status || ''} success=${p.success ?? ''}`.trim();
  if (topic === 'board:card.job_synced') return `${p.card_id || ''} → ${p.card_status || ''} ${p.reason || ''}`.trim();
  if (topic.startsWith('board:card.') && p.card) return `${p.card.id || ''} → ${p.card.status || ''}`.trim();
  if (topic === 'providers:snapshot') return `${(p.profiles || []).length} profiles`;
  if (topic === 'providers:health') return `${Object.keys(p).length} providers`;
  return '';
}

function logJson(label, data, level = 'log') {
  const topic = label.replace('[ws] event: ', '');
  const compact = compactPayload(topic, data);
  const summary = labelSummary(topic, data);
  try {
    console.groupCollapsed(summary ? `${label} · ${summary}` : label);
    console[level](JSON.stringify(compact, null, 2));
    console.groupEnd();
  } catch {
    console[level](label, data);
  }
}

function shouldLogTopic(topic) {
  if (topic === 'chat.token') return false;
  if (topic === 'chat.thinking') {
    const count = (noisyCounts.get(topic) || 0) + 1;
    noisyCounts.set(topic, count);
    if (count === 1 || count % 50 === 0) {
      console.debug(`[ws] event: ${topic} (${count} chunks so far)`);
    }
    return false;
  }
  if (topic === 'providers:health') {
    return false;
  }
  if (topic === 'providers:snapshot') {
    return false;
  }
  return true;
}

function connect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${window.location.host}/ws`;
  ws = new WebSocket(url);
  ws.addEventListener('open', () => {
    console.log('[ws] connected');
    attempts = 0;
    store.setWs(true);
    bus.emit('ws:connected');
    while (buffer.length) send(buffer.shift());
    heartbeatTimer = setInterval(() => send({ type: 'heartbeat', ts: Date.now() }), 30000);
  });
  ws.addEventListener('close', () => {
    console.log('[ws] disconnected');
    store.setWs(false);
    bus.emit('ws:disconnected');
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    attempts += 1;
    const delay = Math.min(30000, 500 * 2 ** attempts);
    reconnectTimer = setTimeout(connect, delay);
  });
  ws.addEventListener('error', () => ws?.close());
  ws.addEventListener('message', (event) => {
    try {
      const msg = JSON.parse(event.data);
      const topic = msg.topic || msg.event_type || msg.type || 'unknown';
      if (shouldLogTopic(topic)) {
        logJson(`[ws] event: ${topic}`, msg.payload || msg);
      }
      bus.emit('ws:any', msg);
      if (msg.topic) bus.emit(`ws:${msg.topic}`, msg.payload);
      const type = msg.event_type || msg.type || '';
      if (type) {
        bus.emit(`ws:${type}`, msg.payload || msg);
        if (type.includes(':')) bus.emit(type, msg.payload || msg);
      }
    } catch (err) {
      console.warn('[ws] bad payload', err);
    }
  });
}

connect();
window.Blackboard = window.Blackboard || {};
function send(data) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    buffer.push(data);
    return;
  }
  ws.send(JSON.stringify(data));
}
window.Blackboard.ws = { send };
