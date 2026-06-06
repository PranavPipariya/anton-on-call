"""Jira builtin tools for Anton."""

import os
from typing import Any, Optional
from pydantic import BaseModel, Field
from config.configuration import Config
from tools.tool_interface import Tool, ToolInvocation, ToolResult, ToolKind


class JiraGetTicketParams(BaseModel):
    issue_key: str = Field(..., description="Jira issue key, e.g. BUG-42")


class JiraGetTicketTool(Tool):
    name = "jira_get_ticket"
    description = "Fetch a Jira ticket by key. Returns summary, description, priority, status, reporter, labels and components."
    kind = ToolKind.NETWORK
    schema = JiraGetTicketParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from integrations.jira_client import get_jira_client
        params = JiraGetTicketParams(**invocation.params)
        client = get_jira_client()
        try:
            ticket = await client.get_ticket(params.issue_key)
            output = (
                f"# {ticket.key}: {ticket.summary}\n\n"
                f"**Priority:** {ticket.priority}\n"
                f"**Status:** {ticket.status}\n"
                f"**Reporter:** {ticket.reporter}\n"
                f"**Labels:** {', '.join(ticket.labels) or 'None'}\n"
                f"**Components:** {', '.join(ticket.components) or 'None'}\n\n"
                f"## Description\n{ticket.description}"
            )
            return ToolResult.success_result(output=output)
        except Exception as e:
            return ToolResult.error_result(f"Jira error: {e}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return False


class JiraCommentParams(BaseModel):
    issue_key: str = Field(..., description="Jira issue key")
    body: str = Field(..., description="Comment text to post")


class JiraCommentTool(Tool):
    name = "jira_add_comment"
    description = "Post a comment on a Jira ticket."
    kind = ToolKind.NETWORK
    schema = JiraCommentParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from integrations.jira_client import get_jira_client
        params = JiraCommentParams(**invocation.params)
        client = get_jira_client()
        try:
            await client.add_comment(params.issue_key, params.body)
            return ToolResult.success_result(output=f"Comment posted on {params.issue_key}")
        except Exception as e:
            return ToolResult.error_result(f"Jira error: {e}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True


class JiraCloseParams(BaseModel):
    issue_key: str = Field(..., description="Jira issue key to close")
    pr_url: str = Field(..., description="Pull request URL to link")
    pr_title: str = Field(..., description="Pull request title")


class JiraCloseTool(Tool):
    name = "jira_close_ticket"
    description = "Close a Jira ticket and link it to a pull request. Posts a comment and transitions status to Done."
    kind = ToolKind.NETWORK
    schema = JiraCloseParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from integrations.jira_client import get_jira_client
        params = JiraCloseParams(**invocation.params)
        client = get_jira_client()
        try:
            await client.link_pr(params.issue_key, params.pr_url, params.pr_title)
            return ToolResult.success_result(output=f"✅ {params.issue_key} closed and linked to PR")
        except Exception as e:
            return ToolResult.error_result(f"Jira error: {e}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True
