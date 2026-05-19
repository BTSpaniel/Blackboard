import { api } from '/ui/core/api.js';
import { bus } from '/ui/core/bus.js';
import { store } from '/ui/core/store.js';
import { pickDirectory } from '/ui/shell/directory_picker.js';
import { resolvePreferredWorkingDir, saveWorkingDir } from '/ui/shell/working_directory.js';

const RUN_PIPELINE = ['inbox', 'designing', 'planning', 'ready', 'executing'];
const ACTIVE_JOB_STATUSES = new Set(['pending', 'paused', 'running', 'merging']);

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function pendingQuestions(card) {
  const pending = card?.metadata?.pending_questions;
  return pending && Array.isArray(pending.questions) && pending.questions.length ? pending : null;
}

function resumeState(card) {
  const resume = card?.metadata?.resume_from;
  return resume?.status ? resume : null;
}

function coordinationState(card) {
  const coordination = card?.metadata?.coordination;
  return coordination?.status ? coordination : null;
}

function mergeRecommendation(card) {
  const candidates = card?.metadata?.merge_candidates;
  return Array.isArray(candidates) && candidates.length ? candidates : [];
}

function executionObjective(card) {
  return card?.metadata?.execution_objective || card?.body || card?.title || '';
}

export function openCardModal(card) {
  const modal = document.getElementById('card-modal');
  const title = document.getElementById('card-modal-title');
  const body = document.getElementById('card-modal-body');
  const footer = document.getElementById('card-modal-footer');
  const projectId = store.get().activeProjectId;
  const panel = modal.querySelector('.bb-modal__panel');
  if (panel) panel.classList.add('bb-modal__panel--card-editor');
  title.textContent = card.title;
  body.innerHTML = `
    ${renderCheckpoint(card)}
    ${renderResumeState(card)}
    ${renderCoordinationState(card)}
    ${renderMergeRecommendation(card)}
    <section class="bb-card-editor__summary">
      <div class="bb-card-editor__summary-item">
        <span class="bb-card-editor__summary-label">Card</span>
        <strong>${escapeHtml(card.id || 'unsaved')}</strong>
      </div>
      <div class="bb-card-editor__summary-item">
        <span class="bb-card-editor__summary-label">Status</span>
        <strong>${escapeHtml(card.status || 'inbox')}</strong>
      </div>
      <div class="bb-card-editor__summary-item">
        <span class="bb-card-editor__summary-label">Files</span>
        <strong>${Array.isArray(card.files) ? card.files.length : 0}</strong>
      </div>
      <div class="bb-card-editor__summary-item">
        <span class="bb-card-editor__summary-label">Checks</span>
        <strong>${Array.isArray(card.verification) ? card.verification.length : 0}</strong>
      </div>
    </section>
    <div class="bb-card-editor__grid">
      <div class="bb-modal__field bb-card-editor__field bb-card-editor__field--full">
        <label for="cm-title">Title</label>
        <input id="cm-title" class="bb-input" value="${escapeHtml(card.title)}" />
      </div>
      <div class="bb-modal__field bb-card-editor__field bb-card-editor__field--full">
        <label for="cm-objective">Execution objective</label>
        <div class="bb-card-editor__hint">This is the full AI/job instruction. It is kept in metadata instead of the visible board note.</div>
        <textarea id="cm-objective" rows="10" class="bb-input bb-card-editor__textarea bb-card-editor__textarea--body">${escapeHtml(executionObjective(card))}</textarea>
      </div>
      <div class="bb-modal__field bb-card-editor__field">
        <label for="cm-body">Body</label>
        <div class="bb-card-editor__hint">Visible board note: briefly explain why this card belongs in its current status.</div>
        <textarea id="cm-body" rows="8" class="bb-input bb-card-editor__textarea">${escapeHtml(card.body || '')}</textarea>
      </div>
      <div class="bb-modal__field bb-card-editor__field">
        <label for="cm-files">Files</label>
        <div class="bb-card-editor__hint">One path per line. Add likely targets to help coding jobs scope faster.</div>
        <textarea id="cm-files" rows="8" class="bb-input bb-card-editor__textarea">${escapeHtml((card.files || []).join('\n'))}</textarea>
      </div>
      <div class="bb-modal__field bb-card-editor__field">
        <label for="cm-verify">Verification</label>
        <div class="bb-card-editor__hint">One requirement per line. Use specific, testable outcomes.</div>
        <textarea id="cm-verify" rows="8" class="bb-input bb-card-editor__textarea">${escapeHtml((card.verification || []).join('\n'))}</textarea>
      </div>
      <div class="bb-modal__field bb-card-editor__field">
        <label for="cm-constraints">Constraints</label>
        <div class="bb-card-editor__hint">One rule per line. Keep stack, file, and safety constraints visible.</div>
        <textarea id="cm-constraints" rows="8" class="bb-input bb-card-editor__textarea">${escapeHtml((card.constraints || []).join('\n'))}</textarea>
      </div>
    </div>
  `;
  footer.innerHTML = `
    <button class="bb-btn bb-btn--danger" id="cm-delete">Delete</button>
    <div style="flex:1"></div>
    <button class="bb-btn" id="cm-execute">Run as Job</button>
    <button class="bb-btn bb-btn--primary" id="cm-save">Save</button>
  `;
  const executeBtn = document.getElementById('cm-execute');
  const activeJob = activeJobForCard(projectId, card.id);
  if (activeJob) {
    executeBtn.disabled = true;
    executeBtn.textContent = activeJob.status === 'paused' ? `Paused ${activeJob.job_id}` : `Running ${activeJob.job_id}`;
  }
  modal.hidden = false;
  document.querySelectorAll('#card-modal [data-close]').forEach((el) => {
    el.onclick = () => { modal.hidden = true; };
  });
  body.querySelectorAll('[data-checkpoint-answer]').forEach((btn) => {
    btn.addEventListener('click', () => answerCheckpoint(card, btn.dataset.questionId, btn.dataset.optionId));
  });

  document.getElementById('cm-save').onclick = async () => {
    const metadata = { ...(card.metadata || {}) };
    metadata.execution_objective = document.getElementById('cm-objective').value;
    metadata.status_note = document.getElementById('cm-body').value;
    const updates = {
      title: document.getElementById('cm-title').value,
      body: document.getElementById('cm-body').value,
      files: splitLines(document.getElementById('cm-files').value),
      verification: splitLines(document.getElementById('cm-verify').value),
      constraints: splitLines(document.getElementById('cm-constraints').value),
      metadata,
    };
    try {
      await api.updateCard(projectId, card.id, updates);
      modal.hidden = true;
      const board = await api.board(projectId);
      store.setBoard(board);
    } catch (err) {
      alert(`Save failed: ${err.message}`);
    }
  };

  document.getElementById('cm-delete').onclick = async () => {
    if (!confirm('Delete this card?')) return;
    try {
      await api.deleteCard(projectId, card.id);
      modal.hidden = true;
      const board = await api.board(projectId);
      store.setBoard(board);
    } catch (err) {
      alert(`Delete failed: ${err.message}`);
    }
  };

  executeBtn.onclick = async () => {
    if (executeBtn.disabled) return;
    executeBtn.disabled = true;
    executeBtn.textContent = 'Checking jobs…';
    const metadata = { ...(card.metadata || {}) };
    metadata.execution_objective = document.getElementById('cm-objective').value;
    metadata.status_note = document.getElementById('cm-body').value;
    const updates = {
      title: document.getElementById('cm-title').value,
      body: document.getElementById('cm-body').value,
      files: splitLines(document.getElementById('cm-files').value),
      verification: splitLines(document.getElementById('cm-verify').value),
      constraints: splitLines(document.getElementById('cm-constraints').value),
      metadata,
    };
    try {
      const latestJobs = await api.listJobs();
      store.setJobs(latestJobs || []);
      const active = activeJobForCard(projectId, card.id, latestJobs || []);
      if (active) {
        bus.emit('app:toast', { kind: 'info', text: `Job ${active.job_id} is already running for this card.` });
        executeBtn.textContent = `Running ${active.job_id}`;
        return;
      }
      const cwd = await pickDirectory({
        initial: await resolvePreferredWorkingDir(),
        title: 'Choose job working directory',
        confirmLabel: 'Run here',
      });
      if (!cwd) {
        executeBtn.disabled = false;
        executeBtn.textContent = 'Run as Job';
        return;
      }
      saveWorkingDir(cwd);
      executeBtn.textContent = 'Submitting…';
      if (metadata.resume_from) {
        metadata.last_resume_from = metadata.resume_from;
        delete metadata.resume_from;
      }
      await api.updateCard(projectId, card.id, { ...updates, metadata });
      await advanceCardPipeline(projectId, card.id, card.status, 'executing');
      const result = await api.submitJob({
        objective: metadata.execution_objective || updates.title,
        cwd,
        files: updates.files,
        constraints: updates.constraints,
        verification: updates.verification,
        context: checkpointAnswerContext(card),
        project_id: projectId,
        card_id: card.id,
      });
      if (!result?.orchestrated) {
        await api.updateCard(projectId, card.id, { job_id: result.job_id });
      }
      store.setJobs(await api.listJobs());
      modal.hidden = true;
      const board = await api.board(projectId);
      store.setBoard(board);
      const toastText = result?.orchestrated
        ? `Orchestrated into ${Number(result?.child_card_ids?.length || 0) || 0} child jobs. Starting with ${result.job_id}.`
        : `Job ${result.job_id} submitted.`;
      bus.emit('app:toast', { kind: 'info', text: toastText });
    } catch (err) {
      executeBtn.disabled = false;
      executeBtn.textContent = 'Run as Job';
      alert(`Job submit failed: ${err.message}`);
    }
  };
}

