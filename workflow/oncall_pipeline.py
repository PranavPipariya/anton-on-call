"""
Anton — Core parallel workflow pipeline.

Flow:
  1. Ingest Jira ticket
  2. Spin up 4 subagents concurrently (triage / code / test / PR description)
  3. Collect results → generate 3 documents
  4. Post Slack briefing with Approve / Request Changes buttons
  5. On approval: commit fix, open GitHub PR, close Jira ticket
  6. On rejection: route feedback back to Code Agent for revision
"""

import asyncio
import logging
import re
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

from agent.orchestrator import Agent
from agent.event_types import AgentEventType
from config.configuration import Config, ModelConfig
from tools.specialized_agents import SubagentTool

from agents.definitions import TRIAGE_AGENT, CODE_AGENT, TEST_AGENT, PR_AGENT
from integrations.jira_client import JiraTicket
from integrations.github_client import get_github_client, PRResult
from integrations.cicd_monitor import get_ci_monitor, CIFailureReport
from integrations.slack_bot import (
    get_slack_notifier,
    build_briefing_blocks,
    build_approved_blocks,
    build_rejected_blocks,
)
from documents.doc_generator import DocumentGenerator

logger = logging.getLogger(__name__)


# ── Result containers ─────────────────────────────────────────────────────────

@dataclass
class TriageResult:
    priority: str = "P2 High"
    component: str = "unknown"
    affected_files: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class CodeResult:
    changed_files: list[str] = field(default_factory=list)
    fix_explanation: str = ""
    file_contents: dict[str, str] = field(default_factory=dict)
    branch: str = ""
    raw: str = ""


@dataclass
class TestResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    new_tests: list[str] = field(default_factory=list)
    test_file_path: str = ""
    test_file_content: str = ""
    all_passing: bool = False
    raw: str = ""


@dataclass
class PipelineRun:
    run_id: str
    ticket: JiraTicket
    triage: Optional[TriageResult] = None
    code: Optional[CodeResult] = None
    test: Optional[TestResult] = None
    pr_description: str = ""
    ci: Optional[CIFailureReport] = None
    pr: Optional[PRResult] = None
    slack_ts: str = ""
    approved: Optional[bool] = None
    rejection_feedback: str = ""


# ── Subagent runner ───────────────────────────────────────────────────────────

async def _run_subagent(definition, goal: str, cwd: str) -> str:
    """Run a single subagent and return its final text response."""
    from pathlib import Path
    config = Config(
        cwd=cwd,
        model=ModelConfig(name=os.getenv("MODEL", "anthropic/claude-3.5-sonnet")),
    )
    tool = SubagentTool(config, definition)
    from tools.tool_interface import ToolInvocation
    invocation = ToolInvocation(
        params={"goal": goal},
        cwd=Path(cwd),
    )
    result = await tool.execute(invocation)
    return result.output or ""


# ── Result parsers ────────────────────────────────────────────────────────────

def _parse_triage(raw: str) -> TriageResult:
    result = TriageResult(raw=raw)
    import json, re

    # Try to extract JSON block
    match = re.search(r'\{[\s\S]*?\}', raw)
    if match:
        try:
            data = json.loads(match.group())
            result.priority = data.get("priority", "P2 High")
            result.component = data.get("component", "unknown")
            result.affected_files = data.get("affected_files_hint", [])
            result.acceptance_criteria = data.get("acceptance_criteria", [])
            return result
        except json.JSONDecodeError:
            pass

    # Fallback: regex scraping
    if "P1" in raw or "Critical" in raw.lower():
        result.priority = "P1 Critical"
    elif "P2" in raw or "High" in raw.lower():
        result.priority = "P2 High"

    comp_match = re.search(r'component["\s:]+([a-zA-Z0-9_-]+)', raw, re.IGNORECASE)
    if comp_match:
        result.component = comp_match.group(1)

    return result


def _parse_code(raw: str) -> CodeResult:
    result = CodeResult(raw=raw)

    # Extract changed files
    files_match = re.search(r'CHANGED_FILES:\s*((?:- .+\n?)+)', raw, re.IGNORECASE)
    if files_match:
        result.changed_files = [
            line.strip().lstrip("- ").strip()
            for line in files_match.group(1).strip().splitlines()
        ]

    # Extract fix explanation
    expl_match = re.search(r'FIX_EXPLANATION:\s*(.+?)(?:\n\n|FILE:|$)', raw, re.IGNORECASE | re.DOTALL)
    if expl_match:
        result.fix_explanation = expl_match.group(1).strip()

    # Extract file contents from fenced code blocks
    file_blocks = re.findall(r'FILE:\s*(.+?)\n```(?:python)?\n([\s\S]+?)```', raw)
    for path, content in file_blocks:
        result.file_contents[path.strip()] = content

    return result


