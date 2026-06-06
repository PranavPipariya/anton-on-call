"""
LIVE Slack demo via Socket Mode — NO ngrok, NO public URL.

The bot opens an OUTBOUND websocket to Slack; button clicks arrive over it. Nothing
on this machine is exposed to the internet.

What it does:
  - On start: the watcher sees the target repo's HEAD failing and fires the pipeline
    (autonomy, no human command). A REAL Slack briefing with Approve / Request Changes
    buttons is posted to your channel.
  - When you click Approve: ProvenancePipeline.approve runs -> opens a real GitHub PR
    (if GITHUB_* set) with the fix + receipt -> updates the Slack message.
  - Rigged receipts (receipt_tampered.json / receipt_forged.json) are staged at the repo
    root for the standalone verifier (beat 3b).

Requires in ROOT/.env:
    SLACK_BOT_TOKEN=xoxb-...      (scope: chat:write)
    SLACK_APP_TOKEN=xapp-...      (scope: connections:write — enables Socket Mode)
    SLACK_CHANNEL=#your-channel
    OPENROUTER_API_KEY=...        (live agent)
    GITHUB_TOKEN=... GITHUB_REPO=owner/name   (optional — real PR)

Run (from anton/):
    ./.venv/bin/python -m provenance.socket_app
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]   # …/anton (repo root)
load_dotenv(ROOT / ".env")

from provenance.pipeline import ProvenancePipeline, ProvRun
from provenance.watcher import CIWatcher
from provenance import rig

REPO = ROOT / "target_repo"
SUMMARY = "histogram::bucket_for returns an out-of-range index for the maximum value"
BUG_REPORT = (
    "histogram::bucket_for(value,min,max,n_buckets) must return an index in [0,n_buckets). "
    "For value==max it returns n_buckets (out of range) and panics the collector. "
    "Failing tests: max_value_lands_in_last_bucket, histogram_never_indexes_out_of_range."
)

_pipeline = ProvenancePipeline(str(REPO), crate="histogram")
_runs: dict[str, ProvRun] = {}

# Two demo modes:
#   AUTO_APPROVE=1  -> full autonomous: Anton approves itself, no human click (24x7).
#   AUTO_MERGE=1    -> also merge the PR (only meaningful with AUTO_APPROVE).
# Default (both unset) -> human-in-the-loop: post briefing, wait for the Approve button.
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "").lower() in ("1", "true", "yes")
AUTO_MERGE = os.environ.get("AUTO_MERGE", "").lower() in ("1", "true", "yes")


def _stage_rigged_receipts(run: ProvRun) -> None:
    honest = ROOT / "receipt.json"
    shutil.copyfile(run.receipt_path, honest)
    receipt = json.loads(honest.read_text())
    (ROOT / "receipt_tampered.json").write_text(json.dumps(rig.tampered_diff(receipt), indent=2))
    (ROOT / "receipt_forged.json").write_text(json.dumps(rig.forged_claim(receipt), indent=2))


async def _trigger() -> None:
    """Autonomy + live fix; posts the real Slack briefing with buttons."""
    watcher = CIWatcher(str(REPO))

    async def on_failure(commit: str, log: str):
        run_id = f"demo-{int(time.time()) % 100000}"
        run = await _pipeline.run(summary=SUMMARY, bug_report=BUG_REPORT, run_id=run_id)
        _runs[run.run_id] = run
        _stage_rigged_receipts(run)
        print(f"  briefed Slack — run {run.run_id}, verify with ./verify receipt.json")
        if AUTO_APPROVE:
            print(f"  AUTO_APPROVE on — Anton approving itself (merge={AUTO_MERGE})")
            await _pipeline.approve(run, auto=True, merge=AUTO_MERGE)
            print(f"  auto-approved -> {run.pr_url}" + (" (merged)" if run.merged else ""))

    fired = await watcher.watch_once(on_failure)
    if not fired:
        print("HEAD CI is green — nothing to fix. (Did the bug get reverted?)")


async def main() -> None:
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("Missing SLACK_BOT_TOKEN / SLACK_APP_TOKEN in .env — see this file's docstring.")
        sys.exit(1)

    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

    app = AsyncApp(token=bot_token)

    @app.action("hitl_approve")
    async def on_approve(ack, body, client):
        await ack()
        run_id = body["actions"][0]["value"].split("|", 1)[-1]
        run = _runs.get(run_id)
        if not run:
            return
        print(f"  APPROVE clicked — opening PR for {run_id}")
        await _pipeline.approve(run, auto=False, merge=AUTO_MERGE)  # opens PR + updates Slack

    @app.action("hitl_reject")
    async def on_reject(ack, body):
        await ack()
        print("  REJECT clicked")

    mode = "FULL-AUTONOMOUS (auto-approve%s)" % (" + auto-merge" if AUTO_MERGE else "") \
        if AUTO_APPROVE else "HUMAN-IN-THE-LOOP (waiting for Approve button)"
    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()         # outbound socket; no public URL
    print(f"Socket Mode connected. Mode: {mode}. Firing the autonomous trigger...")
    await _trigger()
    if AUTO_APPROVE:
        print("Done — full autonomous cycle complete. Ctrl-C to stop.")
    else:
        print("Listening for the Approve button. Ctrl-C to stop.")
    await asyncio.Event().wait()          # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
