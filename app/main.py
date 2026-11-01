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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app.workflow import run_incident
from app.sentry_agent import triage, SYNTHETIC_FIXTURES
from app.approval import approve, simulate_deploy
from app import staging

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
ALLOWED = {"CRITICAL", "WARNING", "INFO", "FALSE_POSITIVE"}

app = FastAPI(title="Night Shift")

# CORS for public deployment: the frontend may be served from a different
# origin (Vercel) than the execution backend. NS_CORS_ORIGIN, if set,
# restricts the allowed origin; unset means same-origin (local dev).
_cors_origin = os.environ.get("NS_CORS_ORIGIN")
if _cors_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_cors_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

# Server-enforced authorization for public deployment.
# NS_DEMO_TOKEN gates all state-mutating endpoints. If set, callers must
# include Authorization: Bearer <token>. Read-only endpoints (GET) are
# allowed without a token so the console can display status.
_DEMO_TOKEN = os.environ.get("NS_DEMO_TOKEN")
_MUTATING_PREFIXES = (
    "/workflow",
    "/inbox",
    "/incident/",
    "/sentry-classify",
)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Bearer-token gate on mutating endpoints for public deployment."""
    if not _DEMO_TOKEN:
        return await call_next(request)
    # Read-only: allow without token
    if request.method == "GET":
        return await call_next(request)
    # Mutating: require matching bearer token
    for prefix in _MUTATING_PREFIXES:
        if request.url.path.rstrip("/").startswith(prefix):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != _DEMO_TOKEN:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized"},
                )
            break
    return await call_next(request)

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

_templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")

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

    The recovery incident (if any) is registered in the incident store
    so the existing /incident/{id}, /approve, /staging-release,
    /staging-stop, and /export endpoints work for that exact incident.
    """
    from app.inbox import process_inbox
    result = process_inbox()
    recovery = result.get("recovery_incident")
    if recovery:
        with _store_lock:
            _incidents[recovery["id"]] = recovery
        result["recovery_incident_id"] = recovery["id"]
    return result


@app.get("/incidents")
def list_incidents():
    """List all stored incidents with id and status, for the evidence selector."""
    with _store_lock:
        return [{"id": iid, "status": inc.get("status", "unknown")}
                for iid, inc in _incidents.items()]


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
    payload = {
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
    return JSONResponse(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="night-shift-evidence-{incident_id}.json"'
            )
        },
    )


@app.get("/app", response_class=HTMLResponse)
def app_console(request: Request):
    with open(os.path.join(_templates_dir, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    with open(os.path.join(_templates_dir, "landing.html"), "r", encoding="utf-8") as f:
        return f.read()

