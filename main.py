"""
Anton — Entry point.

Runs a FastAPI server that:
  - Accepts Jira webhooks  →  triggers the parallel agent pipeline
  - Accepts Slack interactive callbacks  →  handles Approve / Request Changes

Start with:
    uvicorn main:app --port 8000 --reload

Then fire the demo with:
    python demo/trigger.py
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.responses import JSONResponse

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("anton")

# Suppress noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from integrations.jira_client import get_jira_client, JiraTicket
from integrations.slack_bot import get_slack_notifier
from workflow.oncall_pipeline import OnCallPipeline

# ── In-memory store of active pipeline runs (keyed by run_id) ────────────────
# In production this would be Redis / a database
_active_runs: dict[str, "PipelineRun"] = {}  # type: ignore

REPO_CWD = os.getenv("REPO_CWD", os.path.join(os.path.dirname(__file__), "demo/sample_repo"))


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "━" * 60)
    print("  🚨  Anton — Ready")
    print(f"  Repo CWD : {REPO_CWD}")
    print(f"  Jira     : {'live' if os.getenv('JIRA_BASE_URL') else 'mock'}")
    print(f"  Slack    : {'live' if os.getenv('SLACK_BOT_TOKEN') else 'mock (terminal output)'}")
    print(f"  GitHub   : {'live' if os.getenv('GITHUB_TOKEN') else 'mock'}")
    print("━" * 60 + "\n")
    yield


app = FastAPI(title="Anton", version="1.0.0", lifespan=lifespan)


# ── Jira webhook ──────────────────────────────────────────────────────────────

@app.post("/webhook/jira")
async def jira_webhook(request: Request, background: BackgroundTasks):
    """
    Receives a Jira issue_created / issue_updated webhook.
    Immediately returns 200, runs the pipeline in the background.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("webhookEvent", "")
    issue_data = payload.get("issue", {})
    issue_key = issue_data.get("key", "UNKNOWN")

    if not issue_key or issue_key == "UNKNOWN":
        return JSONResponse({"status": "ignored", "reason": "no issue key"})

    logger.info("Jira webhook received: %s  event=%s", issue_key, event)

    # Parse inline (for demo we use the mock client's richer description)
    jira = get_jira_client()
    try:
        ticket = await jira.get_ticket(issue_key)
    except Exception as e:
        # If real Jira call fails, fall back to payload data
        logger.warning("Jira API call failed (%s), building ticket from webhook payload", e)
        fields = issue_data.get("fields", {})
        ticket = JiraTicket(
            key=issue_key,
            summary=fields.get("summary", "No summary"),
            description=str(fields.get("description", "")),
            priority=fields.get("priority", {}).get("name", "High"),
            status=fields.get("status", {}).get("name", "Open"),
            reporter=fields.get("reporter", {}).get("displayName", "Unknown"),
            labels=fields.get("labels", []),
            components=[c["name"] for c in fields.get("components", [])],
            project_key=issue_key.split("-")[0],
        )

    background.add_task(_run_pipeline, ticket)
    return JSONResponse({"status": "accepted", "ticket": issue_key})


async def _run_pipeline(ticket: JiraTicket) -> None:
    pipeline_runner = OnCallPipeline(repo_cwd=REPO_CWD)
    try:
        pipeline = await pipeline_runner.run(ticket)
        _active_runs[pipeline.run_id] = pipeline
        logger.info("Pipeline %s complete — waiting for HITL decision", pipeline.run_id)
    except Exception as e:
        logger.exception("Pipeline failed for %s: %s", ticket.key, e)


# ── Slack slash command /oncall ───────────────────────────────────────────────

@app.post("/webhook/slack/command")
async def slack_command(request: Request, background: BackgroundTasks):
    """
    Handles the /oncall slash command from Slack.
    Usage: /oncall  (fires the demo bug ticket)
    """
    form = await request.form()
    user_name = form.get("user_name", "someone")
    text = (form.get("text") or "").strip()

    if not text:
        summary = "Completing a todo un-completes it on second call"
        description = (
            "BUG REPORT — P1 Critical\n\n"
            "When a user marks a todo as complete, it works the first time. "
            "But calling complete() a second time reverts it back to incomplete. "
            "Root cause: todos/service.py complete() uses `not todo.completed` (toggle) "
            "instead of always setting `todo.completed = True`.\n\n"
            "Steps to reproduce:\n"
            "1. Create a todo\n"
            "2. POST /todos/1/complete  → completed: true ✓\n"
            "3. POST /todos/1/complete again → completed: false ✗\n\n"
            "CI failing: test_complete_is_idempotent"
        )
    else:
        summary = text[:120]
        description = f"BUG REPORT submitted via Slack by {user_name}:\n\n{text}"

    ticket = JiraTicket(
        key="BUG-42",
        summary=summary,
        description=description,
        priority="Critical",
        status="Open",
        reporter=user_name,
        labels=["bug", "p1", "todo-service"],
        components=["todo-service"],
        project_key="BUG",
    )

    background.add_task(_run_pipeline, ticket)

    return JSONResponse({
        "response_type": "in_channel",
        "text": f"🚨 *Anton activated* — _{summary[:80]}_\nFour agents launching in parallel. Briefing incoming to <#aria-oncall>..."
    })


