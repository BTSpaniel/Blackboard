// "Render as HTML" — calls /api/artifacts/{pid}/render with a preset, opens result in a new tab.
import { api } from '/ui/core/api.js';
import { store } from '/ui/core/store.js';

const PRESETS = [
  { value: 'plan-with-subtasks', label: 'Plan with sub-tasks (collapsible)' },
  { value: 'audit-deliverable', label: 'Audit / client deliverable' },
  { value: 'comparison-grid',   label: 'Comparison grid (sortable)' },
  { value: 'pricing-calculator', label: 'Pricing / interactive sliders' },
  { value: 'live-dashboard',    label: 'Live dashboard' },
];

export function renderHtmlButton(card) {
  const btn = document.createElement('button');
  btn.className = 'bb-btn';
  btn.textContent = 'Render as HTML';
  btn.onclick = async () => {
    const presetLabel = window.prompt(
      `Pick a preset:\n${PRESETS.map((p, i) => `${i + 1}. ${p.label}`).join('\n')}\n\nEnter 1-${PRESETS.length}:`,
      '1',
    );
    if (!presetLabel) return;
    const idx = Math.max(1, Math.min(PRESETS.length, parseInt(presetLabel, 10) || 1)) - 1;
    const preset = PRESETS[idx].value;
    const projectId = store.get().activeProjectId;
    if (!projectId) return;

    const sourceMarkdown = [
      `# ${card.title}`,
      '',
      card.body || '',
      '',
      card.files?.length ? `## Files\n${card.files.map((f) => `- ${f}`).join('\n')}` : '',
      card.verification?.length ? `## Verification\n${card.verification.map((v) => `- ${v}`).join('\n')}` : '',
      card.constraints?.length ? `## Constraints\n${card.constraints.map((c) => `- ${c}`).join('\n')}` : '',
    ].filter(Boolean).join('\n');

    btn.textContent = 'Rendering…';
    btn.disabled = true;
    try {
      const result = await api.renderHtml(projectId, {
        name: `card_${card.id}`,
        source_markdown: sourceMarkdown,
        preset,
      });
      window.open(result.url, '_blank');
    } catch (err) {
      alert(`Render failed: ${err.message}`);
    } finally {
      btn.textContent = 'Render as HTML';
      btn.disabled = false;
    }
  };
  return btn;
}
