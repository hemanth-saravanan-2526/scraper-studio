"""
Scraper Studio - Inspection API
================================
Thin HTTP layer in front of inspection_agent_service.py. This is what the
Builder page's "Generate Scraper" button actually calls.

Endpoints:
  POST /jobs                  - submit a new inspection job, returns job_id
  GET  /jobs/{id}              - job status + result once complete
  GET  /jobs/{id}/stream       - Server-Sent Events stream of the live action log
                                  (this is what powers the "live agent log" panel
                                  on the Detail page - each browser action the
                                  agent takes streams to the UI as it happens)
  GET  /jobs                   - list all jobs (backs the Dashboard table)
  POST /jobs/{id}/test-run     - actually run the generated script against a small
                                  sample input, in an isolated per-job venv
  GET  /jobs/{id}/test-run     - poll the test-run's status
  GET  /jobs/{id}/sample-output - download the test-run's output.xlsx

Not wired to a real database/queue - job_store is an in-memory dict for
clarity. Swap JobStore for your actual persistence + your existing DAG
orchestration (Temporal, per the platform's Orchestration tier) in production;
the request/response contracts below are what stay stable either way.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from inspection_agent_service import InspectionJob, InspectionResult, run_inspection_job
from auth_config import require_credentials_at_startup, CredentialError
from cost_metering import JSONLCostSink

app = FastAPI(title="Scraper Studio Inspection API")
cost_sink = JSONLCostSink(path="cost_ledger.jsonl")

# CORS: the Builder/Detail/Dashboard UI (scraper_studio.html) is a static
# file opened directly in a browser or served from a different origin than
# this API, so the browser needs explicit permission to call it.
# allow_origins=["*"] is fine for local dev / a demo; lock this down to your
# actual frontend origin(s) before this is customer-facing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _validate_credentials_on_startup():
    """
    Fail fast: if ANTHROPIC_API_KEY (or Bedrock/Vertex equivalent) isn't
    configured, refuse to come up rather than accept jobs that will fail
    partway through. See auth_config.py for the resolution order.
    """
    try:
        require_credentials_at_startup()
    except CredentialError as exc:
        # Re-raised so uvicorn exits non-zero and the deploy fails loudly,
        # not silently, in your CI/CD or orchestration health checks.
        raise RuntimeError(f"Inspection service cannot start: {exc}") from exc

    # cost_ledger.jsonl is a relative path, so it's easy to look in the wrong
    # folder for it - print exactly where this process will write it.
    ledger_path = Path(cost_sink.path).resolve()
    print(f"[inspection-service] Cost ledger will be written to: {ledger_path}")
    print(f"[inspection-service] (nothing appears here until at least one job finishes)")

    # Restore job history (including the most recent job) from disk, so a
    # restart doesn't wipe the Dashboard - see JOBS_STORE_PATH.
    store.load_from_disk()


@app.get("/health")
async def health():
    """Confirms credentials are resolvable without ever exposing the secret."""
    try:
        creds = require_credentials_at_startup()
        return {"status": "ok", "auth_backend": creds.backend.value}
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/cost-ledger-location")
async def cost_ledger_location():
    """
    Returns the absolute path cost_ledger.jsonl is actually being written to
    (or would be, once a job finishes). cost_ledger.jsonl is a relative path
    that resolves against this process's working directory - not necessarily
    the folder this file lives in - so this exists to remove any ambiguity
    about where to look for it.
    """
    path = Path(cost_sink.path).resolve()
    return {
        "path": str(path),
        "exists": path.exists(),
        "note": "Nothing appears here until at least one job reaches a terminal status.",
    }


# --------------------------------------------------------------------------
# Request / response contracts
# --------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    name: str | None = None            # user-facing label, shown on the Dashboard
    target_url: str
    sample_keyword: str | None = None  # optional: one value for the agent to
                                        # actually test with during inspection
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    max_steps: int = 40
    max_seconds: int = 600
    max_budget_usd: float | None = None  # None -> service default (see cost_metering.py)


class JobRecord(BaseModel):
    job_id: str
    name: str
    status: str  # "queued" | "running" | "completed" | "failed" | "step_limit_hit" | "time_limit_hit" | "budget_limit_hit" | "anti_bot_detected"
    created_at: str
    target_url: str
    sample_keyword: str | None = None  # carried from CreateJobRequest - reused to
                                        # build the sample input.xlsx for test runs
    result: dict | None = None
    cost: dict | None = None  # populated once the job's ResultMessage arrives
    saved_to: str | None = None  # absolute folder path where the script was written to disk
    anti_bot_marker: str | None = None  # set if recon's anti-bot early-exit fired
                                         # (e.g. "captcha", "cloudflare") - surfaced as
                                         # its own field so the Dashboard can flag it
                                         # without parsing confidence_notes text.
    test_run: dict | None = None  # {"status": "not_run"|"running"|"passed"|"failed", ...}
                                   # - see /jobs/{id}/test-run below. None until a test
                                   # run has been started at least once.


# --------------------------------------------------------------------------
# Writing the generated script to a real, browsable folder - not just as a
# string inside jobs_store.json. This is what makes a finished scraper
# something you can open in File Explorer/Finder, not just download once
# from the browser or read out of JSON.
# --------------------------------------------------------------------------

GENERATED_SCRIPTS_DIR = Path("generated_scripts")


def _safe_folder_name(name: str, job_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    short_id = job_id.split("-")[0]  # first segment of the UUID is enough to disambiguate
    return f"{slug}_{short_id}" if slug else short_id


def write_generated_files_to_disk(job_id: str, name: str, generated_code: str, requirements_txt: str) -> str:
    folder = GENERATED_SCRIPTS_DIR / _safe_folder_name(name, job_id)
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "generated_scraper.py").write_text(generated_code, encoding="utf-8")
    (folder / "requirements.txt").write_text(
        requirements_txt or "# agent did not return a requirements.txt for this job - check the script's imports manually\n",
        encoding="utf-8",
    )
    resolved = str(folder.resolve())
    print(f"[inspection-service] Wrote generated script to: {resolved}")
    return resolved


# --------------------------------------------------------------------------
# Test run - actually executes the generated script against a small sample
# input, in an isolated per-job venv, and produces a real output.xlsx a
# person can inspect before trusting the script. This is a genuine run
# against the live target site (not a dry-run/mock), capped at a handful of
# products so it stays quick and cheap.
#
# TRUST BOUNDARY: this executes agent-generated code on this machine with no
# sandbox beyond the isolated venv (dependency isolation only - not process,
# filesystem, or network isolation). That's the same trust boundary as
# running a script a teammate handed you. Fine for local/internal use; do
# not expose this endpoint to untrusted job submitters without adding real
# sandboxing (a container, seccomp, network egress limits) first.
# --------------------------------------------------------------------------

TEST_RUN_MAX_PRODUCTS = 3          # keep the sample run small - it's a smoke test,
                                    # not a production crawl, and every extra
                                    # product is another real request against the
                                    # live site.
PIP_INSTALL_TIMEOUT_SECONDS = 120
TEST_RUN_TIMEOUT_SECONDS = 180     # generous - covers venv create + pip install +
                                    # the script actually running


def _build_sample_input_xlsx(folder: Path, record: "JobRecord") -> Path:
    """
    Builds a one-row input.xlsx for the test run, in the same shape the
    MANDATORY SCRIPT CONTRACT requires every generated script's --input to
    accept (keyword / listing_url / detail_url columns) - reusing whatever
    the job itself was given (sample_keyword, target_url), so the test run
    exercises the same path the live inspection actually walked.
    """
    from openpyxl import Workbook  # local import - only this feature needs it

    wb = Workbook()
    ws = wb.active
    ws.title = "Sample Input"
    ws.append(["keyword", "listing_url", "detail_url"])
    ws.append([record.sample_keyword or None, record.target_url, None])
    path = folder / "sample_input.xlsx"
    wb.save(path)
    return path


async def _run_test_run(job_id: str):
    record = store.jobs.get(job_id)
    if record is None or not record.saved_to:
        return

    folder = Path(record.saved_to)
    record.test_run = {"status": "running", "started_at": datetime.now().isoformat(timespec="seconds")}
    await store._persist()

    venv_dir = folder / ".venv"
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    async def _run(*args, timeout: int, cwd: str | None = None):
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout, stderr

    try:
        # 1) isolated venv per job, so this script's requirements.txt can't
        #    collide with the service's own dependencies or another job's.
        if not venv_python.exists():
            rc, _, stderr = await _run(sys.executable, "-m", "venv", str(venv_dir), timeout=PIP_INSTALL_TIMEOUT_SECONDS)
            if rc != 0:
                raise RuntimeError(f"venv creation failed: {stderr.decode(errors='replace')[-2000:]}")

        # 2) install the generated script's own requirements.txt
        req_file = folder / "requirements.txt"
        if req_file.exists() and req_file.read_text(encoding="utf-8").strip():
            rc, _, stderr = await _run(
                str(venv_python), "-m", "pip", "install", "-q", "-r", str(req_file),
                timeout=PIP_INSTALL_TIMEOUT_SECONDS,
            )
            if rc != 0:
                raise RuntimeError(f"pip install -r requirements.txt failed: {stderr.decode(errors='replace')[-2000:]}")

        # 3) build a small sample input and actually run the script, per the
        #    MANDATORY SCRIPT CONTRACT's --input/--output/--max-products args
        sample_input = _build_sample_input_xlsx(folder, record)
        sample_output = folder / "sample_output.xlsx"
        if sample_output.exists():
            sample_output.unlink()  # don't let a stale file from a prior run look like success

        rc, stdout, stderr = await _run(
            str(venv_python), "generated_scraper.py",
            "--input", str(sample_input),
            "--output", str(sample_output),
            "--max-products", str(TEST_RUN_MAX_PRODUCTS),
            timeout=TEST_RUN_TIMEOUT_SECONDS,
            cwd=str(folder),
        )

        record.test_run = {
            "status": "passed" if rc == 0 and sample_output.exists() else "failed",
            "returncode": rc,
            "stdout_tail": stdout.decode(errors="replace")[-4000:],
            "stderr_tail": stderr.decode(errors="replace")[-4000:],
            "sample_output_ready": sample_output.exists(),
            "max_products": TEST_RUN_MAX_PRODUCTS,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }

    except asyncio.TimeoutError:
        record.test_run = {
            "status": "failed",
            "error": f"Test run timed out after {TEST_RUN_TIMEOUT_SECONDS}s.",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:
        record.test_run = {
            "status": "failed",
            "error": str(exc),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }

    await store._persist()


# --------------------------------------------------------------------------
# Job store - persisted to a local JSON file so job history (including the
# most recent job) survives an API service restart, not just an in-memory
# dict. This is a simple file, not a real database - swap for one in
# production - but it's genuinely durable across restarts, which an
# in-memory-only dict is not.
# --------------------------------------------------------------------------

JOBS_STORE_PATH = Path("jobs_store.json")


class JobStore:
    def __init__(self):
        self.jobs: dict[str, JobRecord] = {}
        self.live_logs: dict[str, asyncio.Queue] = {}
        self._persist_lock = asyncio.Lock()

    def load_from_disk(self):
        """Called once at startup. Missing/corrupt file -> start empty rather
        than crash the service - job history is useful, not load-bearing."""
        if not JOBS_STORE_PATH.exists():
            print(f"[inspection-service] No existing job store at {JOBS_STORE_PATH.resolve()} - starting fresh.")
            return
        try:
            raw = json.loads(JOBS_STORE_PATH.read_text(encoding="utf-8"))
            for job_id, record_dict in raw.items():
                self.jobs[job_id] = JobRecord(**record_dict)
                self.live_logs[job_id] = asyncio.Queue()  # fresh queue - old jobs don't need to re-stream
            print(f"[inspection-service] Restored {len(self.jobs)} job(s) from {JOBS_STORE_PATH.resolve()}")
        except Exception as exc:
            print(f"[inspection-service] Could not load job store ({exc}) - starting fresh.")

    async def _persist(self):
        async with self._persist_lock:
            data = {job_id: record.model_dump() for job_id, record in self.jobs.items()}
            JOBS_STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def create(self, req: CreateJobRequest) -> JobRecord:
        job_id = str(uuid.uuid4())
        record = JobRecord(
            job_id=job_id,
            name=req.name or f"Untitled scraper - {datetime.now().strftime('%b %d, %H:%M')}",
            status="queued",
            created_at=datetime.now().isoformat(timespec="seconds"),
            target_url=req.target_url,
            sample_keyword=req.sample_keyword,
        )
        self.jobs[job_id] = record
        self.live_logs[job_id] = asyncio.Queue()
        await self._persist()
        return record

    def get(self, job_id: str) -> JobRecord:
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        return self.jobs[job_id]

    async def update_status(self, job_id: str, status: str, result: InspectionResult | None = None):
        record = self.jobs[job_id]
        record.status = status
        if result:
            record.result = {
                "generated_code": result.generated_code,
                "requirements_txt": result.requirements_txt,
                "confidence_notes": result.confidence_notes,
                "total_tool_calls": result.total_tool_calls,
                "action_log": result.action_log,
            }
            record.cost = result.cost_record
            record.anti_bot_marker = result.anti_bot_marker

            if result.generated_code:
                record.saved_to = write_generated_files_to_disk(
                    job_id=job_id,
                    name=record.name,
                    generated_code=result.generated_code,
                    requirements_txt=result.requirements_txt,
                )
        await self._persist()


store = JobStore()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.post("/jobs", response_model=JobRecord)
async def create_job(req: CreateJobRequest):
    record = await store.create(req)

    job = InspectionJob(
        job_id=record.job_id,
        target_url=req.target_url,
        sample_keyword=req.sample_keyword,
        search_rules=req.search_rules,
        filter_rules=req.filter_rules,
        navigation_rules=req.navigation_rules,
        extraction_rules=req.extraction_rules,
        max_steps=req.max_steps,
        max_seconds=req.max_seconds,
        max_budget_usd=req.max_budget_usd,
    )

    # Fire and forget - in production this goes on the job queue instead of
    # asyncio.create_task, so it survives worker restarts and can be retried.
    asyncio.create_task(_execute(job))
    return record


async def _execute(job: InspectionJob):
    await store.update_status(job.job_id, "running")
    queue = store.live_logs[job.job_id]

    async def push_live(entry: dict):
        # Best-effort push to the SSE queue as each browser action happens -
        # this is what makes the Detail page's live log panel actually live,
        # rather than only populated after the job finishes.
        await queue.put(json.dumps(entry))

    result = await run_inspection_job(job, cost_sink=cost_sink, on_log_entry=push_live)
    await store.update_status(job.job_id, result.status, result)

    # Per-phase cost visibility in the server log - the two-phase pipeline
    # (recon vs. codegen) means one lump total no longer shows where the
    # spend actually went; this is the cheapest way to see it without a
    # dashboard. Full breakdown is also in result.cost_record["phases"] and
    # therefore in cost_ledger.jsonl (one line per phase).
    phases = (result.cost_record or {}).get("phases", [])
    if phases:
        breakdown = ", ".join(
            f"{p.get('phase') or 'unknown'}=${p.get('total_cost_usd') or 0:.4f} "
            f"({p.get('num_turns')}t, cache_read={p.get('cache_read_input_tokens') or 0})"
            for p in phases
        )
        print(f"[inspection-service] job={job.job_id} status={result.status} {breakdown}")

    await queue.put(None)  # sentinel: stream complete


@app.post("/jobs/{job_id}/test-run")
async def start_test_run(job_id: str):
    """
    Kicks off a real test run of the generated script: builds a one-row
    sample input from the job's own target_url/sample_keyword, installs the
    script's requirements.txt into an isolated per-job venv, and runs it
    against the live site capped at TEST_RUN_MAX_PRODUCTS rows. Returns
    immediately ("started") - poll GET /jobs/{id}/test-run for the result,
    since venv creation + pip install + the run itself can take a while.

    Requires the job to have a generated script on disk already (i.e. it
    reached a terminal status with usable generated_code).
    """
    record = store.get(job_id)
    if not record.saved_to:
        raise HTTPException(status_code=400, detail="This job has no generated script yet - nothing to test run.")
    if record.test_run and record.test_run.get("status") == "running":
        return {"status": "already_running"}

    asyncio.create_task(_run_test_run(job_id))
    return {"status": "started"}


@app.get("/jobs/{job_id}/test-run")
async def get_test_run_status(job_id: str):
    """Poll this while a test run is in flight - see start_test_run() above."""
    record = store.get(job_id)
    return record.test_run or {"status": "not_run"}


@app.get("/jobs/{job_id}/sample-output")
async def download_sample_output(job_id: str):
    """Downloads the .xlsx a passing test run produced. 404s until one exists."""
    record = store.get(job_id)
    if not record.saved_to:
        raise HTTPException(status_code=404, detail="No generated script for this job.")
    sample_output = Path(record.saved_to) / "sample_output.xlsx"
    if not sample_output.exists():
        raise HTTPException(status_code=404, detail="No sample output yet - start a test run first (POST /jobs/{id}/test-run).")
    return FileResponse(
        path=str(sample_output),
        filename=f"{_safe_folder_name(record.name, job_id)}_sample_output.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/jobs/{job_id}/cost")
async def get_job_cost(job_id: str):
    """Standalone endpoint for the Cost Ops dashboard to poll/pull per-job
    spend without fetching the full result payload (generated code, action
    log, etc.) each time."""
    record = store.get(job_id)
    if record.cost is None:
        return {"job_id": job_id, "status": record.status, "cost": None}
    return {"job_id": job_id, "status": record.status, "cost": record.cost}


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str):
    return store.get(job_id)


@app.get("/jobs")
async def list_jobs():
    return list(store.jobs.values())


@app.get("/jobs/{job_id}/stream")
async def stream_job_log(job_id: str):
    """
    Server-Sent Events endpoint - the Detail page's live agent log panel
    subscribes to this while a job is running.
    """
    store.get(job_id)  # 404s if unknown

    async def event_generator():
        queue = store.live_logs[job_id]
        while True:
            item = await queue.get()
            if item is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield f"data: {item}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
