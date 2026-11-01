/* Night Shift — frontend logic
   Inbox → Incident → Evidence: one connected experience.
   All model output rendered as escaped untrusted text. */

let state = {
  busy: false,
  inboxData: null,
  selectedSignalKey: null,
  currentIncidentId: null,
  currentIncident: null,
  tab: 'inbox',
};

// ---- Helpers ----
const $ = id => document.getElementById(id);
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function textPre(v) {
  return `<pre>${esc(typeof v === 'string' ? v : JSON.stringify(v, null, 2))}</pre>`;
}
function badge(label, kind = 'neutral') {
  return `<span class="badge ${kind}">${esc(label)}</span>`;
}
function metric(label, value, kind = '') {
  return `<div class="metric ${kind}"><span class="label">${esc(label)}</span><div class="value">${esc(value)}</div></div>`;
}
// API_BASE is empty for same-origin (local dev or same-host deployment).
const API_BASE = (typeof window !== 'undefined' && window.NS_API_BASE) || '';
async function fetchJson(url, method) {
  const r = await fetch(API_BASE + url, { method: method || 'GET' });
  return r.json();
}

// ---- Readable labels ----
const DISP_LABELS = {
  BENIGN: 'Benign',
  ACTIONABLE: 'Actionable',
  NEEDS_HUMAN_REVIEW: 'Needs review',
};
const ROUTE_LABELS = {
  dismiss: 'Dismiss',
  investigate: 'Investigate',
  human_review: 'Review',
  remediate: 'Remediate',
};
const NEXT_ACTION = {
  dismiss: 'No action needed',
  investigate: 'Investigate further',
  human_review: 'Await human review',
  remediate: 'Open in incident workspace',
};
function statusLabel(s) {
  return {
    'tested_patch_awaiting_human_approval': 'Verified patch — awaiting human approval',
    'human_approved': 'Human approved — staging available',
    'simulated_deployed': 'Simulation recorded (no real deployment)',
    'staging_released': 'Released to local staging',
    'staging_stopped': 'Local staging stopped',
    'blocked': 'Blocked: triage did not qualify',
    'remediation_failed': 'Remediation failed: QA rejected all attempts',
    'needs_human_review': 'Needs human review: grounding failed',
    'running': 'Recovery is running',
    'error': 'Recovery error',
  }[s] || s || 'Unknown';
}
function formatMs(ms) {
  if (ms == null) return 'No verified patch';
  return (Number(ms) / 1000).toFixed(1) + 's';
}
function primaryActionLabel(status) {
  return {
    'tested_patch_awaiting_human_approval': 'Approve patch',
    'human_approved': 'Release to local staging',
    'staging_released': 'Stop local staging',
    'staging_stopped': 'Staging stopped',
    'blocked': 'No action available',
    'remediation_failed': 'No action available',
    'needs_human_review': 'No action available',
  }[status] || 'Refresh';
}

// ---- Tab navigation ----
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});
function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => {
    const active = b.dataset.tab === tab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.id === 'tab-' + tab);
  });
}

// ---- Status bar ----
function setStatus(text, live = false) {
  $('statusText').textContent = text;
  $('liveDot').classList.toggle('live', live);
}
function setBusy(v) {
  state.busy = v;
  $('btnAnalyzeInbox').disabled = v;
  $('btnRunWorkflow') && ($('btnRunWorkflow').disabled = v);
}

// ---- Inbox ----
$('btnAnalyzeInbox').onclick = async () => {
  if (state.busy) return;
  setBusy(true);
  setStatus('Analyzing 5 synthetic signals… (synchronous, indeterminate duration)', true);
  $('inboxEmpty').classList.add('hidden');
  $('inboxError').classList.add('hidden');
  $('inboxLoading').classList.remove('hidden');
  $('inboxResultsWrap').classList.add('hidden');

  try {
    const r = await fetchJson('/inbox', 'POST');
    state.inboxData = r;
    $('inboxLoading').classList.add('hidden');
    $('inboxResultsWrap').classList.remove('hidden');
    renderInboxResults(r);
    setStatus('Inbox analysis complete — ' + (r.summary.routes.remediate || 0) + ' signal(s) routed to remediation.');
  } catch (e) {
    $('inboxLoading').classList.add('hidden');
    const err = $('inboxError');
    err.classList.remove('hidden');
    err.innerHTML = `<p class="error">Inbox analysis failed: ${esc(e.message)}</p>`;
    setStatus('Inbox analysis failed.');
  } finally {
    setBusy(false);
  }
};

