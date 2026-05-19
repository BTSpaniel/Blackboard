// Inline artifact widget renderer.
// Parses <blackboard-artifact type="..." title="..."> JSON </blackboard-artifact> tags
// embedded in chat or card text and replaces them with small interactive widgets.
//
// Supported types:
//   line-chart   { data: [{x, y}, ...] }      — canvas2d sparkline
//   bar-chart    { data: [{label, value}] }   — css horizontal bars
//   table        { columns: [], rows: [[]] }  — sortable HTML table
//   html         { html: "..." }              — sandboxed iframe srcdoc

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function parseTagAttrs(tag) {
  const out = {};
  for (const m of tag.matchAll(/(\w[\w-]*)\s*=\s*"([^"]*)"/g)) {
    out[m[1]] = m[2];
  }
  return out;
}

function copyText(text) {
  const value = String(text || '');
  if (!value) return Promise.resolve(false);
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(value).then(() => true, () => false);
  }
  const field = document.createElement('textarea');
  field.value = value;
  field.setAttribute('readonly', 'readonly');
  field.style.position = 'fixed';
  field.style.top = '-1000px';
  field.style.opacity = '0';
  document.body.appendChild(field);
  field.select();
  field.setSelectionRange(0, field.value.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  field.remove();
  return Promise.resolve(ok);
}

function isGenericArtifactTitle(title) {
  const value = String(title || '').trim().toLowerCase().replace(/\s+/g, ' ');
  return ['', 'preview', 'artifact', 'artifact studio', 'html preview', 'html artifact', 'untitled artifact'].includes(value);
}

