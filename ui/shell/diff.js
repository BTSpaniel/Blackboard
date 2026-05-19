import { api } from '/ui/core/api.js';

function colorize(diff) {
  return diff.split('\n').map((line) => {
    if (line.startsWith('+++') || line.startsWith('---')) return `<span class="bb-diff__hdr">${escape(line)}</span>`;
    if (line.startsWith('@@')) return `<span class="bb-diff__hdr">${escape(line)}</span>`;
    if (line.startsWith('+')) return `<span class="bb-diff__add">${escape(line)}</span>`;
    if (line.startsWith('-')) return `<span class="bb-diff__del">${escape(line)}</span>`;
    return escape(line);
  }).join('\n');
}

function escape(t) {
  return String(t).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
}

export function openDiffModal({ jobId, diff, review, onMerge }) {
  const modal = document.getElementById('diff-modal');
  const body = document.getElementById('diff-body');
  const footer = document.getElementById('diff-footer');
  body.innerHTML = colorize(diff || '(no diff)');
  footer.innerHTML = `
    <div style="flex:1;color:var(--fg-2);font-size:12px">
      ${review ? `Lint clean: ${review.lint_clean ? '✓' : '✗'} · Tests: ${review.tests_passed ? '✓' : '✗'}` : ''}
    </div>
    <button class="bb-btn" data-close>Cancel</button>
    <button class="bb-btn bb-btn--primary" id="diff-merge">Approve & Merge</button>
  `;
  modal.hidden = false;
  modal.querySelectorAll('[data-close]').forEach((el) => { el.onclick = () => { modal.hidden = true; }; });
  document.getElementById('diff-merge').onclick = async () => {
    try {
      await api.mergeJob(jobId, { confirm: true });
      modal.hidden = true;
      if (onMerge) onMerge();
    } catch (err) {
      alert(`Merge failed: ${err.message}`);
    }
  };
}