function renderInboxResults(r) {
  const s = r.summary || {};
  const d = s.dispositions || {};
  const rt = s.routes || {};
  // Compact summary strip: total, dismissed, investigate, review, remediate
  $('inboxSummaryStrip').innerHTML =
    `<span><strong>${s.total || 0}</strong> signals</span>` +
    `<span><strong>${rt.dismiss || 0}</strong> dismissed</span>` +
    `<span><strong>${rt.investigate || 0}</strong> investigate</span>` +
    `<span><strong>${rt.human_review || 0}</strong> human review</span>` +
    `<span><strong>${rt.remediate || 0}</strong> remediate</span>`;

  // Mismatch counts go into evaluation diagnostics, not the main table

  const rows = (r.inbox_results || []).map(x => {
    const pe = x.patch_eligibility || {};
    const dispCls = x.disposition === 'BENIGN' ? 'good' : x.disposition === 'NEEDS_HUMAN_REVIEW' ? 'warn' : '';
    const routeCls = x.policy_route === 'remediate' ? 'good' : x.policy_route === 'human_review' ? 'warn' : '';
    return `<tr class="signal-row" tabindex="0" data-key="${esc(x.log_key)}">
      <td><strong>${esc(x.description || x.log_key)}</strong><br><small style="color:var(--text-muted)">${esc(x.log_key)}</small></td>
      <td>${badge(DISP_LABELS[x.disposition] || x.disposition, dispCls)}</td>
      <td>${badge(ROUTE_LABELS[x.policy_route] || x.policy_route, routeCls)}</td>
      <td><button class="btn secondary sm" data-open="${esc(x.log_key)}" aria-label="Open ${esc(x.description || x.log_key)}">Open</button></td>
    </tr>`;
  }).join('');
  $('inboxTableBody').innerHTML = rows;

  // Row selection
  document.querySelectorAll('.signal-row').forEach(row => {
    row.addEventListener('click', () => selectSignal(row.dataset.key));
    row.addEventListener('keydown', e => {
      if (e.key === 'Enter') selectSignal(row.dataset.key);
      if (e.key === 'ArrowDown') { e.preventDefault(); row.nextElementSibling?.focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); row.previousElementSibling?.focus(); }
    });
  });
  document.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      selectSignal(btn.dataset.open);
    });
  });

  // Evaluation diagnostics
  let evalHtml = '';
  if ((r.classification_mismatches || []).length) {
    evalHtml += `<div class="notice warn"><strong>Classification mismatches</strong><br><small>${r.classification_mismatches.map(m => esc(m.log_key) + ': expected ' + esc(m.expected) + ', got ' + esc(m.actual)).join(' · ')}</small></div>`;
  }
  if ((r.routing_mismatches || []).length) {
    evalHtml += `<div class="notice warn"><strong>Routing mismatches</strong><br><small>${r.routing_mismatches.map(m => esc(m.log_key) + ': expected ' + esc(m.expected) + ', got ' + esc(m.actual)).join(' · ')}</small></div>`;
  }
  // Expected vs actual table
  evalHtml += '<div class="tablewrap"><table><thead><tr><th>Signal</th><th>Expected disposition</th><th>Actual</th><th>Expected route</th><th>Actual route</th><th>Patch-eligible</th></tr></thead><tbody>';
  for (const x of r.inbox_results || []) {
    const pe = x.patch_eligibility || {};
    evalHtml += `<tr><td>${esc(x.log_key)}</td><td>${esc(x.expected_disposition)}</td><td>${esc(x.disposition)}</td><td>${esc(x.expected_route)}</td><td>${esc(x.policy_route)}</td><td>${pe.eligible ? 'Yes' : 'No'}</td></tr>`;
  }
  evalHtml += '</tbody></table></div>';
  if ((r.handoffs || []).length) {
    evalHtml += '<h3>Human review handoffs</h3><div class="tablewrap"><table><thead><tr><th>Signal</th><th>Reason</th></tr></thead><tbody>';
    for (const h of r.handoffs) {
      evalHtml += `<tr><td>${esc(h.log_key)}</td><td>${esc(h.reason)}</td></tr>`;
    }
    evalHtml += '</tbody></table></div>';
  }
  $('inboxEval').innerHTML = evalHtml;

  // If a recovery incident was created, populate the evidence selector
  if (r.recovery_incident_id) {
    state.currentIncidentId = r.recovery_incident_id;
    refreshEvidenceSelector();
  }
}

