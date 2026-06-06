"""
LIVE Slack demo via Socket Mode — NO ngrok, NO public URL.

The bot opens an OUTBOUND websocket to Slack; button clicks + slash commands arrive
over it. Nothing on this machine is exposed to the internet.

What it does:
  - On start: the watcher sees the target repo's HEAD failing and fires the pipeline
    (autonomy, no human command). A REAL Slack briefing with Approve / Request Changes
    buttons is posted to your channel.
  - Approve button -> ProvenancePipeline.approve -> real GitHub PR (fix + receipt) ->
    Slack message updated.
  - Rigged receipts (receipt_tampered.json / receipt_forged.json) are staged at the repo
    root for the standalone verifier (beat 3b).

Toggle modes live with a Slack slash command (no restart):
    /anton manual   -> human-in-the-loop: post briefing, wait for Approve button
    /anton auto     -> full-autonomous: Anton approves + merges itself, no human
    /anton run      -> trigger a fresh incident now (uses the current mode)
    /anton status   -> show the current mode
Starting mode is taken from env AUTO_APPROVE / AUTO_MERGE (default: manual).

Requires in ROOT/.env:
    SLACK_BOT_TOKEN=xoxb-...      (scope: chat:write, commands)
    SLACK_APP_TOKEN=xapp-...      (scope: connections:write — enables Socket Mode)
    SLACK_CHANNEL=#your-channel
    OPENROUTER_API_KEY=...        (live agent)
    GITHUB_TOKEN=... GITHUB_REPO=owner/name   (optional — real PR)

Slack app one-time setup for the slash command: Features -> Slash Commands ->
Create New Command -> Command `/anton` (with Socket Mode, leave Request URL blank).

Run (from the repo root):
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

# Runtime mode (togglable live via /anton). Starting value comes from env.
#   auto  -> Anton approves itself (no human click)
#   merge -> also merge the PR
_mode = {
    "auto": os.environ.get("AUTO_APPROVE", "").lower() in ("1", "true", "yes"),
    "merge": os.environ.get("AUTO_MERGE", "").lower() in ("1", "true", "yes"),
}


def _mode_label() -> str:
    if _mode["auto"]:
        return "FULL-AUTONOMOUS (auto-approve%s)" % (" + auto-merge" if _mode["merge"] else "")
    return "HUMAN-IN-THE-LOOP (Approve button required)"


def _stage_rigged_receipts(run: ProvRun) -> None:
    honest = ROOT / "receipt.json"
    shutil.copyfile(run.receipt_path, honest)
    receipt = json.loads(honest.read_text())
    (ROOT / "receipt_tampered.json").write_text(json.dumps(rig.tampered_diff(receipt), indent=2))
    (ROOT / "receipt_forged.json").write_text(json.dumps(rig.forged_claim(receipt), indent=2))


async def _run_incident() -> ProvRun:
    """Run one full pipeline cycle, honoring the current mode."""
    run_id = f"demo-{int(time.time()) % 100000}"
    run = await _pipeline.run(summary=SUMMARY, bug_report=BUG_REPORT, run_id=run_id)
    _runs[run.run_id] = run
    _stage_rigged_receipts(run)
    print(f"  briefed Slack — run {run.run_id} (mode: {_mode_label()})")
    if _mode["auto"]:
        await _pipeline.approve(run, auto=True, merge=_mode["merge"])
        print(f"  auto-approved -> {run.pr_url}" + (" (merged)" if run.merged else ""))
    return run


async def _trigger_via_watcher() -> None:
    """Autonomy: only fires if the watcher actually sees CI failing on HEAD."""
    watcher = CIWatcher(str(REPO))

    async def on_failure(commit: str, log: str):
        await _run_incident()

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
    async def on_approve(ack, body):
        await ack()
        run_id = body["actions"][0]["value"].split("|", 1)[-1]
        run = _runs.get(run_id)
        if not run:
            return
        print(f"  APPROVE clicked — opening PR for {run_id}")
        await _pipeline.approve(run, auto=False, merge=_mode["merge"])

    @app.action("hitl_reject")
    async def on_reject(ack):
        await ack()
        print("  REJECT clicked")

    @app.command("/anton")
    async def on_cmd(ack, command, respond):
        await ack()
        text = (command.get("text") or "").strip().lower()
        if text == "auto":
            _mode["auto"], _mode["merge"] = True, True
            await respond(f"Mode → *{_mode_label()}*. Next incident is fully autonomous.")
        elif text == "manual":
            _mode["auto"] = False
            await respond(f"Mode → *{_mode_label()}*. Next incident waits for the Approve button.")
        elif text == "run":
            await respond(f"Triggering an incident now (mode: *{_mode_label()}*)…")
            await _run_incident()
        else:
            await respond(f"Anton mode: *{_mode_label()}*. "
                          "Commands: `/anton manual`, `/anton auto`, `/anton run`, `/anton status`.")

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()         # outbound socket; no public URL
    print(f"Socket Mode connected. Starting mode: {_mode_label()}.")
    print("Firing the autonomous trigger (watcher)…")
    await _trigger_via_watcher()
    print("Ready. Toggle with /anton manual | /anton auto | /anton run. Ctrl-C to stop.")
    await asyncio.Event().wait()          # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
