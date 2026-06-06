"""
CI/CD monitor for Anton.

Watches GitHub Actions runs and retrieves failure logs for the Code Agent
to analyse and fix.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class CIFailureReport:
    run_id: int
    branch: str
    failed_jobs: list[str]
    log_snippet: str       # The most relevant failure output
    html_url: str


class GitHubActionsMonitor:
    """Poll a GitHub Actions run until it completes, then extract failure logs."""

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        self.base = "https://api.github.com"

    async def wait_for_run(self, run_id: int, timeout: int = 300, poll_interval: int = 10) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                resp = await client.get(
                    f"{self.base}/repos/{self.repo}/actions/runs/{run_id}",
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                if data["status"] == "completed":
                    return data
                await asyncio.sleep(poll_interval)
        raise TimeoutError(f"CI run {run_id} did not complete within {timeout}s")

    async def get_failure_report(self, run_id: int) -> CIFailureReport:
        async with httpx.AsyncClient() as client:
            # Fetch jobs
            resp = await client.get(
                f"{self.base}/repos/{self.repo}/actions/runs/{run_id}/jobs",
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])

        failed_jobs = [j["name"] for j in jobs if j.get("conclusion") == "failure"]

        # Try to get the log zip (GitHub returns a redirect URL)
        log_snippet = await self._get_log_snippet(run_id, failed_jobs)

        run_url = f"https://github.com/{self.repo}/actions/runs/{run_id}"
        return CIFailureReport(
            run_id=run_id,
            branch="",
            failed_jobs=failed_jobs,
            log_snippet=log_snippet,
            html_url=run_url,
        )

    async def _get_log_snippet(self, run_id: int, failed_jobs: list[str]) -> str:
        """Best-effort: return a short failure summary."""
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(
                    f"{self.base}/repos/{self.repo}/actions/runs/{run_id}/logs",
                    headers=self.headers,
                    timeout=15,
                )
                if resp.status_code == 200:
                    # Logs come back as a zip; we just note we have them
                    return f"[Log download available — {len(resp.content)} bytes]"
        except Exception as e:
            logger.debug("Log fetch skipped: %s", e)

        return f"Failed jobs: {', '.join(failed_jobs)}" if failed_jobs else "No failure details available."


class MockCIMonitor:
    """Returns pre-canned failure report from the demo log file."""

    def __init__(self):
        log_path = os.path.join(os.path.dirname(__file__), "../demo/sample_repo/ci_failure.log")
        try:
            with open(log_path) as f:
                self._log = f.read()
        except FileNotFoundError:
            self._log = "CI run failed — 3 tests failed in tests/test_calculator.py"

    async def wait_for_run(self, run_id: int, timeout: int = 300, poll_interval: int = 10) -> dict:
        logger.info("[MOCK] CI run %d watching...", run_id)
        await asyncio.sleep(1)
        return {"status": "completed", "conclusion": "failure", "id": run_id}

    async def get_failure_report(self, run_id: int) -> CIFailureReport:
        logger.info("[MOCK] Fetching CI failure report for run %d", run_id)
        return CIFailureReport(
            run_id=run_id,
            branch="main",
            failed_jobs=["test (3.11)"],
            log_snippet=self._log,
            html_url=f"https://github.com/demo-org/storefront/actions/runs/{run_id}",
        )


def get_ci_monitor():
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    if token and repo:
        return GitHubActionsMonitor(token, repo)
    logger.warning("GitHub credentials not set — using mock CI monitor")
    return MockCIMonitor()