function selectSignal(key) {
  state.selectedSignalKey = key;
  document.querySelectorAll('.signal-row').forEach(r => {
    r.classList.toggle('selected', r.dataset.key === key);
  });
  const signal = (state.inboxData.inbox_results || []).find(x => x.log_key === key);
  if (!signal) return;

  // Update status bar to reflect this signal immediately
  setStatus(ROUTE_LABELS[signal.policy_route] ? signal.description + ' — ' + ROUTE_LABELS[signal.policy_route] : signal.description);

  // If this is the remediation signal, open its connected incident
  if (signal.policy_route === 'remediate' && state.inboxData.recovery_incident_id) {
    loadIncident(state.inboxData.recovery_incident_id, signal);
  } else {
    renderNonRemediationSignal(signal);
  }
  switchTab('incident');
}

// ---- Incident workspace ----
async function loadIncident(incidentId, signal) {
  setStatus('Loading incident…', true);
  try {
    const d = await fetchJson('/incident/' + incidentId);
    state.currentIncident = d;
    state.currentIncidentId = incidentId;
    renderIncident(d, signal);
    renderEvidence(d);
    setStatus(statusLabel(d.status));
    refreshEvidenceSelector();
  } catch (e) {
    $('incidentEmpty').classList.remove('hidden');
    $('incidentEmpty').innerHTML = `<p class="error">Failed to load incident: ${esc(e.message)}</p>`;
    setStatus('Failed to load incident.');
  }
}

function renderNonRemediationSignal(signal) {
  $('incidentEmpty').classList.add('hidden');
  $('incidentContent').classList.remove('hidden');
  const pe = signal.patch_eligibility || {};
  const isHandoff = signal.policy_route === 'human_review';
  const handoff = (state.inboxData.handoffs || []).find(h => h.log_key === signal.log_key);

  let html = `<h1 class="page-title">${esc(signal.description || signal.log_key)}</h1>
  <p class="page-subtitle">${esc(signal.log_key)} &middot; ${esc(DISP_LABELS[signal.disposition] || signal.disposition)} &middot; ${esc(ROUTE_LABELS[signal.policy_route] || signal.policy_route)}</p>`;

  html += `<div class="metric-row" style="margin-bottom:16px">
    ${metric('Disposition', DISP_LABELS[signal.disposition] || signal.disposition)}
    ${metric('Policy route', ROUTE_LABELS[signal.policy_route] || signal.policy_route)}
    ${metric('Patch-eligible', pe.eligible ? 'Yes' : 'No')}
  </div>`;

  // Show policy override reason FIRST if present (not model explanation)
  if (signal.policy_override) {
    html += `<div class="notice warn" style="margin-bottom:12px"><strong>Policy decision:</strong> ${esc(signal.policy_override)}</div>`;
  }

  html += `<div class="section"><h3>Original log</h3><pre>${esc(signal.log || '—')}</pre></div>`;

  // Model reasoning separately accessible
  if (signal.reason) {
    html += `<details><summary>Model reasoning (unvalidated)</summary>
      <p class="kicker">Disposition: ${esc(signal.model_disposition || '—')} · Severity: ${esc(signal.model_assessed_severity || '—')}</p>
      <pre>${esc(signal.reason || '—')}</pre>
    </details>`;
  }

  if (isHandoff && handoff) {
    html += `<div class="notice" style="margin-top:10px"><strong>Human review handoff</strong><br><small>Attempts: 0 · Timing: null (no patch attempted)</small></div>`;
  }

  $('incidentContent').innerHTML = html;
}

