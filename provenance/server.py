"""
Focused FastAPI server for the LIVE Slack-button demo.

Why a separate server (not main.py): main.py wires the OLD OnCallPipeline + Jira.
This one drives ONLY the provenance pipeline, so the Approve button opens OUR PR with
the receipt — no untangling, less stage risk.

Flow:
  POST /trigger        -> watcher sees HEAD failing, runs the pipeline, posts a REAL
                          Slack briefing with Approve / Request Changes buttons.
  POST /slack/actions  -> Slack calls this when a button is clicked. Approve ->
                          ProvenancePipeline.approve (real PR if GitHub creds) ->
                          updates the Slack message.

Run (from anton/, with deps installed and .env loaded):
    ./.venv/bin/uvicorn provenance.server:app --port 8000

Then expose it publicly (Slack must reach it):  ngrok http 8000
and set the Slack app's Interactivity Request URL to  https://<ngrok>/slack/actions
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]   # …/anton (repo root)
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from provenance.pipeline import ProvenancePipeline, ProvRun
from provenance.watcher import CIWatcher
from provenance import rig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("anton.provenance.server")

REPO = ROOT / "target_repo"
SUMMARY = "histogram::bucket_for returns an out-of-range index for the maximum value"
BUG_REPORT = (
    "histogram::bucket_for(value,min,max,n_buckets) must return an index in [0,n_buckets). "
    "For value==max it returns n_buckets (out of range) and panics the collector. "
    "Failing tests: max_value_lands_in_last_bucket, histogram_never_indexes_out_of_range."
)

app = FastAPI(title="Anton — Provenance Demo")
_pipeline = ProvenancePipeline(str(REPO), crate="histogram")
_runs: dict[str, ProvRun] = {}


@app.get("/")
def health():
    return {"service": "anton-provenance", "runs": list(_runs.keys())}


@app.post("/trigger")
async def trigger():
    """Autonomy + live fix. Posts the real Slack briefing with buttons."""
    watcher = CIWatcher(str(REPO))

    async def on_failure(commit: str, log: str):
        run = await _pipeline.run(summary=SUMMARY, bug_report=BUG_REPORT, run_id="demo01")
        _runs[run.run_id] = run
        # Stage the rigged receipts next to the honest one for beat 3b.
        import shutil
        honest = ROOT / "receipt.json"
        shutil.copyfile(run.receipt_path, honest)
        receipt = json.loads(honest.read_text())
        (ROOT / "receipt_tampered.json").write_text(json.dumps(rig.tampered_diff(receipt), indent=2))
        (ROOT / "receipt_forged.json").write_text(json.dumps(rig.forged_claim(receipt), indent=2))

    fired = await watcher.watch_once(on_failure)
    if not fired:
        return JSONResponse({"status": "no_failure", "detail": "HEAD CI is green; nothing to fix"})
    run = _runs["demo01"]
    last = run.outcome.results[-1].entry
    return JSONResponse({"status": "briefed", "run_id": run.run_id,
                         "verdict": last.verdict, "entry_hash": last.entry_hash})


@app.post("/slack/actions")
async def slack_actions(request: Request):
    """Slack interactive callback. Approve -> open the PR with the receipt."""
    form = await request.form()
    payload = json.loads(form.get("payload", "{}"))
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse({"status": "no_action"})

    value = actions[0].get("value", "")        # "approve|<run_id>" / "reject|<run_id>"
    decision, _, run_id = value.partition("|")
    run = _runs.get(run_id)
    if not run:
        return JSONResponse({"status": "unknown_run", "run_id": run_id})

    if decision == "approve":
        pr_url = await _pipeline.approve(run)
        logger.info("Approved %s -> PR %s", run_id, pr_url)
        return JSONResponse({"status": "approved", "pr_url": pr_url})

    # reject -> just acknowledge for the demo
    return JSONResponse({"status": "changes_requested", "run_id": run_id})
