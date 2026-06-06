"""
Slack integration for Anton.

Sends structured Block Kit incident briefings and handles
Approve / Request Changes interactive button callbacks.
"""

import logging
import os
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

try:
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.errors import SlackApiError
    HAS_SLACK = True
except ImportError:
    HAS_SLACK = False
    AsyncWebClient = None


# ── Priority formatting ───────────────────────────────────────────────────────

_PRIORITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🟢",
}

_CI_EMOJI = {True: "✅", False: "❌"}


def _priority_label(priority: str) -> str:
    emoji = _PRIORITY_EMOJI.get(priority.lower(), "⚪")
    return f"{emoji} {priority}"


# ── Block Kit message builder ─────────────────────────────────────────────────

def build_briefing_blocks(
    ticket_key: str,
    summary: str,
    priority: str,
    component: str,
    root_cause: str,
    fix_summary: str,
    files_changed: list[str],
    tests_added: int,
    tests_total: int,
    ci_green: bool,
    branch: str,
    run_id: str,
) -> list[dict]:
    """Build the Slack Block Kit payload for the incident briefing."""

    files_str = "\n".join(f"• `{f}`" for f in files_changed) if files_changed else "• _(none)_"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚨  Anton — Incident Response Ready", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Ticket*\n<https://your-jira.atlassian.net/browse/{ticket_key}|{ticket_key}>"},
                {"type": "mrkdwn", "text": f"*Priority*\n{_priority_label(priority)}"},
                {"type": "mrkdwn", "text": f"*Component*\n`{component}`"},
                {"type": "mrkdwn", "text": f"*Branch*\n`{branch}`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root Cause*\n{root_cause}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Fix Applied*\n{fix_summary}\n\n{files_str}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Tests*\n{tests_added} new  ·  {tests_total} total  ·  All passing ✅",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*CI Status*\n{_CI_EMOJI[ci_green]}  {'All checks green' if ci_green else 'Checks failing'}",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"hitl_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅  Approve & Open PR", "emoji": True},
                    "style": "primary",
                    "value": f"approve|{run_id}",
                    "action_id": "hitl_approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️  Request Changes", "emoji": True},
                    "style": "danger",
                    "value": f"reject|{run_id}",
                    "action_id": "hitl_reject",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Powered by *Anton*  ·  respond within 30 min or PR will be flagged for manual review"},
            ],
        },
    ]


def build_approved_blocks(ticket_key: str, pr_url: str, pr_title: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅  *Approved* — PR opened for `{ticket_key}`\n<{pr_url}|{pr_title}>",
            },
        },
    ]


def build_rejected_blocks(ticket_key: str, feedback: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✏️  *Changes Requested* for `{ticket_key}`\nFeedback routed back to Code Agent.\n\n_{feedback}_",
            },
        },
    ]


# ── Slack client wrapper ──────────────────────────────────────────────────────

class SlackNotifier:
    def __init__(self, token: str, channel: str):
        if not HAS_SLACK:
            raise ImportError("slack_sdk not installed. Run: pip install slack-sdk")
        self.client = AsyncWebClient(token=token)
        self.channel = channel

    async def post_briefing(self, blocks: list[dict], text: str = "Anton — Incident Response Ready") -> str:
        """Post the briefing and return 'channel_id|ts' for later updates."""
        try:
            resp = await self.client.chat_postMessage(
                channel=self.channel,
                blocks=blocks,
                text=text,
            )
            channel_id = resp["channel"]
            ts = resp["ts"]
            logger.info("Briefing posted to Slack channel=%s ts=%s", channel_id, ts)
            return f"{channel_id}|{ts}"
        except SlackApiError as e:
            logger.error("Slack post failed: %s", e.response["error"])
            raise

    async def update_message(self, channel_ts: str, blocks: list[dict], text: str = "") -> None:
        """Replace the briefing message using the stored channel ID."""
        if "|" in channel_ts:
            channel_id, ts = channel_ts.split("|", 1)
        else:
            channel_id, ts = self.channel, channel_ts
        try:
            await self.client.chat_update(
                channel=channel_id,
                ts=ts,
                blocks=blocks,
                text=text,
            )
        except SlackApiError as e:
            logger.error("Slack update failed: %s", e.response["error"])
            raise

    async def post_simple(self, text: str) -> None:
        await self.client.chat_postMessage(channel=self.channel, text=text)


# ── Mock notifier for demo without Slack creds ───────────────────────────────

class MockSlackNotifier:
    def __init__(self):
        self.channel = "#anton-demo"

    async def post_briefing(self, blocks: list[dict], text: str = "") -> str:
        print("\n" + "━" * 60)
        print("📨  SLACK MESSAGE → #anton")
        print("━" * 60)
        # Pretty-print the key sections
        for block in blocks:
            btype = block.get("type")
            if btype == "header":
                print(f"\n  {block['text']['text']}")
            elif btype == "section":
                if "text" in block:
                    print(f"\n  {block['text']['text']}")
                if "fields" in block:
                    for f in block["fields"]:
                        print(f"  {f['text']}")
            elif btype == "actions":
                labels = [e["text"]["text"] for e in block.get("elements", [])]
                print(f"\n  BUTTONS: {' | '.join(labels)}")
            elif btype == "divider":
                print("  " + "─" * 50)
        print("━" * 60 + "\n")
        return "mock_ts_12345"

    async def update_message(self, ts: str, blocks: list[dict], text: str = "") -> None:
        for block in blocks:
            if block.get("type") == "section":
                print(f"\n  [Slack updated] {block['text']['text']}\n")

    async def post_simple(self, text: str) -> None:
        print(f"\n  [Slack] {text}\n")


def get_slack_notifier(channel: Optional[str] = None):
    token = os.getenv("SLACK_BOT_TOKEN")
    ch = channel or os.getenv("SLACK_CHANNEL", "#anton")
    if token:
        return SlackNotifier(token, ch)
    logger.warning("SLACK_BOT_TOKEN not set — using mock notifier")
    return MockSlackNotifier()