function renderIncident(d, signal) {
  $('incidentEmpty').classList.add('hidden');
  $('incidentContent').classList.remove('hidden');

  const attempts = d.attempts || [];
  const events = d.events || [];
  const patch = d.verified_patch || {};
  const verified = attempts.find(a => a.qa && a.qa.approval && a.verification) || null;
  const firstEng = events.find(e => e.agent === 'engineer');

  // Timeline
  const qaPassed = !!verified;
  const humanApproved = !!d.human_approval;
  const staged = !!d.staging_release?.released;
  const stopped = d.status === 'staging_stopped';
  const sim = !!d.simulated_deployment;

  const steps = [
    ['Sentry', d.log_key ? 'Triage complete' : 'Not run', d.log_key ? 'active' : 'pending'],
    ['Engineer', (firstEng?.fault_injection && qaPassed) ? 'Recovered after rejected attempt' : (firstEng?.fault_injection ? 'Rejected (fault injection)' : 'Proposal interpreted'), firstEng ? 'active' : 'pending'],
    ['QA', qaPassed ? 'Verified patch' : firstEng ? 'Rejected' : 'Not run', qaPassed ? 'active' : (firstEng ? 'blocked' : 'pending')],
    ['Human', humanApproved ? 'Approved' : (qaPassed ? 'Awaiting approval' : 'Not eligible'), humanApproved ? 'active' : (qaPassed ? 'pending' : 'blocked')],
    ['Staging', stopped ? 'Stopped' : (staged ? ('Released :' + d.staging_release.port) : (humanApproved ? 'Ready' : 'Awaiting approval')), stopped ? 'active' : (staged ? 'active' : 'pending')],
    ['Stop', stopped ? 'Stopped' : (staged ? 'Running' : '—'), stopped ? 'active' : 'pending'],
  ];

  let timelineHtml = '<div class="timeline">';
  for (const [agent, detail, cls] of steps) {
    timelineHtml += `<div class="timeline-step ${cls}"><div class="agent">${esc(agent)}</div><div class="detail">${esc(detail)}</div></div>`;
  }
  timelineHtml += '</div>';

  // Primary action button
  const status = d.status;
  let primaryBtn = '';
  if (status === 'tested_patch_awaiting_human_approval') {
    primaryBtn = `<button class="btn primary" id="btnApprove">${primaryActionLabel(status)}</button>`;
  } else if (status === 'human_approved') {
    primaryBtn = `<button class="btn primary" id="btnRelease">${primaryActionLabel(status)}</button>`;
  } else if (status === 'staging_released') {
    primaryBtn = `<button class="btn warn" id="btnStop">${primaryActionLabel(status)}</button>`;
  } else if (status === 'staging_stopped') {
    primaryBtn = `<span class="badge good">Staging stopped — export evidence from the Evidence tab.</span>`;
  }

  // Diff
  let diffHtml = '';
  if (verified) {
    const diff = patch.requirements_diff || verified.verification?.requirements_diff || '';
    diffHtml = `<div class="patch"><div class="patch-head"><strong>Requirements diff</strong>${badge('SHA-256 bound', 'good')}</div><pre class="diff-output">${esc(diff).replace(/^(-.*)$/gm, '<span class="minus">$1</span>').replace(/^(\+.*)$/gm, '<span class="plus">$1</span>')}</pre></div>`;
  }

  // QA summary
  let qaHtml = '';
  if (verified) {
    const v = verified.verification || {};
    const junit = v.junit || {};
    const tests = Object.entries(junit.tests || {});
    const inv = verified.engineer?.investigation || {};
    qaHtml = `<div class="metric-row" style="margin-bottom:10px">
      ${metric('Installed version', v.installed_version || 'N/A', 'good')}
      ${metric('Tests passed', tests.filter(([_,r]) => r === 'passed').length + '/' + tests.length, 'good')}
      ${metric('Artifact SHA-256', String(patch.artifact_sha256 || 'N/A').slice(0, 16) + '…')}
    </div>`;
    if (inv.evidence?.length) {
      qaHtml += '<h3>Source citations (location-validated)</h3><div class="tablewrap"><table><thead><tr><th>Source</th><th>Line</th><th>Finding</th></tr></thead><tbody>';
      for (const e of inv.evidence) {
        qaHtml += `<tr><td>${esc(e.file)}</td><td>${esc(e.line)}</td><td>${esc(e.finding)}</td></tr>`;
      }
      qaHtml += '</tbody></table></div>';
    }
    if (inv.uncertainty) {
      qaHtml += `<div class="notice"><strong>Remaining uncertainty:</strong> ${esc(inv.uncertainty)}</div>`;
    }
    if (v.deterministic_checks) {
      qaHtml += `<details><summary>Deterministic checks</summary>${textPre(v.deterministic_checks)}</details>`;
    }
  } else {
    qaHtml = '<div class="notice warn">No QA-verified patch available.</div>';
  }

  // Signal context
  let signalHtml = '';
  if (signal) {
    signalHtml = `<div class="section"><h3>Selected signal: ${esc(signal.log_key)}</h3>`;
    if (signal.policy_override) {
      signalHtml += `<div class="notice warn"><strong>Policy decision:</strong> ${esc(signal.policy_override)}</div>`;
    }
    signalHtml += `<details style="margin-top:6px"><summary>Model reasoning (unvalidated)</summary><pre>${esc(signal.reason || '—')}</pre></details></div>`;
  }

  // Staging results
  let stagingHtml = '';
  if (d.staging_release) {
    const sr = d.staging_release;
    stagingHtml = `<div class="section"><h3>Local staging</h3>`;
    if (sr.health) {
      stagingHtml += `<div class="metric-row" style="margin-bottom:8px">${metric('Health', sr.health.status || '—', 'good')}${metric('Jinja2', sr.health.jinja2 || '—')}${metric('MarkupSafe', sr.health.MarkupSafe || '—')}</div>`;
    }
    if (sr.smoke) {
      const checks = sr.smoke.checks || {};
      stagingHtml += '<div class="tablewrap"><table><thead><tr><th>Smoke check</th><th>Result</th></tr></thead><tbody>';
      for (const [k, v] of Object.entries(checks)) {
        stagingHtml += `<tr><td>${esc(k)}</td><td class="${v ? 'ok' : 'error'}">${v ? 'Passed' : 'Failed'}</td></tr>`;
      }
      stagingHtml += '</tbody></table></div>';
    }
    if (d.status === 'staging_stopped') {
      stagingHtml += `<div class="notice" style="margin-top:8px"><strong>Staging stopped.</strong> Process terminated, directory cleaned.</div>`;
    }
    stagingHtml += '</div>';
  }

  // Full event/evidence details
  let detailHtml = '<div class="section"><h3>Event detail</h3>';
  for (const a of attempts) {
    const eng = a.engineer || {};
    const inv = eng.investigation || {};
    detailHtml += `<details style="margin:6px 0"><summary>Attempt ${esc(a.attempt)} ${eng.fault_injection ? badge('Fault injection', 'bad') : ''}</summary>`;
    if (inv.evidence_collection) detailHtml += `<div class="notice"><strong>Evidence collection:</strong> ${esc(inv.evidence_collection.selected_by || 'Recorded reads')}</div>`;
    if (a.verification) {
      detailHtml += `<details><summary>QA verification detail</summary>${textPre(a.verification)}</details>`;
    }
    if (eng.raw) detailHtml += `<details><summary>Raw Engineer model output (untrusted text)</summary>${textPre(eng.raw)}</details>`;
    if (a.qa?.raw) detailHtml += `<details><summary>Raw QA model output (untrusted text)</summary>${textPre(a.qa.raw)}</details>`;
    detailHtml += '</details>';
  }
  if (d.human_approval) detailHtml += `<details><summary>Human approval record</summary>${textPre(d.human_approval)}</details>`;
  if (d.staging_release) detailHtml += `<details><summary>Staging release record</summary>${textPre(d.staging_release)}</details>`;
  detailHtml += '</div>';

  // Git deliverable line: branch, commit, diff — only when verified.
  const gitp = d.git_proposal || {};
  let gitLine = '';
  if (gitp && gitp.branch && gitp.proposal_commit && gitp.verified) {
    gitLine = `<details style="display:inline-block;vertical-align:top"><summary class="kicker">Git proposal</summary>
      <div style="background:var(--surface);border:1px solid var(--border-subtle);border-radius:6px;padding:8px 10px;font-size:12px;min-width:280px">
        <div><span class="kicker">Branch</span> <code>${esc(gitp.branch)}</code></div>
        <div><span class="kicker">Commit</span> <code>${esc(String(gitp.proposal_commit).slice(0, 12))}</code></div>
        <pre style="margin:6px 0 0;font-size:12px;white-space:pre-wrap">${esc(gitp.diff || '')}</pre>
      </div>
    </details>`;
  } else if (gitp && gitp.error) {
    gitLine = `<span>${badge('Git deliverable failed verification', 'bad')}</span>`;
  }

  $('incidentContent').innerHTML = `
    <div class="page-toolbar">
      <div class="page-toolbar-left">
        <h1 class="page-title">${esc(signal?.description || d.log_key || 'Recovery incident')}</h1>
        <p class="page-subtitle">${esc(d.log_key || '—')} &middot; ${esc(statusLabel(status))}</p>
      </div>
      <div class="page-toolbar-right">
        ${primaryBtn}
        <button class="btn secondary sm" id="btnRefresh">Refresh</button>
        <details class="demo-badge" style="margin-left:4px">
          <summary>Simulation</summary>
          <div style="position:absolute;right:0;margin-top:4px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px;min-width:200px;z-index:100">
            <button class="btn secondary sm" id="btnSimulate" disabled>Record simulation only</button>
          </div>
        </details>
      </div>
    </div>
    <div class="summary-strip" style="margin-bottom:16px">
      <span>Alert → tested patch <strong>${formatMs(d.elapsed_alert_to_tested_patch_ms)}</strong></span>
      <span>Attempts <strong>${attempts.length}</strong></span>
      ${firstEng?.fault_injection ? `<span>${badge('Fault injection on attempt 1', 'bad')}</span>` : ''}
      ${gitLine}
    </div>
    <div class="incident-decision">
      <div>
        <h3 style="margin-top:0">Patch review</h3>
        ${diffHtml}
      </div>
      <div>
        <h3 style="margin-top:0">QA verdict</h3>
        ${qaHtml}
      </div>
    </div>
    <div class="section"><h3>Workflow</h3>${timelineHtml}</div>
    ${signalHtml}
    <div class="section full">${stagingHtml}</div>
    <div class="section full">${detailHtml}</div>
  `;

  // Wire up action buttons
  const approveBtn = $('btnApprove');
  if (approveBtn) approveBtn.onclick = () => doApprove();
  const releaseBtn = $('btnRelease');
  if (releaseBtn) releaseBtn.onclick = () => doRelease();
  const stopBtn = $('btnStop');
  if (stopBtn) stopBtn.onclick = () => doStop();
  const refreshBtn = $('btnRefresh');
  if (refreshBtn) refreshBtn.onclick = () => loadIncident(state.currentIncidentId, signal);
  const simBtn = $('btnSimulate');
  if (simBtn) {
    simBtn.disabled = status !== 'human_approved' || !!d.simulated_deployment;
    simBtn.onclick = () => doSimulate();
  }
}

