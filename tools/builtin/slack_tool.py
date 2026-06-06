"""Slack builtin tools for Anton."""

import os
from typing import Any
from pydantic import BaseModel, Field
from config.configuration import Config
from tools.tool_interface import Tool, ToolInvocation, ToolResult, ToolKind


class SlackPostParams(BaseModel):
    message: str = Field(..., description="Message text to post")
    channel: str = Field(None, description="Slack channel (defaults to SLACK_CHANNEL env var)")


class SlackPostTool(Tool):
    name = "slack_post"
    description = "Post a plain-text message to a Slack channel."
    kind = ToolKind.NETWORK
    schema = SlackPostParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from integrations.slack_bot import get_slack_notifier
        params = SlackPostParams(**invocation.params)
        notifier = get_slack_notifier(channel=params.channel)
        try:
            await notifier.post_simple(params.message)
            return ToolResult.success_result(output="Message posted to Slack")
        except Exception as e:
            return ToolResult.error_result(f"Slack error: {e}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True


class SlackBriefingParams(BaseModel):
    ticket_key: str = Field(..., description="Jira ticket key e.g. BUG-42")
    summary: str = Field(..., description="One-line incident summary")
    priority: str = Field(..., description="Priority label e.g. P1 Critical")
    component: str = Field(..., description="Affected component or service")
    root_cause: str = Field(..., description="Technical root cause explanation")
    fix_summary: str = Field(..., description="What was changed and why")
    files_changed: list[str] = Field(default_factory=list, description="List of changed file paths")
    tests_added: int = Field(0, description="Number of new tests added")
    tests_total: int = Field(0, description="Total tests in suite")
    ci_green: bool = Field(True, description="Whether CI is passing")
    branch: str = Field(..., description="Fix branch name")
    run_id: str = Field(..., description="Pipeline run ID for HITL correlation")
    channel: str = Field(None, description="Slack channel override")


class SlackBriefingTool(Tool):
    name = "slack_post_briefing"
    description = (
        "Post a structured Anton incident briefing to Slack with "
        "Approve and Request Changes interactive buttons for human-in-the-loop review."
    )
    kind = ToolKind.NETWORK
    schema = SlackBriefingParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from integrations.slack_bot import get_slack_notifier, build_briefing_blocks
        params = SlackBriefingParams(**invocation.params)
        notifier = get_slack_notifier(channel=params.channel)
        blocks = build_briefing_blocks(
            ticket_key=params.ticket_key,
            summary=params.summary,
            priority=params.priority,
            component=params.component,
            root_cause=params.root_cause,
            fix_summary=params.fix_summary,
            files_changed=params.files_changed,
            tests_added=params.tests_added,
            tests_total=params.tests_total,
            ci_green=params.ci_green,
            branch=params.branch,
            run_id=params.run_id,
        )
        try:
            ts = await notifier.post_briefing(blocks)
            return ToolResult.success_result(
                output=f"Briefing posted to Slack (ts={ts})",
                metadata={"ts": ts},
            )
        except Exception as e:
            return ToolResult.error_result(f"Slack error: {e}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True
