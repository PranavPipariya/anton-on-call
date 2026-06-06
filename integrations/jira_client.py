"""Jira REST API integration for Anton."""

import os
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class JiraTicket:
    key: str           # e.g. "BUG-42"
    summary: str
    description: str
    priority: str      # Critical / High / Medium / Low
    status: str
    reporter: str
    labels: list[str]
    components: list[str]
    project_key: str


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = (email, api_token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/rest/api/3/{path.lstrip('/')}"

    async def get_ticket(self, issue_key: str) -> JiraTicket:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._url(f"issue/{issue_key}"),
                auth=self.auth,
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        fields = data["fields"]
        description_text = ""
        if fields.get("description"):
            desc = fields["description"]
            if isinstance(desc, dict):
                # Atlassian Document Format → plain text
                for block in desc.get("content", []):
                    for inline in block.get("content", []):
                        description_text += inline.get("text", "")
                    description_text += "\n"
            else:
                description_text = str(desc)

        return JiraTicket(
            key=data["key"],
            summary=fields.get("summary", ""),
            description=description_text.strip(),
            priority=fields.get("priority", {}).get("name", "Medium"),
            status=fields.get("status", {}).get("name", "Open"),
            reporter=fields.get("reporter", {}).get("displayName", "Unknown"),
            labels=fields.get("labels", []),
            components=[c["name"] for c in fields.get("components", [])],
            project_key=data["key"].split("-")[0],
        )

    async def update_status(self, issue_key: str, transition_name: str) -> None:
        """Move a ticket to a new status by transition name (e.g. 'Done')."""
        async with httpx.AsyncClient() as client:
            # 1. fetch available transitions
            resp = await client.get(
                self._url(f"issue/{issue_key}/transitions"),
                auth=self.auth,
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            transitions = resp.json().get("transitions", [])
            target = next((t for t in transitions if t["name"].lower() == transition_name.lower()), None)
            if not target:
                available = [t["name"] for t in transitions]
                raise ValueError(f"Transition '{transition_name}' not found. Available: {available}")

            # 2. perform the transition
            resp = await client.post(
                self._url(f"issue/{issue_key}/transitions"),
                auth=self.auth,
                headers=self.headers,
                json={"transition": {"id": target["id"]}},
                timeout=10,
            )
            resp.raise_for_status()
        logger.info("Jira %s → %s", issue_key, transition_name)

    async def add_comment(self, issue_key: str, body: str) -> None:
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": body}]}
                ],
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._url(f"issue/{issue_key}/comment"),
                auth=self.auth,
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
        logger.info("Comment added to Jira %s", issue_key)

    async def link_pr(self, issue_key: str, pr_url: str, pr_title: str) -> None:
        comment = f"✅ Anton resolved this ticket.\n\nPull Request: [{pr_title}]({pr_url})"
        await self.add_comment(issue_key, comment)
        await self.update_status(issue_key, "Done")


# ── Mock client for demo (no Jira account needed) ────────────────────────────

class MockJiraClient:
    """Drop-in replacement that logs instead of hitting the API."""

    async def get_ticket(self, issue_key: str) -> JiraTicket:
        logger.info("[MOCK] Fetching Jira ticket %s", issue_key)
        return JiraTicket(
            key=issue_key,
            summary="Completing a todo un-completes it on second call",
            description=(
                "BUG REPORT — P1 Critical\n\n"
                "When a user marks a todo as complete, it works the first time. "
                "But calling complete() a second time on the same todo reverts it back to incomplete.\n\n"
                "Root cause appears to be in todos/service.py — the complete() method uses "
                "`not todo.completed` (toggle) instead of always setting `todo.completed = True`.\n\n"
                "Steps to reproduce:\n"
                "1. Create a todo\n"
                "2. Mark it complete (POST /todos/1/complete)\n"
                "3. Mark it complete again\n"
                "4. Observe: todo.completed is now False\n\n"
                "CI failing: test_complete_is_idempotent\n"
                "Affected file: todos/service.py"
            ),
            priority="Critical",
            status="In Progress",
            reporter="Alex Chen",
            labels=["bug", "p1", "todo-service"],
            components=["todo-service"],
            project_key="BUG",
        )

    async def update_status(self, issue_key: str, transition_name: str) -> None:
        logger.info("[MOCK] Jira %s → %s", issue_key, transition_name)

    async def add_comment(self, issue_key: str, body: str) -> None:
        logger.info("[MOCK] Comment on %s:\n%s", issue_key, body)

    async def link_pr(self, issue_key: str, pr_url: str, pr_title: str) -> None:
        logger.info("[MOCK] Linking PR '%s' to %s and closing ticket", pr_title, issue_key)


def get_jira_client():
    """Return real or mock client based on env vars."""
    url = os.getenv("JIRA_BASE_URL")
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if url and email and token:
        return JiraClient(url, email, token)
    logger.warning("Jira credentials not set — using mock client")
    return MockJiraClient()