// ---- Actions ----
async function doApprove() {
  setStatus('Recording human approval…', true);
  try {
    const r = await fetchJson(`/incident/${state.currentIncidentId}/approve`, 'POST');
    if (r.approved) {
      setStatus('Human approval recorded — staging available.');
    } else {
      setStatus('Approval rejected: ' + (r.error || 'unknown'));
    }
    await reloadIncident();
  } catch (e) {
    setStatus('Approval failed: ' + e.message);
  }
}

async function doRelease() {
  setStatus('Releasing to local staging…', true);
  try {
    const r = await fetchJson(`/incident/${state.currentIncidentId}/staging-release`, 'POST');
    if (r.released) {
      setStatus('Released to local staging on 127.0.0.1:' + r.port);
    } else {
      setStatus('Staging release failed: ' + (r.error || 'unknown'));
    }
    await reloadIncident();
  } catch (e) {
    setStatus('Staging release failed: ' + e.message);
  }
}

async function doStop() {
  setStatus('Stopping local staging…', true);
  try {
    const r = await fetchJson(`/incident/${state.currentIncidentId}/staging-stop`, 'POST');
    if (r.stopped) {
      setStatus('Local staging stopped.');
    } else {
      setStatus('Stop failed: ' + (r.error || 'unknown'));
    }
    await reloadIncident();
  } catch (e) {
    setStatus('Stop failed: ' + e.message);
  }
}

