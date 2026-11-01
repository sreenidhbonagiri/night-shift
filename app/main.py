"""Night Shift — backend with approval gate and simulated deployment.

State-changing endpoints are POST. Incidents are keyed by UUID with per-incident
locks protecting approval/state transitions against concurrent requests.
Note: these changes do NOT make the application ready for public hosting.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app.workflow import run_incident
from app.sentry_agent import triage, SYNTHETIC_FIXTURES
from app.approval import approve, simulate_deploy
from app import staging

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3:4b"
ALLOWED = {"CRITICAL", "WARNING", "INFO", "FALSE_POSITIVE"}

app = FastAPI(title="Night Shift")

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# In-memory store for incidents by UUID; keyed for approval binding.
_incidents: dict = {}
_incident_locks: dict = {}
_store_lock = threading.Lock()


def _get_lock(incident_id: str) -> threading.Lock:
    with _store_lock:
        if incident_id not in _incident_locks:
            _incident_locks[incident_id] = threading.Lock()
        return _incident_locks[incident_id]


@app.get("/health")
def health():
    return {"status": "ok", "service": "night-shift", "checkpoint": 5}


@app.get("/sentry-classify")
def sentry_classify():
    """Checkpoint 1 endpoint preserved: classify the benign synthetic log."""
    synthetic_log = (
        "[SYNTHETIC — BENIGN] 2024-06-01T12:00:00Z "
        "service=payment-gateway level=INFO "
        "msg='Scheduled health check completed successfully'"
    )

    system_prompt = (
        "You are the Night Shift Sentry agent. "
        "Classify the log as one of: CRITICAL, WARNING, INFO, FALSE_POSITIVE. "
        "Return ONLY a JSON object with keys 'classification' and 'reason'. "
        "The reason must be one sentence."
    )

    payload = {
        "model": MODEL,
        "think": False,
        "format": "json",
        "prompt": f"{system_prompt}\n\nLog line:\n{synthetic_log}\n\nClassification:",
        "stream": False,
        "options": {"num_predict": 128, "temperature": 0.0},
    }

    start = time.perf_counter()
    try:
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"error": str(exc), "model": MODEL, "elapsed_ms": elapsed_ms, "log": synthetic_log}
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

    raw = data.get("response", "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"error": f"Malformed JSON: {exc}", "raw": raw, "model": MODEL, "elapsed_ms": elapsed_ms, "log": synthetic_log}

    classification = parsed.get("classification", "").upper()
    reason = parsed.get("reason", "").strip()

    if classification not in ALLOWED:
        return {"error": f"Invalid classification: {classification}", "raw": raw, "model": MODEL, "elapsed_ms": elapsed_ms, "log": synthetic_log}
    if not reason:
        return {"error": "Empty reason", "raw": raw, "model": MODEL, "elapsed_ms": elapsed_ms, "log": synthetic_log}

    return {"classification": classification, "reason": reason, "model": MODEL, "elapsed_ms": elapsed_ms, "log": synthetic_log}


@app.post("/workflow")
def workflow():
    """Run the full Sentry → Engineer → QA incident workflow."""
    incident = run_incident(log_key="actionable", fault_inject_first=True)
    with _store_lock:
        _incidents[incident["id"]] = incident
    return incident


@app.get("/triage/{log_key}")
def triage_endpoint(log_key: str):
    if log_key not in SYNTHETIC_FIXTURES:
        return {"error": f"Unknown log_key: {log_key}. Allowed: {list(SYNTHETIC_FIXTURES)}"}
    return triage(log_key)


@app.post("/incident/{incident_id}/approve")
def approve_endpoint(incident_id: str):
    """Record human approval for a specific incident and its verified patch."""
    with _get_lock(incident_id):
        incident = _incidents.get(incident_id)
        return approve(incident, actor="human")


@app.post("/incident/{incident_id}/simulate-deploy")
def simulate_deploy_endpoint(incident_id: str):
    """Record a simulated deployment, gated on explicit human approval."""
    with _get_lock(incident_id):
        incident = _incidents.get(incident_id)
        return simulate_deploy(incident)


@app.post("/incident/{incident_id}/staging-release")
def staging_release_endpoint(incident_id: str):
    """Release the human-approved artifact to REAL local staging on 127.0.0.1.

    Distinct from simulate-deploy: this runs the approved snapshot in a
    separate environment, validates health/smoke, and records the release.
    """
    with _get_lock(incident_id):
        incident = _incidents.get(incident_id)
        if not incident:
            return {"released": False, "error": "Incident not found",
                    "incident_id": incident_id}
        return staging.release(incident)


@app.post("/incident/{incident_id}/staging-stop")
def staging_stop_endpoint(incident_id: str):
    with _get_lock(incident_id):
        incident = _incidents.get(incident_id)
        if not incident:
            return {"stopped": False, "error": "Incident not found",
                    "incident_id": incident_id}
        return staging.stop(incident)


@app.get("/incident/{incident_id}/staging-status")
def staging_status_endpoint(incident_id: str):
    incident = _incidents.get(incident_id)
    if not incident:
        return {"error": "Incident not found", "incident_id": incident_id}
    return staging.status(incident)


@app.get("/incident/{incident_id}")
def get_incident(incident_id: str):
    incident = _incidents.get(incident_id)
    if not incident:
        return {"error": "Incident not found", "incident_id": incident_id}
    return incident


@app.get("/export/{incident_id}")
def export_incident(incident_id: str):
    """Download JSON evidence for the selected incident, including approval and simulation events."""
    incident = _incidents.get(incident_id)
    if not incident:
        return {"error": "Incident not found. Run a workflow first.", "incident_id": incident_id}
    return {
        "evidence_disclaimer": (
            "Raw model responses (fields named 'raw') are model-generated text "
            "preserved as evidence. They are NOT validated security facts. "
            "Validated facts are in 'advisory_metadata' (sourced from GitHub "
            "Security Advisories), deterministic check fields, and the JUnit "
            "structured results. The Engineer prompt's test_output excerpt is "
            "truncated to 500 characters; full output and structured results are "
            "in the verification object. Jinja2 3.1.4 is a historical target for "
            "the named advisories (CVE-2024-22195, CVE-2024-34064); it is not "
            "asserted to be a currently secure release. All incident inputs are "
            "synthetic; exploitability is unverified."
        ),
        "incident": incident,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Night Shift</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.6rem; }
  h2 { font-size: 1.2rem; margin-top: 24px; }
  pre { background: #f4f4f4; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 0.82rem; white-space: pre-wrap; }
  button { padding: 8px 16px; font-size: 1rem; cursor: pointer; margin-right: 8px; margin-bottom: 8px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .error { color: #c00; }
  .ok { color: #080; }
  .section { margin-top: 24px; padding: 16px; border: 1px solid #ddd; border-radius: 8px; }
  .timeline { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  .step { padding: 8px 12px; border-radius: 6px; background: #eee; }
  .step.active { background: #d4edda; }
  .step.blocked { background: #f8d7da; }
  .step.pending { background: #fff3cd; }
  details { margin: 8px 0; }
  summary { cursor: pointer; font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8rem; background: #ddd; }
</style>
</head>
<body>
<h1>Night Shift</h1>
<p>Synthetic incident-response workflow: Sentry → Engineer → QA → Human → Simulated deployment.</p>
<p><small>All inputs are synthetic. Exploitability is unverified. Jinja2 3.1.4 is a historical remediation target for CVE-2024-22195 / CVE-2024-34064, not a claim of current security. The Sentry agent is not related to the Sentry (sentry.io) product.</small></p>

<div class="section">
  <h2>Controls</h2>
  <button id="runWorkflow">Run full recovery workflow</button>
  <button id="triageActionable">Triage actionable</button>
  <button id="triageBenign">Triage benign</button>
  <button id="triageAmbiguous">Triage ambiguous</button>
  <button id="approveBtn" disabled>Approve verified patch</button>
  <button id="deployBtn" disabled>Simulate deployment</button>
  <button id="downloadBtn" disabled>Download evidence JSON</button>
</div>

<div class="section" id="progressBox" style="display:none;">
  <h2>Progress</h2>
  <div id="progressText">Idle</div>
</div>

<div class="section" id="timelineBox" style="display:none;">
  <h2>Timeline</h2>
  <div class="timeline" id="timeline"></div>
  <div id="metrics"></div>
</div>

<div class="section" id="detailBox" style="display:none;">
  <h2>Detailed evidence</h2>
  <div id="detail"></div>
</div>

<div id="out"></div>

<script>
let currentIncidentId = null;
let running = false;

const out = document.getElementById('out');
const progressBox = document.getElementById('progressBox');
const progressText = document.getElementById('progressText');
const timelineBox = document.getElementById('timelineBox');
const timeline = document.getElementById('timeline');
const metrics = document.getElementById('metrics');
const detailBox = document.getElementById('detailBox');
const detail = document.getElementById('detail');
const approveBtn = document.getElementById('approveBtn');
const deployBtn = document.getElementById('deployBtn');
const downloadBtn = document.getElementById('downloadBtn');
const runWorkflowBtn = document.getElementById('runWorkflow');

function setRunning(v) {
  running = v;
  runWorkflowBtn.disabled = v;
}

async function fetchJson(url, method) {
  const r = await fetch(url, { method: method || 'GET' });
  return await r.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function collapsible(title, content) {
  return `<details><summary>${esc(title)}</summary><pre>${esc(typeof content === 'string' ? content : JSON.stringify(content, null, 2))}</pre></details>`;
}

function statusLabel(status) {
  const labels = {
    'tested_patch_awaiting_human_approval': 'Awaiting human approval',
    'human_approved': 'Human approved — ready for simulated deployment',
    'simulated_deployed': 'Complete: simulated deployment recorded (simulation only)',
    'blocked': 'Blocked: incident did not qualify for auto-remediation',
    'remediation_failed': 'Remediation failed: all attempts rejected by QA'
  };
  return labels[status] || status;
}

function formatMs(ms) {
  if (ms === null || ms === undefined) return 'N/A (no verified patch)';
  return (ms / 1000).toFixed(1) + ' seconds';
}

function renderTimeline(d) {
  const steps = [
    { key: 'sentry', label: 'Sentry' },
    { key: 'engineer', label: 'Engineer' },
    { key: 'qa', label: 'QA' },
    { key: 'human', label: 'Human' },
    { key: 'deployment_simulator', label: 'Simulated deployment' }
  ];
  const events = d.events || [];
  const hasHuman = events.some(e => e.agent === 'human' && e.action === 'approval');
  const hasSim = events.some(e => e.agent === 'deployment_simulator');
  const qaApproved = (d.attempts || []).some(a => a.qa && a.qa.approval);
  const firstEng = events.find(e => e.agent === 'engineer');
  const blocked = d.status === 'blocked';
  const failed = d.status === 'remediation_failed';

  timeline.innerHTML = steps.map(s => {
    let cls = 'pending';
    let state = 'Pending';
    if (s.key === 'sentry') {
      cls = d.log_key ? 'active' : 'pending';
      state = cls === 'active' ? (blocked ? 'Triage: not actionable' : 'Triage complete') : 'Not run';
    }
    if (s.key === 'engineer') {
      if (!firstEng) { cls = 'pending'; state = 'Not run'; }
      else if (failed) { cls = 'blocked'; state = 'All attempts rejected'; }
      else if (firstEng.fault_injection) { cls = 'blocked'; state = 'Recovered after rejected attempt'; }
      else { cls = 'active'; state = 'Proposal submitted'; }
    }
    if (s.key === 'qa') {
      if (!firstEng) { cls = 'pending'; state = 'Not run'; }
      else if (qaApproved) { cls = 'active'; state = 'Verified: version + tests passed'; }
      else { cls = 'blocked'; state = 'Rejected patch'; }
    }
    if (s.key === 'human') {
      if (hasHuman) { cls = 'active'; state = 'Approved'; }
      else if (qaApproved) { cls = 'pending'; state = 'Awaiting approval'; }
      else { cls = 'blocked'; state = 'Not eligible'; }
    }
    if (s.key === 'deployment_simulator') {
      if (hasSim) { cls = 'active'; state = 'Simulated (no real deployment)'; }
      else if (hasHuman) { cls = 'pending'; state = 'Ready to simulate'; }
      else { cls = 'pending'; state = 'Awaiting approval'; }
    }
    return `<div class="step ${cls}"><strong>${esc(s.label)}</strong><br><small>${esc(state)}</small></div>`;
  }).join('');

  const alertToTested = d.elapsed_alert_to_tested_patch_ms;
  const approvalTs = d.human_approval ? d.human_approval.utc_timestamp : null;
  const simTs = d.simulated_deployment ? d.simulated_deployment.utc_timestamp : null;
  let metricsHtml = `<p><strong>Alert-to-tested-patch:</strong> ${esc(formatMs(alertToTested))}</p>`;
  if (d.verified_patch) metricsHtml += `<p><strong>Verified patch:</strong> ${esc(d.verified_patch.package)}==${esc(d.verified_patch.version)} (artifact sha256: ${esc(String(d.verified_patch.artifact_sha256 || '').slice(0, 16))}…)</p>`;
  if (approvalTs) metricsHtml += `<p><strong>Human approval at:</strong> ${esc(new Date(approvalTs).toLocaleTimeString())}</p>`;
  if (simTs) metricsHtml += `<p><strong>Simulated deployment at:</strong> ${esc(new Date(simTs).toLocaleTimeString())} (simulation only)</p>`;
  if (d.status === 'blocked' || d.status === 'remediation_failed') metricsHtml += `<p class="error">${esc(d.reason || '')}</p>`;
  metrics.innerHTML = metricsHtml;

  let detailHtml = '';
  for (const a of d.attempts || []) {
    detailHtml += `<h3>Attempt ${esc(a.attempt)} ${a.engineer.fault_injection ? '<span class="badge">FAULT INJECTION</span>' : ''}</h3>`;
    detailHtml += `<p><strong>Proposal:</strong> ${esc(a.engineer.proposal?.package)}==${esc(a.engineer.proposal?.version)}</p>`;
    detailHtml += `<p><strong>Installed:</strong> ${esc(a.verification?.installed_version)}</p>`;
    detailHtml += `<p><strong>Installer invoked:</strong> ${a.verification?.installer_invoked ? 'yes' : 'no — rejected at boundary'}</p>`;
    detailHtml += `<p><strong>QA approval:</strong> ${a.qa?.approval ? '<span class="ok">APPROVED</span>' : '<span class="error">REJECTED</span>'}</p>`;
    detailHtml += `<p><strong>QA reason:</strong> ${esc(a.qa?.reason)}</p>`;
    detailHtml += collapsible('Deterministic checks', a.qa?.deterministic_checks);
    detailHtml += collapsible('JUnit structured results', a.verification?.junit);
    detailHtml += collapsible('Full test output', a.verification?.test_output || '');
    if (a.engineer.raw) detailHtml += collapsible('Raw Engineer model response', a.engineer.raw);
    if (a.qa && a.qa.raw) detailHtml += collapsible('Raw QA model response', a.qa.raw);
  }
  if (d.human_approval) {
    detailHtml += `<h3>Human approval</h3><pre>${esc(JSON.stringify(d.human_approval, null, 2))}</pre>`;
  }
  if (d.simulated_deployment) {
    detailHtml += `<h3>Simulated deployment</h3><pre>${esc(JSON.stringify(d.simulated_deployment, null, 2))}</pre>`;
  }
  detail.innerHTML = detailHtml;
  detailBox.style.display = 'block';
}

async function runWorkflow() {
  if (running) return;
  setRunning(true);
  out.innerHTML = '';
  progressBox.style.display = 'block';
  progressText.textContent = 'Running Sentry → Engineer → QA workflow (this may take ~20–30 seconds with local Ollama)...';
  timelineBox.style.display = 'none';
  detailBox.style.display = 'none';
  approveBtn.disabled = true;
  deployBtn.disabled = true;
  downloadBtn.disabled = true;

  try {
    const d = await fetchJson('/workflow', 'POST');
    currentIncidentId = d.id;
    timelineBox.style.display = 'block';
    renderTimeline(d);
    progressText.textContent = `Workflow complete: ${esc(statusLabel(d.status))}`;

    if (d.status === 'tested_patch_awaiting_human_approval') {
      approveBtn.disabled = false;
    }
    downloadBtn.disabled = false;
  } catch (e) {
    progressText.textContent = 'Workflow failed: ' + esc(e.message);
  } finally {
    setRunning(false);
  }
}

approveBtn.addEventListener('click', async () => {
  if (!currentIncidentId) return;
  const d = await fetchJson(`/incident/${currentIncidentId}/approve`, 'POST');
  const msg = document.createElement('p');
  msg.className = d.approved ? 'ok' : 'error';
  msg.textContent = d.approved ? 'Approved: ' + d.note : 'Approval rejected: ' + d.error;
  out.appendChild(msg);
  const inc = await fetchJson(`/incident/${currentIncidentId}`);
  renderTimeline(inc);
  progressText.textContent = 'Current status: ' + statusLabel(inc.status);
  if (d.approved) {
    deployBtn.disabled = false;
    approveBtn.disabled = true;
  }
});

deployBtn.addEventListener('click', async () => {
  if (!currentIncidentId) return;
  const d = await fetchJson(`/incident/${currentIncidentId}/simulate-deploy`, 'POST');
  const msg = document.createElement('p');
  msg.className = d.simulated ? 'ok' : 'error';
  msg.textContent = d.simulated ? 'Simulated deployment recorded.' : 'Simulation rejected: ' + d.error;
  out.appendChild(msg);
  const inc = await fetchJson(`/incident/${currentIncidentId}`);
  renderTimeline(inc);
  progressText.textContent = 'Current status: ' + statusLabel(inc.status);
  if (d.simulated) {
    deployBtn.disabled = true;
  }
});

downloadBtn.addEventListener('click', () => {
  if (!currentIncidentId) return;
  window.open(`/export/${currentIncidentId}`, '_blank');
});

async function runTriage(key) {
  progressBox.style.display = 'block';
  progressText.textContent = 'Triaging ' + esc(key) + '...';
  const d = await fetchJson('/triage/' + encodeURIComponent(key));
  out.innerHTML = '<pre>' + esc(JSON.stringify(d, null, 2)) + '</pre>';
  progressText.textContent = 'Triage complete: ' + esc(d.disposition);
}

document.getElementById('runWorkflow').addEventListener('click', runWorkflow);
document.getElementById('triageActionable').addEventListener('click', () => runTriage('actionable'));
document.getElementById('triageBenign').addEventListener('click', () => runTriage('benign'));
document.getElementById('triageAmbiguous').addEventListener('click', () => runTriage('ambiguous'));
</script>
</body>
</html>
"""
