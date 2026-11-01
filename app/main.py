"""Night Shift — backend with approval gate and simulated deployment.

State-changing endpoints are POST. Incidents are keyed by UUID with per-incident
locks protecting approval/state transitions against concurrent requests.
Note: these changes do NOT make the application ready for public hosting.
"""
import json
import os
import threading
import time
import uuid
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

# Live-progress tracking for background workflows.
_running_workflows: dict = {}  # incident_id -> True while running


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
    """Start the full incident workflow in the background.

    Returns immediately with the incident id and a 'running' status.
    Poll /workflow/status/{id} for live progress.
    Duplicate-submission guard: if a workflow is already running, returns
    the existing incident id instead of starting a new one.

    The duplicate check, incident pre-registration, and workflow
    reservation happen inside a single lock acquisition, so concurrent
    submissions can never start two jobs.
    """
    with _store_lock:
        # Duplicate guard: if any workflow is already running, return it.
        for inc_id, running in _running_workflows.items():
            if running:
                return {"id": inc_id, "status": "running",
                        "message": "A workflow is already running. Poll /workflow/status/"
                                   + inc_id + " for progress."}

        incident_id = str(uuid.uuid4())
        # Pre-register the incident so status polling works immediately.
        _incidents[incident_id] = {
            "id": incident_id,
            "status": "running",
            "events": [],
            "attempts": [],
            "log_key": "actionable",
        }
        _running_workflows[incident_id] = True

    def _progress_cb(events, attempts, status):
        """Publish real progress into the stored incident as it happens."""
        with _store_lock:
            current = _incidents.get(incident_id)
            if current is None:
                return
            current["events"] = list(events)
            current["attempts"] = list(attempts)
            if status:
                current["status"] = status

    def _run():
        try:
            incident = run_incident(log_key="actionable", fault_inject_first=True,
                                    incident_id=incident_id,
                                    progress_cb=_progress_cb)
            incident["id"] = incident_id  # keep the pre-registered id
            with _store_lock:
                _incidents[incident_id] = incident
        except Exception as exc:
            with _store_lock:
                _incidents[incident_id] = {
                    "id": incident_id, "status": "error",
                    "error": str(exc), "events": [], "attempts": [],
                }
        finally:
            with _store_lock:
                _running_workflows.pop(incident_id, None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"id": incident_id, "status": "running",
            "message": "Workflow started. Poll /workflow/status/" + incident_id}


@app.get("/workflow/status/{incident_id}")
def workflow_status(incident_id: str):
    """Poll the live progress of a running or completed workflow."""
    with _store_lock:
        running = _running_workflows.get(incident_id, False)
        incident = _incidents.get(incident_id)
    if not incident:
        return {"error": "Incident not found", "incident_id": incident_id}
    # Summarize current progress from events.
    events = incident.get("events", [])
    agents_seen = []
    for e in events:
        a = e.get("agent")
        if a and a not in agents_seen:
            agents_seen.append(a)
    return {
        "incident_id": incident_id,
        "status": incident.get("status", "unknown"),
        "running": running,
        "agents_completed": agents_seen,
        "attempt_count": len(incident.get("attempts", [])),
        "event_count": len(events),
    }


@app.post("/workflow-sync")
def workflow_sync():
    """Run the full workflow synchronously (for tests and live checks)."""
    incident = run_incident(log_key="actionable", fault_inject_first=True)
    with _store_lock:
        _incidents[incident["id"]] = incident
    return incident


@app.get("/triage/{log_key}")
def triage_endpoint(log_key: str):
    if log_key not in SYNTHETIC_FIXTURES:
        return {"error": f"Unknown log_key: {log_key}. Allowed: {list(SYNTHETIC_FIXTURES)}"}
    return triage(log_key)