async function doSimulate() {
  try {
    const r = await fetchJson(`/incident/${state.currentIncidentId}/simulate-deploy`, 'POST');
    setStatus(r.simulated ? 'Simulation recorded (no real deployment).' : 'Simulation rejected: ' + (r.error || 'unknown'));
    await reloadIncident();
  } catch (e) {
    setStatus('Simulation failed: ' + e.message);
  }
}

async function reloadIncident() {
  if (!state.currentIncidentId) return;
  const d = await fetchJson('/incident/' + state.currentIncidentId);
  state.currentIncident = d;
  const signal = state.inboxData ? (state.inboxData.inbox_results || []).find(x => x.log_key === state.selectedSignalKey) : null;
  renderIncident(d, signal);
  setStatus(statusLabel(d.status));
  renderEvidence(d);
  refreshEvidenceSelector();
}

// ---- Evidence tab ----
async function refreshEvidenceSelector() {
  try {
    const incidents = await fetchJson('/incidents');
    const sel = $('evidenceIncidentSelect');
    const current = sel.value;
    sel.innerHTML = '<option value="">Select incident…</option>';
    for (const inc of incidents) {
      const opt = document.createElement('option');
      opt.value = inc.id;
      opt.textContent = inc.id.slice(0, 8) + '… · ' + statusLabel(inc.status);
      if (inc.id === state.currentIncidentId) opt.selected = true;
      sel.appendChild(opt);
    }
    $('btnExport').disabled = !state.currentIncidentId;
  } catch (e) {
    // /incidents may not exist yet
  }
}