function plainTextFragment(value) {
  return String(value || '')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function artifactTypeLabel(type = 'html') {
  return {
    html: 'HTML',
    markdown: 'Markdown',
    json: 'JSON',
    javascript: 'JavaScript',
    css: 'CSS',
    'line-chart': 'Line chart',
    'bar-chart': 'Bar chart',
    table: 'Table',
    text: 'Text',
  }[String(type || '').trim().toLowerCase()] || 'Artifact';
}

function inferArtifactTitle(source, type = 'html') {
  const kind = String(type || 'html').trim().toLowerCase();
  const text = String(source || '');
  if (kind === 'html') {
    for (const pattern of [/<title[^>]*>([\s\S]*?)<\/title>/i, /<h1[^>]*>([\s\S]*?)<\/h1>/i, /<h2[^>]*>([\s\S]*?)<\/h2>/i]) {
      const match = text.match(pattern);
      if (!match) continue;
      const candidate = plainTextFragment(match[1]).slice(0, 120);
      if (candidate) return candidate;
    }
  }
  return `${artifactTypeLabel(kind)} artifact`;
}

function resolveArtifactTitle(title, source, type = 'html') {
  const value = String(title || '').trim();
  if (value && !isGenericArtifactTitle(value)) return value;
  return inferArtifactTitle(source, type);
}

function appendHtmlSourceControls(container, source, title = 'Artifact', options = {}) {
  const artifactRef = options.artifactRef && typeof options.artifactRef === 'object' ? options.artifactRef : { artifactId: String(options.artifactId || '').trim() };
  const artifactType = String(options.type || 'html');
  const resolvedTitle = resolveArtifactTitle(title, source, artifactType);
  const controls = document.createElement('div');
  controls.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px';
  const open = document.createElement('button');
  open.type = 'button';
  open.textContent = 'Edit / Tweak';
  open.style.cssText = 'appearance:none;border:1px solid rgba(147,180,255,0.22);background:rgba(79,124,255,0.12);color:#dbe7ff;border-radius:999px;padding:4px 10px;font-size:11px;cursor:pointer';
  open.addEventListener('click', async () => {
    open.disabled = true;
    open.textContent = 'Opening…';
    try {
      const opened = await window.Blackboard?.openArtifactStudio?.({
        artifactId: String(artifactRef.artifactId || '').trim(),
        title: resolvedTitle,
        source,
        type: artifactType,
      });
      const artifactId = String(opened?.artifact_id || artifactRef.artifactId || '').trim();
      if (artifactId) artifactRef.artifactId = artifactId;
    } finally {
      open.textContent = 'Edit / Tweak';
      open.disabled = false;
    }
  });
  controls.appendChild(open);
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.textContent = 'Copy code';
  copy.style.cssText = 'appearance:none;border:1px solid var(--line);background:var(--bg-1);color:var(--fg-1);border-radius:999px;padding:4px 10px;font-size:11px;cursor:pointer';
  copy.addEventListener('click', () => {
    copyText(source).then((ok) => {
      const next = ok ? 'Copied' : 'Copy failed';
      copy.textContent = next;
      window.setTimeout(() => {
        if (copy.isConnected) copy.textContent = 'Copy code';
      }, 1200);
    });
  });
  controls.appendChild(copy);
  container.appendChild(controls);

  const details = document.createElement('details');
  details.open = true;
  details.style.cssText = 'margin:0 0 8px';
  const summary = document.createElement('summary');
  summary.textContent = 'View code';
  summary.style.cssText = 'cursor:pointer;font-size:11px;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.4px';
  details.appendChild(summary);
  const pre = document.createElement('pre');
  pre.textContent = source;
  pre.style.cssText = 'margin:8px 0 0;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--bg-1);color:var(--fg-1);font-size:11px;line-height:1.45;white-space:pre-wrap;overflow:auto;max-height:260px';
  details.appendChild(pre);
  container.appendChild(details);
  return { details, summary };
}

const HTML_ARTIFACT_DEFAULT_HEIGHT = 420;
const HTML_ARTIFACT_MIN_HEIGHT = 320;
const HTML_ARTIFACT_MAX_HEIGHT = 1200;

function renderLineChart(container, data) {
  const canvas = document.createElement('canvas');
  canvas.width = 480; canvas.height = 120;
  canvas.style.width = '100%';
  container.appendChild(canvas);
  const ctx = canvas.getContext('2d');
  const points = (data?.data || []).map((p, i) => ({ x: p.x ?? i, y: Number(p.y ?? p) || 0 }));
  if (!points.length) return;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
  const xrange = xmax - xmin || 1, yrange = ymax - ymin || 1;
  const W = canvas.width, H = canvas.height, P = 10;
  ctx.strokeStyle = '#4f7cff'; ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = P + ((p.x - xmin) / xrange) * (W - 2 * P);
    const y = H - P - ((p.y - ymin) / yrange) * (H - 2 * P);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function renderBarChart(container, data) {
  const rows = (data?.data || []);
  if (!rows.length) return;
  const max = Math.max(...rows.map((r) => Number(r.value) || 0)) || 1;
  for (const r of rows) {
    const wrap = document.createElement('div');
    wrap.style.display = 'flex'; wrap.style.alignItems = 'center'; wrap.style.gap = '8px'; wrap.style.margin = '3px 0';
    wrap.innerHTML = `
      <div style="width:80px;font-size:11px;color:var(--fg-2)">${escapeHtml(String(r.label || ''))}</div>
      <div style="flex:1;background:var(--bg-1);border-radius:3px;overflow:hidden;height:14px">
        <div style="width:${(Number(r.value) || 0) / max * 100}%;background:var(--accent);height:100%"></div>
      </div>
      <div style="width:48px;text-align:right;font-size:11px;color:var(--fg-1)">${escapeHtml(String(r.value || ''))}</div>
    `;
    container.appendChild(wrap);
  }
}

function renderTable(container, data) {
  const columns = data?.columns || (data?.rows?.[0] ? Object.keys(data.rows[0]) : []);
  const rows = data?.rows || [];
  if (!rows.length) return;
  const table = document.createElement('table');
  table.style.width = '100%'; table.style.borderCollapse = 'collapse'; table.style.fontSize = '12px';
  const head = `<thead><tr style="text-align:left;color:var(--fg-3)">${columns.map((c) => `<th style="padding:4px 6px;border-bottom:1px solid var(--line)">${escapeHtml(String(c))}</th>`).join('')}</tr></thead>`;
  const tbody = rows.map((row) => {
    const cells = Array.isArray(row) ? row : columns.map((c) => row[c]);
    return `<tr>${cells.map((c) => `<td style="padding:4px 6px;border-bottom:1px solid var(--line)">${escapeHtml(String(c ?? ''))}</td>`).join('')}</tr>`;
  }).join('');
  table.innerHTML = head + `<tbody>${tbody}</tbody>`;
  container.appendChild(table);
}

function buildHtmlSrcdoc(html, frameId) {
  const source = String(html || '');
  const bridge = `<script>
(() => {
  const frameId = ${JSON.stringify(frameId)};
  let lastHeight = 0;
  const publish = () => {
    const body = document.body;
    const doc = document.documentElement;
    const height = Math.max(
      body ? body.scrollHeight : 0,
      body ? body.offsetHeight : 0,
      doc ? doc.scrollHeight : 0,
      doc ? doc.offsetHeight : 0,
      120,
    );
    if (Math.abs(height - lastHeight) < 2) return;
    lastHeight = height;
    parent.postMessage({ source: 'blackboard-artifact', frameId, height }, '*');
  };
  const schedule = () => requestAnimationFrame(publish);
  window.addEventListener('load', schedule);
  window.addEventListener('resize', schedule);
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true });
  if ('ResizeObserver' in window) {
    const ro = new ResizeObserver(schedule);
    if (document.documentElement) ro.observe(document.documentElement);
    if (document.body) ro.observe(document.body);
  }
  setTimeout(publish, 0);
  setTimeout(publish, 120);
  setTimeout(publish, 400);
})();
</script>`;
  if (/<\/body\s*>/i.test(source)) {
    return source.replace(/<\/body\s*>/i, `${bridge}</body>`);
  }
  return `${source}${bridge}`;
}

function renderHtml(container, data) {
  const source = String(data?.html || '');
  const artifactRef = data?.artifactRef && typeof data.artifactRef === 'object' ? data.artifactRef : { artifactId: String(data?.artifact_id || '').trim() };
  const title = resolveArtifactTitle(String(data?.title || 'Artifact'), source, 'html');
  const controls = appendHtmlSourceControls(container, source, title, { artifactRef, type: 'html' });
  const previewShell = document.createElement('div');
  previewShell.style.cssText = 'display:grid;gap:8px';
  const previewState = document.createElement('div');
  previewState.style.cssText = 'font-size:11px;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.4px';
  const previewHost = document.createElement('div');
  previewShell.appendChild(previewState);
  previewShell.appendChild(previewHost);
  container.appendChild(previewShell);

  let lastHeight = HTML_ARTIFACT_DEFAULT_HEIGHT;
  let releaseFrame = null;
  let nearViewport = false;
  let intersectionObserver = null;
  let removalObserver = null;
  let workspaceObserver = null;
  let fallbackScrollHandler = null;
  let fallbackResizeHandler = null;
  let manualCodeVisible = Boolean(controls?.details?.open);
  let forcedCodeVisible = false;
  let codeVisible = manualCodeVisible;
  let autoOpenedForStudio = false;
  let programmaticToggle = false;

  const workspaceShell = () => document.getElementById('workspace-shell');
  const isArtifactStudioOpen = () => workspaceShell()?.dataset.activeWorkspaceView === 'studio';

  const renderPausedState = (label) => {
    previewState.textContent = label;
    const paused = document.createElement('div');
    paused.style.cssText = 'display:grid;place-items:center;min-height:96px;padding:14px;border:1px dashed var(--line);border-radius:6px;background:rgba(255,255,255,0.02);color:var(--fg-2);font-size:12px;text-align:center';
    paused.textContent = label;
    previewHost.replaceChildren(paused);
  };

  const unmountFrame = (label) => {
    if (releaseFrame) {
      const cleanup = releaseFrame;
      releaseFrame = null;
      cleanup();
    }
    renderPausedState(label);
  };

  const mountFrame = () => {
    if (releaseFrame) return;
    const frameId = `artifact-${Math.random().toString(36).slice(2)}`;
    const iframe = document.createElement('iframe');
    iframe.sandbox = 'allow-scripts';
    iframe.style.width = '100%';
    iframe.style.height = `${lastHeight}px`;
    iframe.style.border = '1px solid var(--line)';
    iframe.style.borderRadius = '4px';
    iframe.style.opacity = '0.999';
    const onMessage = (event) => {
      const payload = event.data;
      if (!payload || payload.source !== 'blackboard-artifact' || payload.frameId !== frameId) return;
      const nextHeight = Math.max(HTML_ARTIFACT_MIN_HEIGHT, Math.min(Number(payload.height) || HTML_ARTIFACT_DEFAULT_HEIGHT, HTML_ARTIFACT_MAX_HEIGHT));
      lastHeight = nextHeight;
      const currentHeight = Math.max(HTML_ARTIFACT_MIN_HEIGHT, Number.parseFloat(iframe.style.height) || HTML_ARTIFACT_DEFAULT_HEIGHT);
      if (nextHeight <= currentHeight) return;
      iframe.style.height = `${nextHeight}px`;
    };
    window.addEventListener('message', onMessage);
    iframe.addEventListener('load', () => {
      iframe.style.opacity = '1';
    });
    iframe.srcdoc = buildHtmlSrcdoc(source, frameId);
    previewState.textContent = 'Live preview active';
    previewHost.replaceChildren(iframe);
    releaseFrame = () => {
      window.removeEventListener('message', onMessage);
      iframe.srcdoc = '';
      if (iframe.isConnected) iframe.remove();
    };
  };

  const syncPreviewState = () => {
    if (codeVisible) {
      previewShell.style.display = 'none';
      unmountFrame('Preview hidden while viewing code');
      return;
    }
    previewShell.style.display = 'grid';
    if (document.hidden) {
      unmountFrame('Preview paused while tab inactive');
      return;
    }
    if (nearViewport) {
      mountFrame();
      return;
    }
    unmountFrame('Preview paused offscreen');
  };

  const syncCodeMode = () => {
    codeVisible = forcedCodeVisible || manualCodeVisible;
    if (controls?.summary) controls.summary.textContent = codeVisible ? 'Hide code' : 'View code';
    syncPreviewState();
  };

  const setDetailsOpen = (open) => {
    if (!controls?.details || controls.details.open === open) return false;
    programmaticToggle = true;
    controls.details.open = open;
    queueMicrotask(() => {
      programmaticToggle = false;
      syncCodeMode();
    });
    return true;
  };

  const syncWorkspaceCodeMode = () => {
    forcedCodeVisible = isArtifactStudioOpen();
    if (forcedCodeVisible) {
      if (controls?.details && !controls.details.open) {
        autoOpenedForStudio = true;
        if (setDetailsOpen(true)) return;
      }
    } else if (autoOpenedForStudio && !manualCodeVisible) {
      autoOpenedForStudio = false;
      if (controls?.details?.open) {
        if (setDetailsOpen(false)) return;
      }
    }
    syncCodeMode();
  };

  if (controls?.details && controls?.summary) {
    const syncCodeState = () => {
      if (programmaticToggle) return;
      if (forcedCodeVisible && !controls.details.open) {
        autoOpenedForStudio = true;
        setDetailsOpen(true);
        return;
      }
      manualCodeVisible = controls.details.open;
      if (manualCodeVisible) autoOpenedForStudio = false;
      syncCodeMode();
    };
    controls.details.addEventListener('toggle', syncCodeState);
    syncWorkspaceCodeMode();
  }

  const updateFallbackVisibility = () => {
    const bounds = container.getBoundingClientRect();
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    nearViewport = bounds.bottom >= -240 && bounds.top <= viewportHeight + 240 && bounds.right >= 0 && bounds.left <= viewportWidth;
    syncPreviewState();
  };

  if ('IntersectionObserver' in window) {
    intersectionObserver = new IntersectionObserver((entries) => {
      nearViewport = entries.some((entry) => entry.isIntersecting || entry.intersectionRatio > 0);
      syncPreviewState();
    }, { rootMargin: '240px 0px', threshold: 0.01 });
    intersectionObserver.observe(container);
    updateFallbackVisibility();
  } else {
    fallbackScrollHandler = () => updateFallbackVisibility();
    fallbackResizeHandler = () => updateFallbackVisibility();
    window.addEventListener('scroll', fallbackScrollHandler, { passive: true });
    window.addEventListener('resize', fallbackResizeHandler);
    updateFallbackVisibility();
  }

  const onVisibilityChange = () => {
    syncPreviewState();
  };
  document.addEventListener('visibilitychange', onVisibilityChange);

  const shell = workspaceShell();
  if (shell) {
    workspaceObserver = new MutationObserver(() => {
      syncWorkspaceCodeMode();
    });
    workspaceObserver.observe(shell, { attributes: true, attributeFilter: ['data-active-workspace-view'] });
  }

  removalObserver = new MutationObserver(() => {
    if (container.isConnected) return;
    if (intersectionObserver) intersectionObserver.disconnect();
    if (workspaceObserver) workspaceObserver.disconnect();
    if (fallbackScrollHandler) window.removeEventListener('scroll', fallbackScrollHandler);
    if (fallbackResizeHandler) window.removeEventListener('resize', fallbackResizeHandler);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    if (releaseFrame) {
      const cleanup = releaseFrame;
      releaseFrame = null;
      cleanup();
    }
    removalObserver.disconnect();
  });
  removalObserver.observe(document.documentElement, { childList: true, subtree: true });
  syncWorkspaceCodeMode();
}

const RENDERERS = {
  'line-chart': renderLineChart,
  'bar-chart':  renderBarChart,
  'table':      renderTable,
  'html':       renderHtml,
};

const EMBED_RE = /<blackboard-artifact\s+([^>]*)>([\s\S]*?)<\/blackboard-artifact>|```html[^\S\r\n]*\r?\n([\s\S]*?)```/gi;

function appendText(host, text) {
  if (!text) return;
  const block = document.createElement('div');
  block.style.whiteSpace = 'pre-wrap';
  block.textContent = text;
  host.appendChild(block);
}

export function containsRenderableArtifacts(sourceText) {
  const text = String(sourceText || '');
  const hasArtifact = EMBED_RE.test(text);
  EMBED_RE.lastIndex = 0;
  return hasArtifact;
}

export function renderArtifactsInto(host, sourceText) {
  let lastIndex = 0;
  host.innerHTML = '';
  const text = String(sourceText || '');
  let match;
  while ((match = EMBED_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      appendText(host, text.slice(lastIndex, match.index));
    }
    const isTaggedArtifact = Boolean(match[1]);
    const attrs = isTaggedArtifact ? parseTagAttrs(match[1]) : { type: 'html', title: '' };
    const inner = isTaggedArtifact ? match[2] : match[3];
    const wrap = document.createElement('div');
    wrap.className = 'bb-artifact';
    wrap.style.cssText = 'background:var(--bg-2);border:1px solid var(--line);border-radius:6px;padding:8px;margin:6px 0';
    let data = null;
    try { data = JSON.parse(inner); } catch { data = { html: inner }; }
    if (attrs.title && data && typeof data === 'object' && !Array.isArray(data) && !data.title) {
      data = { ...data, title: attrs.title };
    }
    const persistedSource = attrs.type === 'html'
      ? String((data && typeof data === 'object' && !Array.isArray(data) ? data.html : '') || inner || '')
      : JSON.stringify(data ?? inner ?? '', null, 2);
    const persistedTitle = resolveArtifactTitle(String((data && typeof data === 'object' && !Array.isArray(data) && data.title) || attrs.title || ''), persistedSource, attrs.type || 'html');
    const artifactRef = { artifactId: String((data && typeof data === 'object' && !Array.isArray(data) && data.artifact_id) || '').trim() };
    const h = document.createElement('div');
    h.style.cssText = 'font-size:11px;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:6px';
    h.textContent = `${attrs.type} · ${persistedTitle}`;
    wrap.appendChild(h);
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      data = { ...data, title: persistedTitle, artifactRef };
    }
    const renderer = RENDERERS[attrs.type] || renderHtml;
    try { renderer(wrap, data); } catch (err) {
      wrap.appendChild(Object.assign(document.createElement('pre'), { textContent: `(artifact render error: ${err.message})`, style: 'color:var(--c-red);font-size:11px' }));
    }
    host.appendChild(wrap);
    lastIndex = EMBED_RE.lastIndex;
  }
  if (lastIndex < text.length) {
    appendText(host, text.slice(lastIndex));
  }
  EMBED_RE.lastIndex = 0;
}