@app.post("/inbox")
def inbox_endpoint():
    """Process the mixed synthetic incident inbox.

    Triages a fixed batch of signals and, if one is actionable with
    sufficient evidence, starts the existing recovery workflow.
    Benign and uncertain signals are recorded as separate handoffs.
    """
    from app.inbox import process_inbox
    return process_inbox()


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Night Shift · Local recovery console</title>
<style>
:root { color-scheme: dark; --bg:#061225; --panel:#0b1b33; --panel2:#0e2542; --line:#1e3a5f; --text:#e7f1ff; --muted:#91a7c5; --teal:#29d3c3; --teal2:#0e9f9a; --warn:#f3bd55; --bad:#fa7185; --good:#46dfb1; --ink:#061225; }
* { box-sizing:border-box; }
body { margin:0; background:linear-gradient(180deg,#061225 0,#07182d 60%,#061225 100%); color:var(--text); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { width:min(1240px,calc(100% - 32px)); margin:0 auto 56px; }
header { padding:36px 0 22px; display:flex; justify-content:space-between; gap:24px; align-items:start; border-bottom:1px solid var(--line); }
h1 { font-size:clamp(1.7rem,4vw,2.7rem); letter-spacing:-.04em; margin:5px 0; } h2 { font-size:1.02rem; margin:0 0 14px; } h3 { font-size:.92rem; margin:20px 0 8px; color:#cce3ff; }
p { margin:7px 0; }.eyebrow { color:var(--teal); font-size:.75rem; letter-spacing:.13em; text-transform:uppercase; font-weight:750; }.lede { color:var(--muted); max-width:720px; }
.grid { display:grid; gap:14px; }.grid.two { grid-template-columns:repeat(2,minmax(0,1fr)); }.grid.three { grid-template-columns:repeat(3,minmax(0,1fr)); }
.panel { background:rgba(11,27,51,.94); border:1px solid var(--line); border-radius:12px; padding:18px; box-shadow:0 12px 30px rgba(0,0,0,.16); }.panel + .panel,.panel { margin-top:14px; }
.controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; } button { border:1px solid #2d5f78; border-radius:8px; background:#123451; color:var(--text); padding:9px 13px; font:inherit; font-weight:650; cursor:pointer; } button:hover:not(:disabled){ border-color:var(--teal); background:#174564; } button.primary { background:var(--teal); color:var(--ink); border-color:var(--teal); } button.primary:hover:not(:disabled){ background:#57e5d8; } button.secondary { background:transparent; color:#c6d8ee; } button.warn { border-color:#89662e; color:#ffe1a0; background:#3b2e15; } button:disabled { opacity:.38; cursor:not-allowed; }
.status { display:flex; gap:9px; align-items:center; color:var(--muted); }.dot { width:9px; height:9px; border-radius:50%; background:var(--muted); box-shadow:0 0 0 4px rgba(145,167,197,.1); }.dot.live { background:var(--teal); box-shadow:0 0 0 4px rgba(41,211,195,.12); animation:pulse 1.6s infinite; } @keyframes pulse { 50% { opacity:.45; } }
.kicker { color:var(--muted); font-size:.82rem; }.metric { background:#08182d; border:1px solid #183654; border-radius:9px; padding:12px; }.metric strong { display:block; font-size:1.06rem; margin-top:3px; }.metric.good strong{color:var(--good);}.metric.warn strong{color:var(--warn);}
.timeline { display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:8px; overflow-x:auto; }.step { min-height:78px; padding:11px; border:1px solid #28445f; border-radius:9px; background:#09192c; color:var(--muted); }.step strong { display:block; color:#dcecff; font-size:.82rem; }.step small { display:block; margin-top:5px; line-height:1.28; }.step.active { border-color:#218f87; background:#0a292f; }.step.active small{color:var(--good)}.step.blocked { border-color:#8c4051; background:#2b1721; }.step.blocked small{color:#ff9bad}.step.pending { border-color:#69552d; background:#211c12; }.step.pending small{color:#f4cb72}
.badge { display:inline-flex; align-items:center; gap:5px; border:1px solid #38536f; background:#102743; padding:2px 7px; border-radius:99px; font-size:.72rem; font-weight:700; }.badge.good{border-color:#2a8e73;color:var(--good)}.badge.warn{border-color:#86642c;color:#f8cd70}.badge.bad{border-color:#974354;color:#ff9cab}
.notice { border-left:3px solid var(--teal); background:#082330; padding:11px 13px; color:#bcd4e8; border-radius:0 8px 8px 0; font-size:.88rem; }.notice.warn { border-color:var(--warn); }.notice small { color:var(--muted); }
.patch { border:1px solid #235069; border-radius:10px; background:#07172a; overflow:hidden; }.patch-head { padding:11px 13px; border-bottom:1px solid #1d3c56; display:flex; justify-content:space-between; gap:8px; }.diff { margin:0; border:0; border-radius:0; max-height:260px; }.diff .minus{color:#ff9bad}.diff .plus{color:#67e7bd}
pre { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; overflow:auto; max-height:360px; padding:12px; background:#061426; border:1px solid #17344e; border-radius:8px; color:#cfe1f6; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; } details { border:1px solid #1b3a56; border-radius:8px; padding:0 11px; margin:9px 0; background:#07182c; } summary { cursor:pointer; padding:10px 0; font-weight:650; color:#cfe3f7; } details pre { margin:0 0 11px; }
.tablewrap { overflow:auto; border:1px solid #1b3a56; border-radius:8px; } table { border-collapse:collapse; width:100%; min-width:660px; font-size:.86rem; } th,td { text-align:left; padding:9px 10px; border-bottom:1px solid #193551; vertical-align:top; } th { background:#0b213a; color:#9db9d6; font-weight:650; } tr:last-child td{border-bottom:0}.ok{color:var(--good)}.error{color:#ff9bad}.hidden{display:none!important}.out { margin-top:12px; }
.disclosures { color:var(--muted); font-size:.82rem; }.disclosures li { margin:5px 0; }.break { word-break:break-all; }
@media(max-width:800px){ header{display:block}.grid.two,.grid.three{grid-template-columns:1fr}.timeline{grid-template-columns:repeat(6,150px)} main{width:min(100% - 20px,1240px)} }
</style>
</head>
<body>
<main>
<header>
  <div><div class="eyebrow">Local-only operations console</div><h1>Night Shift recovery</h1><p class="lede">A constrained, synthetic recovery demonstration: detect, ground evidence, verify a patch, require human approval, then release only to real local staging.</p></div>
  <div class="status"><span class="dot" id="liveDot"></span><span id="consoleStatus">Ready for a synthetic recovery</span></div>
</header>

<section class="panel">
  <div class="grid two"><div><h2>Recovery action</h2><p class="kicker">Starts the complete synthetic workflow. Fault injection intentionally verifies that the affected dependency is rejected before the corrected artifact is considered.</p><div class="controls"><button id="runWorkflow" class="primary">Run local recovery</button><button id="downloadBtn" class="secondary" disabled>Download evidence JSON</button></div></div><div class="notice"><strong>Demonstration constraints</strong><br><small>Synthetic inputs · fault injection is intentional · Jinja2 3.1.4 is a historical remediation target, not a current-security claim · all services bind to localhost · human approval is intentionally unauthenticated for this local demo.</small></div></div>
</section>

<section class="panel" id="progressBox">
  <h2>Live progress</h2><div class="status"><span class="dot" id="progressDot"></span><span id="progressText">Idle — no incident selected.</span></div><div id="liveMeta" class="kicker" style="margin-top:8px"></div>
</section>

<section class="panel hidden" id="timelineBox"><h2>Recovery path</h2><div class="timeline" id="timeline"></div><div class="grid three" id="metrics" style="margin-top:14px"></div></section>

<section class="panel hidden" id="reviewBox"><h2>Patch review</h2><div id="review"></div><div class="controls" style="margin-top:14px"><button id="approveBtn" class="primary" disabled>Record human approval</button><button id="stagingReleaseBtn" class="primary" disabled>Release to local staging</button><button id="stagingStopBtn" class="warn" disabled>Stop local staging</button><button id="deployBtn" class="secondary" disabled>Record simulation instead</button></div><p class="kicker" style="margin-top:8px">Local staging is the primary post-approval action. Simulation is secondary and records no real deployment.</p></section>

<section class="panel hidden" id="detailBox"><h2>Evidence and logs</h2><p class="kicker">All model output and logs below are rendered as text. Expand details to review the actual recorded evidence; download the export for the complete incident record.</p><div id="detail"></div></section>

<section class="panel"><h2>Quick triage examples</h2><div class="controls"><button id="triageActionable" class="secondary">Actionable synthetic log</button><button id="triageBenign" class="secondary">Benign synthetic log</button><button id="triageAmbiguous" class="secondary">Ambiguous synthetic log</button></div><div class="out" id="out"></div></section>

<section class="panel disclosures"><h2>Disclosures</h2><ul><li>Inputs are synthetic; exploitability is unverified.</li><li>The first remediation attempt is fault-injected to demonstrate deterministic rejection.</li><li>Jinja2 3.1.4 is a historical remediation target for the named advisories, not a claim of current security.</li><li>Local staging is a real process bound to <code>127.0.0.1</code>; it is not public hosting or external deployment.</li><li>Approval is an intentionally unauthenticated human-action control for this local demonstration.</li><li>Evidence reads are orchestrator-selected; Engineer interprets the actual numbered contents and cannot claim tool execution as evidence.</li></ul></section>
</main>
<script>
let currentIncidentId = null, running = false;
const $ = id => document.getElementById(id);
const out=$('out'), progressBox=$('progressBox'), progressText=$('progressText'), liveMeta=$('liveMeta'), timelineBox=$('timelineBox'), timeline=$('timeline'), metrics=$('metrics'), reviewBox=$('reviewBox'), review=$('review'), detailBox=$('detailBox'), detail=$('detail'), approveBtn=$('approveBtn'), deployBtn=$('deployBtn'), downloadBtn=$('downloadBtn'), stagingReleaseBtn=$('stagingReleaseBtn'), stagingStopBtn=$('stagingStopBtn'), runWorkflowBtn=$('runWorkflow');
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function textPre(v){return `<pre>${esc(typeof v==='string'?v:JSON.stringify(v,null,2))}</pre>`;}
function collapsible(title,content,open=false){return `<details ${open?'open':''}><summary>${esc(title)}</summary>${textPre(content)}</details>`;}
async function fetchJson(url,method){const r=await fetch(url,{method:method||'GET'});return r.json();}
function setRunning(v){running=v;runWorkflowBtn.disabled=v;$('liveDot').classList.toggle('live',v);$('progressDot').classList.toggle('live',v);}
function show(id){$(id).classList.remove('hidden');}
function formatMs(ms){return ms==null?'No verified patch':(Number(ms)/1000).toFixed(1)+' seconds';}
function statusLabel(s){return {'tested_patch_awaiting_human_approval':'Verified patch awaiting human approval','human_approved':'Human approved — local staging available','simulated_deployed':'Simulation recorded (no real deployment)','staging_released':'Released to real local staging','staging_stopped':'Local staging stopped','blocked':'Blocked: triage did not qualify','remediation_failed':'Remediation failed: QA rejected all attempts','needs_human_review':'Needs human review: grounding failed','running':'Recovery is running','error':'Recovery error'}[s]||s||'Unknown';}
function badge(label,kind=''){return `<span class="badge ${kind}">${esc(label)}</span>`;}
function metric(label,value,kind=''){return `<div class="metric ${kind}"><span class="kicker">${esc(label)}</span><strong>${esc(value)}</strong></div>`;}
function renderTimeline(d){const events=d.events||[], attempts=d.attempts||[];const qa=attempts.some(a=>a.qa?.approval);const human=!!d.human_approval;const staged=!!d.staging_release?.released;const sim=!!d.simulated_deployment;const first=events.find(e=>e.agent==='engineer');const steps=[['Sentry',d.log_key?'Triage complete':'Not run',d.log_key?'active':'pending'],['Engineer',d.status==='needs_human_review'?'Grounding handoff':first?.fault_injection?'Recovered after rejected fault':'Proposal interpreted',d.status==='needs_human_review'?'blocked':first?'active':'pending'],['QA',qa?'Verified version + tests':first?'Rejected patch':'Not run',qa?'active':first?'blocked':'pending'],['Human',human?'Approval recorded':qa?'Awaiting explicit approval':'Not eligible',human?'active':qa?'pending':'blocked'],['Local staging',staged?'Released on 127.0.0.1:'+d.staging_release.port:human?'Ready for release':'Awaiting approval',staged?'active':'pending'],['Simulation',sim?'Recorded — no deployment':human?'Optional secondary action':'Awaiting approval',sim?'active':'pending']];timeline.innerHTML=steps.map(x=>`<div class="step ${x[2]}"><strong>${esc(x[0])}</strong><small>${esc(x[1])}</small></div>`).join('');metrics.innerHTML=metric('Alert → tested patch',formatMs(d.elapsed_alert_to_tested_patch_ms),'good')+metric('QA decision',qa?'Approved':'Not approved',qa?'good':'warn')+metric('Current state',statusLabel(d.status),staged?'good':'');}
function verifiedAttempt(d){return (d.attempts||[]).find(a=>a.qa?.approval&&a.verification)||null;}
function renderReview(d){const a=verifiedAttempt(d), patch=d.verified_patch;if(!a||!patch){review.innerHTML=`<div class="notice warn">No QA-verified patch is available. Approval and local staging remain disabled.</div>`;return;}const inv=a.engineer?.investigation||{};const diff=patch.requirements_diff||a.verification?.requirements_diff||'';const junit=a.verification?.junit||{};const tests=Object.entries(junit.tests||{});review.innerHTML=`<div class="grid three">${metric('Proposed / verified patch',`${patch.package}==${patch.version}`,'good')}${metric('Installed version',a.verification?.installed_version||'Not recorded','good')}${metric('Artifact identifier',String(patch.artifact_sha256||'N/A').slice(0,16)+'…')}</div><h3>Requirements diff</h3><div class="patch"><div class="patch-head"><strong>Verified artifact requirements</strong>${badge('SHA-256 bound','good')}</div><pre class="diff">${esc(diff).replace(/^(-.*)$/gm,'<span class="minus">$1</span>').replace(/^(\+.*)$/gm,'<span class="plus">$1</span>')}</pre></div><h3>Verified citations</h3><div class="tablewrap"><table><thead><tr><th>Source</th><th>Line</th><th>Recorded finding</th></tr></thead><tbody>${(inv.evidence||[]).map(e=>`<tr><td>${esc(e.file)}</td><td>${esc(e.line)}</td><td>${esc(e.finding)}</td></tr>`).join('')||'<tr><td colspan="3">No citations recorded.</td></tr>'}</tbody></table></div><h3>Test results</h3><div class="tablewrap"><table><thead><tr><th>Test</th><th>Result</th></tr></thead><tbody>${tests.map(([n,v])=>`<tr><td>${esc(n)}</td><td class="${v==='passed'?'ok':'error'}">${esc(v)}</td></tr>`).join('')||'<tr><td colspan="2">No structured results recorded.</td></tr>'}</tbody></table></div>`;}
function renderDetail(d){let html='';for(const a of d.attempts||[]){const eng=a.engineer||{},inv=eng.investigation||{};html+=`<h3>Attempt ${esc(a.attempt)} ${eng.fault_injection?badge('Fault injection','bad'):''}</h3>`;if(inv.evidence_collection){html+=`<div class="notice"><strong>Evidence collection: orchestrator-selected</strong><br><small>${esc(inv.evidence_collection.selected_by||'Recorded reads')}</small></div>`;}html+=`<p>${badge('Declared: '+(inv.declared_version||'N/A'))} ${badge('Fixture pin: '+(inv.fixture_declared_version||'N/A'))} ${a.verification?badge('Installed: '+(a.verification.installed_version||'N/A'),'good'):''}</p>`;if(inv.applicability)html+=`<p><strong>Applicability:</strong> ${esc(inv.applicability)}</p>`;if(inv.uncertainty)html+=`<p><strong>Uncertainty:</strong> ${esc(inv.uncertainty)}</p>`;if(inv.tool_calls?.length)html+=collapsible('Executed orchestrator-selected evidence reads',inv.tool_calls);if(inv.evidence?.length)html+=collapsible('Verified citation record',inv.evidence);if(inv.citation_problems?.length)html+=`<p class="error">Citation problems: ${esc(inv.citation_problems.join('; '))}</p>`;if(inv.grounding_problems?.length)html+=`<p class="error">Grounding problems: ${esc(inv.grounding_problems.join('; '))}</p>`;if(a.verification){html+=collapsible('QA deterministic checks',a.qa?.deterministic_checks);html+=collapsible('JUnit structured results',a.verification.junit);html+=collapsible('Full test output',a.verification.test_output||'');}if(eng.raw)html+=collapsible('Raw Engineer model output (text only)',eng.raw);if(a.qa?.raw)html+=collapsible('Raw QA model output (text only)',a.qa.raw);}if(d.human_approval)html+=collapsible('Human approval record',d.human_approval);if(d.staging_release)html+=collapsible('Local staging record',d.staging_release);if(d.simulated_deployment)html+=collapsible('Simulation record (secondary)',d.simulated_deployment);detail.innerHTML=html||'<p class="kicker">No incident evidence yet.</p>';}
function renderIncident(d){show('timelineBox');show('reviewBox');show('detailBox');renderTimeline(d);renderReview(d);renderDetail(d);const ready=d.status==='tested_patch_awaiting_human_approval';approveBtn.disabled=!ready;const approved=d.status==='human_approved'||!!d.human_approval;stagingReleaseBtn.disabled=!approved||!!d.staging_release?.running;stagingStopBtn.disabled=!d.staging_release?.running;deployBtn.disabled=!approved||!!d.simulated_deployment;downloadBtn.disabled=false;}
async function refreshIncident(){if(!currentIncidentId)return null;const d=await fetchJson('/incident/'+currentIncidentId);renderIncident(d);return d;}
async function runWorkflow(){if(running)return;setRunning(true);progressText.textContent='Starting synthetic recovery…';liveMeta.textContent='Sentry → Engineer → QA are running against the local model and disposable verifier.';currentIncidentId=null;timelineBox.classList.add('hidden');reviewBox.classList.add('hidden');detailBox.classList.add('hidden');approveBtn.disabled=deployBtn.disabled=downloadBtn.disabled=stagingReleaseBtn.disabled=stagingStopBtn.disabled=true;try{const start=await fetchJson('/workflow','POST');currentIncidentId=start.id;const poll=async()=>{const st=await fetchJson('/workflow/status/'+currentIncidentId);progressText.textContent=st.running?'Recovery running — '+(st.agents_completed||[]).join(' → ')+' · '+st.event_count+' recorded events':statusLabel(st.status);liveMeta.textContent='Incident '+currentIncidentId+' · '+st.attempt_count+' remediation attempt(s)';if(st.running){setTimeout(poll,1500);return;}setRunning(false);const d=await refreshIncident();progressText.textContent=statusLabel(d.status);};setTimeout(poll,750);}catch(e){setRunning(false);progressText.textContent='Recovery request failed: '+e.message;}}
function message(ok,txt){out.innerHTML=`<p class="${ok?'ok':'error'}">${esc(txt)}</p>`;}
approveBtn.onclick=async()=>{const r=await fetchJson(`/incident/${currentIncidentId}/approve`,'POST');message(r.approved,r.approved?'Human approval recorded. Local staging is now available.':'Approval rejected: '+r.error);const d=await refreshIncident();progressText.textContent=statusLabel(d.status);};
stagingReleaseBtn.onclick=async()=>{const r=await fetchJson(`/incident/${currentIncidentId}/staging-release`,'POST');message(r.released,r.released?'Released to local staging on 127.0.0.1:'+r.port:'Local staging rejected: '+r.error);const d=await refreshIncident();progressText.textContent=statusLabel(d.status);};
stagingStopBtn.onclick=async()=>{const r=await fetchJson(`/incident/${currentIncidentId}/staging-stop`,'POST');message(r.stopped,r.stopped?'Local staging stopped.':'Stop failed: '+r.error);await refreshIncident();};
deployBtn.onclick=async()=>{const r=await fetchJson(`/incident/${currentIncidentId}/simulate-deploy`,'POST');message(r.simulated,r.simulated?'Simulation recorded; no real deployment occurred.':'Simulation rejected: '+r.error);await refreshIncident();};
downloadBtn.onclick=()=>{if(currentIncidentId)window.open('/export/'+currentIncidentId,'_blank');};
async function runTriage(k){progressText.textContent='Triaging '+k+' synthetic input…';const d=await fetchJson('/triage/'+encodeURIComponent(k));out.innerHTML=textPre(d);progressText.textContent=d.error?'Triage error':'Triage complete';}
runWorkflowBtn.onclick=runWorkflow;$('triageActionable').onclick=()=>runTriage('actionable');$('triageBenign').onclick=()=>runTriage('benign');$('triageAmbiguous').onclick=()=>runTriage('ambiguous');
</script>
</body>
</html>
"""