$('evidenceIncidentSelect').addEventListener('change', async (e) => {
  const id = e.target.value;
  state.currentIncidentId = id || null;
  $('btnExport').disabled = !id;
  if (id) {
    try {
      const d = await fetchJson('/incident/' + id);
      state.currentIncident = d;
      renderEvidence(d);
    } catch (err) {
      $('evidenceContent').innerHTML = `<div class="notice bad">Failed to load: ${esc(err.message)}</div>`;
    }
  } else {
    $('evidenceContent').innerHTML = '';
  }
});

function renderEvidence(d) {
  const attempts = d.attempts || [];
  let html = '';

  // Timing
  $('evidRecoveryTiming').textContent = formatMs(d.elapsed_alert_to_tested_patch_ms);
  $('evidInboxTiming').textContent = state.inboxData ? formatMs(state.inboxData.elapsed_ms) : '—';

  // Advisory sources
  const sentry = (d.events || []).find(e => e.agent === 'sentry') || {};
  if (sentry.advisory_metadata) {
    html += '<div class="section"><h3>Advisory sources (trusted)</h3>';
    for (const [cve, meta] of Object.entries(sentry.advisory_metadata)) {
      html += `<details><summary>${esc(cve)} — ${esc(meta.description || '')}</summary>
        <p class="kicker">GHSA: ${esc(meta.ghsa || '—')} · Affected: ${esc(meta.affected_versions || '—')} · Patched: ${esc(meta.patched_version || '—')}</p>
        <p class="kicker">Source: ${esc(meta.source || '—')}</p>
      </details>`;
    }
    html += '</div>';
  }

  // Per-attempt evidence
  for (const a of attempts) {
    const eng = a.engineer || {};
    const inv = eng.investigation || {};
    const v = a.verification || {};
    html += `<div class="section"><h3>Attempt ${esc(a.attempt)} ${eng.fault_injection ? badge('Fault injection', 'bad') : ''}</h3>`;
    if (inv.tool_calls?.length) {
      html += `<details><summary>Orchestrator-selected evidence reads</summary>${textPre(inv.tool_calls)}</details>`;
    }
    if (inv.evidence?.length) {
      html += `<details><summary>Source citations (location-validated)</summary>
        <p class="kicker" style="margin-bottom:6px">Validation confirms each cited file was actually read and the cited line number exists in it. It does not confirm that the stated finding is semantically supported by that exact line — read the citation yourself before relying on it.</p>
        <div class="tablewrap"><table><thead><tr><th>Source</th><th>Line</th><th>Finding</th></tr></thead><tbody>`;
      for (const e of inv.evidence) {
        html += `<tr><td>${esc(e.file)}</td><td>${esc(e.line)}</td><td>${esc(e.finding)}</td></tr>`;
      }
      html += '</tbody></table></div></details>';
    }
    if (v.junit) {
      html += `<details><summary>JUnit structured results</summary>${textPre(v.junit)}</details>`;
    }
    if (v.test_output) {
      html += `<details><summary>Full test output (truncated)</summary>${textPre(v.test_output)}</details>`;
    }
    if (v.deterministic_checks) {
      html += `<details><summary>Deterministic checks</summary>${textPre(v.deterministic_checks)}</details>`;
    }
    if (eng.raw) {
      html += `<details><summary>Raw Engineer model output (untrusted text)</summary>${textPre(eng.raw)}</details>`;
    }
    if (a.qa?.raw) {
      html += `<details><summary>Raw QA model output (untrusted text)</summary>${textPre(a.qa.raw)}</details>`;
    }
    html += '</div>';
  }

  if (d.human_approval) {
    html += `<div class="section"><h3>Human approval</h3>${textPre(d.human_approval)}</div>`;
  }
  if (d.staging_release) {
    html += `<div class="section"><h3>Staging release</h3>${textPre(d.staging_release)}</div>`;
  }

  $('evidenceContent').innerHTML = html || '<div class="empty-state">No evidence available for this incident.</div>';
}