def _parse_test(raw: str) -> TestResult:
    result = TestResult(raw=raw)

    # Total / Passed / Failed
    counts = re.search(r'Total:\s*(\d+)\s+Passed:\s*(\d+)\s+Failed:\s*(\d+)', raw, re.IGNORECASE)
    if counts:
        result.total = int(counts.group(1))
        result.passed = int(counts.group(2))
        result.failed = int(counts.group(3))
    else:
        # Try pytest summary line
        pytest_match = re.search(r'(\d+) passed', raw)
        if pytest_match:
            result.passed = int(pytest_match.group(1))
            result.total = result.passed

    result.all_passing = result.failed == 0 and result.passed > 0

    # New test names
    new_tests_match = re.search(r'NEW_TESTS_ADDED:\s*((?:- .+\n?)+)', raw, re.IGNORECASE)
    if new_tests_match:
        result.new_tests = [
            line.strip().lstrip("- ").strip()
            for line in new_tests_match.group(1).strip().splitlines()
        ]

    # Test file content
    file_blocks = re.findall(r'FILE:\s*(tests/.+?)\n```(?:python)?\n([\s\S]+?)```', raw)
    if file_blocks:
        result.test_file_path, result.test_file_content = file_blocks[0]
        result.test_file_path = result.test_file_path.strip()

    if "ALL TESTS PASSING" in raw.upper():
        result.all_passing = True

    return result


# ── Main pipeline ─────────────────────────────────────────────────────────────