function splitLines(text) {
  return String(text || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
}

function renderCheckpoint(card) {
  const checkpoint = pendingQuestions(card);
  if (!checkpoint) return '';
  return `
    <section class="bb-checkpoint">
      <div class="bb-checkpoint__eyebrow">Checkpoint · user input required</div>
      <div class="bb-checkpoint__reason">${escapeHtml(checkpoint.reason || 'The agent needs a decision before continuing.')}</div>
      ${(checkpoint.questions || []).map((q) => `
        <div class="bb-checkpoint__question">
          <strong>${escapeHtml(q.prompt || 'Choose an option')}</strong>
          <div class="bb-checkpoint__options">
            ${(q.options || []).map((opt) => `
              <button class="bb-btn" type="button" data-checkpoint-answer="1" data-question-id="${escapeHtml(q.id)}" data-option-id="${escapeHtml(opt.id)}">${escapeHtml(opt.label)}</button>
            `).join('')}
          </div>
        </div>
      `).join('')}
    </section>
  `;
}

function renderResumeState(card) {
  const resume = resumeState(card);
  if (!resume) return '';
  return `
    <section class="bb-checkpoint bb-checkpoint--resume">
      <div class="bb-checkpoint__eyebrow">Iteration restart point</div>
      <div class="bb-checkpoint__reason">Next run will treat this card as restarting from <strong>${escapeHtml(resume.status)}</strong>${resume.previous_status ? ` after being moved from ${escapeHtml(resume.previous_status)}` : ''}.</div>
    </section>
  `;
}

function renderCoordinationState(card) {
  const coordination = coordinationState(card);
  if (!coordination) return '';
  const conflicts = Array.isArray(coordination.conflicts) ? coordination.conflicts : [];
  return `
    <section class="bb-checkpoint bb-checkpoint--coordination">
      <div class="bb-checkpoint__eyebrow">Job coordination pause</div>
      <div class="bb-checkpoint__reason">${escapeHtml(coordination.reason || 'This job is paused to avoid crossing another active job.')}</div>
      ${coordination.related_job_id ? `<div>Related job: <strong>${escapeHtml(coordination.related_job_id)}</strong></div>` : ''}
      ${coordination.related_card_id ? `<div>Related card: <strong>${escapeHtml(coordination.related_card_id)}</strong></div>` : ''}
      ${conflicts.length ? `<div>Overlap: ${conflicts.map((item) => `<code>${escapeHtml(item)}</code>`).join(', ')}</div>` : ''}
    </section>
  `;
}

function renderMergeRecommendation(card) {
  const candidates = mergeRecommendation(card);
  if (!candidates.length) return '';
  return `
    <section class="bb-checkpoint bb-checkpoint--merge">
      <div class="bb-checkpoint__eyebrow">Semantic merge suggestion</div>
      <div class="bb-checkpoint__reason">This job appears related to other active or completed jobs. Review before merging branches.</div>
      ${candidates.map((candidate) => `
        <div class="bb-checkpoint__question">
          <strong>${escapeHtml(candidate.job_id)}${candidate.card_id ? ` · card ${escapeHtml(candidate.card_id)}` : ''} · ${(Number(candidate.score || 0) * 100).toFixed(0)}%</strong>
          <div>${escapeHtml(candidate.objective || '')}</div>
          ${(candidate.reasons || []).length ? `<div>${candidate.reasons.map((reason) => `<code>${escapeHtml(reason)}</code>`).join(' ')}</div>` : ''}
        </div>
      `).join('')}
    </section>
  `;
}

function checkpointAnswerContext(card) {
  const answers = card?.metadata?.checkpoint_answers;
  const resume = resumeState(card);
  const lines = [];
  if (resume) {
    lines.push(`User manually rerouted this card to restart from pipeline state: ${resume.status}.`);
    if (resume.reason) lines.push(`Reroute reason: ${resume.reason}.`);
  }
  if (Array.isArray(answers) && answers.length) {
    lines.push('User answered previous checkpoint questions:');
    lines.push(...answers.slice(-6).map((answer) => `- ${answer.prompt || answer.question_id}: ${answer.value || answer.label || answer.option_id}`));
  }
  return lines.join('\n');
}

async function answerCheckpoint(card, questionId, optionId) {
  const projectId = store.get().activeProjectId;
  const checkpoint = pendingQuestions(card);
  const question = (checkpoint?.questions || []).find((q) => q.id === questionId);
  const option = (question?.options || []).find((opt) => opt.id === optionId);
  if (!projectId || !question || !option) return;
  const metadata = { ...(card.metadata || {}) };
  const answers = Array.isArray(metadata.checkpoint_answers) ? [...metadata.checkpoint_answers] : [];
  answers.push({
    question_id: question.id,
    prompt: question.prompt || '',
    option_id: option.id,
    label: option.label || '',
    value: option.value || option.label || '',
    answered_at: Date.now() / 1000,
  });
  metadata.checkpoint_answers = answers;
  delete metadata.pending_questions;
  metadata.last_checkpoint_answer = answers[answers.length - 1];
  metadata.resume_from = {
    status: 'ready',
    previous_status: card.status,
    reason: 'checkpoint answered by user',
    moved_at: Date.now() / 1000,
  };
  try {
    const updated = await api.updateCard(projectId, card.id, { metadata, status: 'ready', progress: Math.max(0, Number(card.progress || 0) - 10) });
    const board = await api.board(projectId);
    store.setBoard(board);
    bus.emit('checkpoint:answered', { project_id: projectId, card: updated, answer: metadata.last_checkpoint_answer });
    bus.emit('app:toast', { kind: 'success', text: 'Checkpoint answered. Card moved back to ready.' });
    document.getElementById('card-modal').hidden = true;
  } catch (err) {
    alert(`Checkpoint answer failed: ${err.message}`);
  }
}

function activeJobForCard(projectId, cardId, jobs = store.get().jobs || []) {
  const currentCard = (store.get().board?.cards || []).find((entry) => String(entry?.id || '') === String(cardId || '')) || null;
  const orchestration = currentCard?.metadata?.orchestration || {};
  const childJobIds = new Set(Array.isArray(orchestration.child_job_ids) ? orchestration.child_job_ids.map((item) => String(item || '')) : []);
  return jobs.find((job) => {
    if (!ACTIVE_JOB_STATUSES.has(job.status)) return false;
    const task = job.task || {};
    return (
      (task.project_id === projectId && task.card_id === cardId)
      || job.card_id === cardId
      || (task.project_id === projectId && task.parent_card_id === cardId)
      || (task.project_id === projectId && task.root_card_id === cardId)
      || childJobIds.has(String(job.job_id || ''))
    );
  }) || null;
}

async function advanceCardPipeline(projectId, cardId, currentStatus, targetStatus) {
  const start = RUN_PIPELINE.indexOf(currentStatus);
  const end = RUN_PIPELINE.indexOf(targetStatus);
  if (start === -1 || end === -1 || start >= end) {
    if (currentStatus !== targetStatus) {
      await api.updateCard(projectId, cardId, { status: targetStatus });
    }
    return;
  }
  for (const status of RUN_PIPELINE.slice(start + 1, end + 1)) {
    await api.updateCard(projectId, cardId, { status });
  }
}