$('btnExport').onclick = () => {
  if (state.currentIncidentId) {
    window.location.href = '/export/' + state.currentIncidentId;
  }
};

// ---- Demo tools ----
$('btnRunWorkflow').onclick = async () => {
  if (state.busy) return;
  setBusy(true);
  setStatus('Starting standalone recovery…', true);
  $('demoOut').innerHTML = '';
  try {
    const start = await fetchJson('/workflow', 'POST');
    const id = start.id;
    const poll = async () => {
      const st = await fetchJson('/workflow/status/' + id);
      if (st.running) {
        setStatus('Recovery running — ' + (st.agents_completed || []).join(' → ') + ' · ' + st.event_count + ' events', true);
        setTimeout(poll, 1500);
        return;
      }
      setBusy(false);
      const d = await fetchJson('/incident/' + id);
      state.currentIncident = d;
      state.currentIncidentId = id;
      setStatus('Standalone recovery complete: ' + statusLabel(d.status));
      $('demoOut').innerHTML = `<p class="ok">Recovery complete. <button class="btn secondary sm" id="btnOpenStandalone">Open in Incident tab</button></p>`;
      $('btnOpenStandalone').onclick = () => { loadIncident(id, null); switchTab('incident'); };
      refreshEvidenceSelector();
    };
    setTimeout(poll, 750);
  } catch (e) {
    setBusy(false);
    setStatus('Recovery failed: ' + e.message);
    $('demoOut').innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
};

async function runTriage(key) {
  setStatus('Triaging ' + key + '…', true);
  $('demoOut').innerHTML = '<p class="kicker">Loading…</p>';
  try {
    const d = await fetchJson('/triage/' + encodeURIComponent(key));
    $('demoOut').innerHTML = textPre(d);
    setStatus(d.error ? 'Triage error' : 'Triage complete');
  } catch (e) {
    $('demoOut').innerHTML = `<p class="error">${esc(e.message)}</p>`;
    setStatus('Triage failed.');
  }
}
$('btnTriageActionable').onclick = () => runTriage('actionable');
$('btnTriageBenign').onclick = () => runTriage('benign');
$('btnTriageAmbiguous').onclick = () => runTriage('ambiguous');