# ── Slack interactive callbacks ───────────────────────────────────────────────

@app.post("/webhook/slack/actions")
async def slack_actions(request: Request, background: BackgroundTasks):
    """
    Receives Slack Block Kit interactive component callbacks.
    Handles 'hitl_approve' and 'hitl_reject' button actions.
    """
    form = await request.form()
    payload_str = form.get("payload", "")
    if not payload_str:
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload JSON")

    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse({"status": "no_action"})

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")

    # value format: "approve|<run_id>" or "reject|<run_id>"
    parts = value.split("|", 1)
    if len(parts) != 2:
        return JSONResponse({"status": "ignored"})

    decision, run_id = parts
    pipeline = _active_runs.get(run_id)

    if not pipeline:
        logger.warning("No active pipeline for run_id=%s", run_id)
        return JSONResponse({"status": "not_found"})

    if decision == "approve" or action_id == "hitl_approve":
        logger.info("HITL: APPROVED — run %s", run_id)
        background.add_task(_handle_approval, pipeline)

    elif decision == "reject" or action_id == "hitl_reject":
        feedback = payload.get("state", {}).get("values", {}).get("feedback", {}).get("value", "No specific feedback provided.")
        logger.info("HITL: REJECTED — run %s — feedback: %s", run_id, feedback)
        background.add_task(_handle_rejection, pipeline, feedback)

    # Slack expects a 200 immediately
    return JSONResponse({"status": "ok"})


async def _handle_approval(pipeline) -> None:
    runner = OnCallPipeline(repo_cwd=REPO_CWD)
    runner.slack = get_slack_notifier()
    runner.github = pipeline.github if hasattr(pipeline, "github") else __import__(
        "integrations.github_client", fromlist=["get_github_client"]
    ).get_github_client()

    jira = get_jira_client()
    try:
        pr = await runner.approve(pipeline)
        await jira.link_pr(pipeline.ticket.key, pr.url, pr.title)
        logger.info("✅  %s closed. PR: %s", pipeline.ticket.key, pr.url)
        print(f"\n🎉  Done! PR: {pr.url}\n")
    except Exception as e:
        logger.exception("Approval handling failed: %s", e)


async def _handle_rejection(pipeline, feedback: str) -> None:
    runner = OnCallPipeline(repo_cwd=REPO_CWD)
    runner.slack = get_slack_notifier()
    try:
        await runner.request_changes(pipeline, feedback)
        logger.info("Code Agent re-running with feedback for %s", pipeline.ticket.key)
    except Exception as e:
        logger.exception("Rejection handling failed: %s", e)


# ── Health / status endpoints ─────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "anton", "active_runs": len(_active_runs)}


@app.get("/runs")
def list_runs():
    return {
        run_id: {
            "ticket": r.ticket.key,
            "approved": r.approved,
            "pr_url": r.pr.url if r.pr else None,
        }
        for run_id, r in _active_runs.items()
    }


@app.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, background: BackgroundTasks):
    """Demo-friendly approve endpoint — call this after tapping Approve in Slack."""
    pipeline = _active_runs.get(run_id)
    if not pipeline:
        # Try latest run if run_id == "latest"
        if run_id == "latest" and _active_runs:
            pipeline = list(_active_runs.values())[-1]
            run_id = list(_active_runs.keys())[-1]
        else:
            raise HTTPException(status_code=404, detail="Run not found")
    if pipeline.approved is True:
        return {"status": "already_approved", "pr_url": pipeline.pr.url if pipeline.pr else None}
    background.add_task(_handle_approval, pipeline)
    return {"status": "approving", "run_id": run_id, "ticket": pipeline.ticket.key}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    pipeline = _active_runs.get(run_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run_id": run_id,
        "ticket": pipeline.ticket.key,
        "priority": pipeline.triage.priority if pipeline.triage else None,
        "component": pipeline.triage.component if pipeline.triage else None,
        "fix": pipeline.code.fix_explanation if pipeline.code else None,
        "tests_passing": pipeline.test.all_passing if pipeline.test else None,
        "approved": pipeline.approved,
        "pr_url": pipeline.pr.url if pipeline.pr else None,
    }