class OnCallPipeline:
    def __init__(self, repo_cwd: str):
        self.repo_cwd = repo_cwd
        self.github = get_github_client()
        self.ci_monitor = get_ci_monitor()
        self.slack = get_slack_notifier()
        self.doc_gen = DocumentGenerator()

    async def run(self, ticket: JiraTicket) -> PipelineRun:
        run_id = str(uuid.uuid4())[:8]
        pipeline = PipelineRun(run_id=run_id, ticket=ticket)

        logger.info("━━━ Anton ▸ run %s ▸ %s ━━━", run_id, ticket.key)

        # ── Step 1: Ingest CI failure (if available) ──────────────────────────
        ci_report = await self._ingest_ci()
        pipeline.ci = ci_report

        # ── Step 2: Launch all 4 agents in parallel ───────────────────────────
        print(f"\n🚀  [{ticket.key}] Launching 4 agents in parallel...\n")

        triage_goal = f"Ticket: {ticket.key}\nSummary: {ticket.summary}\nDescription:\n{ticket.description}"
        code_goal = (
            f"Repository CWD: {self.repo_cwd}\n"
            f"Ticket: {ticket.key}\n"
            f"Summary: {ticket.summary}\n"
            f"Description:\n{ticket.description}\n\n"
            + (f"CI Failure Logs:\n{ci_report.log_snippet}" if ci_report else "")
        )
        test_goal = (
            f"Repository CWD: {self.repo_cwd}\n"
            f"Ticket: {ticket.key}\n"
            f"The fix is in: {ticket.description}\n"
            "Generate tests, run the suite, confirm all pass."
        )
        pr_goal = (
            f"Ticket: {ticket.key} — {ticket.summary}\n"
            f"Priority: {ticket.priority}\n"
            f"Description:\n{ticket.description}"
        )

        triage_raw, code_raw, test_raw, pr_raw = await asyncio.gather(
            _run_subagent(TRIAGE_AGENT, triage_goal, self.repo_cwd),
            _run_subagent(CODE_AGENT, code_goal, self.repo_cwd),
            _run_subagent(TEST_AGENT, test_goal, self.repo_cwd),
            _run_subagent(PR_AGENT, pr_goal, self.repo_cwd),
            return_exceptions=False,
        )

        pipeline.triage = _parse_triage(triage_raw)
        pipeline.code = _parse_code(code_raw)
        pipeline.test = _parse_test(test_raw)
        pipeline.pr_description = pr_raw

        print(f"✅  All agents completed\n")
        self._log_agent_summary(pipeline)

        # ── Step 3: Commit fix to a new branch ───────────────────────────────
        branch = f"fix/{ticket.key.lower()}-{run_id}"
        pipeline.code.branch = branch
        await self._commit_fix(pipeline)

        # ── Step 4: Generate documents ────────────────────────────────────────
        docs = self.doc_gen.generate_all(pipeline)
        logger.info("Documents generated: %s", list(docs.keys()))

        # ── Step 5: Post Slack briefing ───────────────────────────────────────
        blocks = build_briefing_blocks(
            ticket_key=ticket.key,
            summary=ticket.summary,
            priority=pipeline.triage.priority,
            component=pipeline.triage.component or (ticket.components[0] if ticket.components else "unknown"),
            root_cause=_extract_root_cause(pipeline.code.fix_explanation),
            fix_summary=pipeline.code.fix_explanation or "Fix applied — see PR for details",
            files_changed=pipeline.code.changed_files,
            tests_added=len(pipeline.test.new_tests),
            tests_total=pipeline.test.total,
            ci_green=pipeline.test.all_passing,
            branch=branch,
            run_id=run_id,
        )
        pipeline.slack_ts = await self.slack.post_briefing(blocks)

        return pipeline

    async def approve(self, pipeline: PipelineRun) -> PRResult:
        """Called when the human taps Approve in Slack."""
        print(f"\n✅  Approved — opening PR for {pipeline.ticket.key}")

        pr = self.github.create_pull_request(
            title=f"[{pipeline.ticket.key}] {pipeline.ticket.summary}",
            body=pipeline.pr_description,
            head=pipeline.code.branch,
            base="main",
            labels=["bug", "anton"],
        )
        pipeline.pr = pr
        pipeline.approved = True

        # Update Slack message
        await self.slack.update_message(
            pipeline.slack_ts,
            build_approved_blocks(pipeline.ticket.key, pr.url, pr.title),
            text=f"PR opened: {pr.url}",
        )

        logger.info("PR created: %s", pr.url)
        return pr

    async def request_changes(self, pipeline: PipelineRun, feedback: str) -> str:
        """Called when the human taps Request Changes in Slack."""
        pipeline.approved = False
        pipeline.rejection_feedback = feedback

        await self.slack.update_message(
            pipeline.slack_ts,
            build_rejected_blocks(pipeline.ticket.key, feedback),
            text="Changes requested — routing back to Code Agent",
        )

        # Re-run code agent with feedback
        revised_goal = (
            f"PREVIOUS ATTEMPT WAS REJECTED.\n"
            f"Human reviewer feedback: {feedback}\n\n"
            f"Ticket: {pipeline.ticket.key}\n"
            f"Summary: {pipeline.ticket.summary}\n"
            f"Description:\n{pipeline.ticket.description}\n\n"
            "Please revise the fix addressing the reviewer's concerns."
        )
        revised_raw = await _run_subagent(CODE_AGENT, revised_goal, self.repo_cwd)
        pipeline.code = _parse_code(revised_raw)
        logger.info("Code Agent revised fix for %s", pipeline.ticket.key)
        return revised_raw

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _ingest_ci(self) -> Optional[CIFailureReport]:
        try:
            return await self.ci_monitor.get_failure_report(0)
        except Exception as e:
            logger.warning("CI ingestion skipped: %s", e)
            return None

    async def _commit_fix(self, pipeline: PipelineRun) -> None:
        try:
            self.github.create_branch(pipeline.code.branch)
            for path, content in pipeline.code.file_contents.items():
                self.github.commit_file(
                    path,
                    content,
                    f"[{pipeline.ticket.key}] {pipeline.code.fix_explanation or 'Apply fix'}",
                    pipeline.code.branch,
                )
            if pipeline.test.test_file_content and pipeline.test.test_file_path:
                self.github.commit_file(
                    pipeline.test.test_file_path,
                    pipeline.test.test_file_content,
                    f"[{pipeline.ticket.key}] Add regression tests",
                    pipeline.code.branch,
                )
        except Exception as e:
            logger.error("Commit failed (continuing without commit): %s", e)

    def _log_agent_summary(self, pipeline: PipelineRun) -> None:
        t = pipeline.triage
        c = pipeline.code
        te = pipeline.test
        print(f"  🔍 Triage  : {t.priority} | {t.component}")
        print(f"  🔧 Code    : {', '.join(c.changed_files) or 'no files parsed'}")
        print(f"  🧪 Tests   : {te.passed}/{te.total} passing | {'✅ all green' if te.all_passing else '❌ failures'}")
        print(f"  📝 PR desc : {len(pipeline.pr_description)} chars\n")


def _extract_root_cause(fix_explanation: str) -> str:
    if not fix_explanation:
        return "See fix explanation in PR description."
    # Keep it to one sentence
    sentences = fix_explanation.split(".")
    return sentences[0].strip() + "." if sentences else fix_explanation
