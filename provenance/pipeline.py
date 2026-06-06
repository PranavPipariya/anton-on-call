"""
Provenance pipeline — the demo's real path.

This deliberately bypasses Anton's old `_parse_test` hole (where the agent's text
was scraped for "ALL TESTS PASSING"). Here the verdict shown to the human and
attached to the PR is the RUNNER's signed verdict — never the agent's claim.

Flow:
  1. Fix-loop produces a real diff (frozen replay or live LLM).
  2. The keyed runner runs cargo test and signs a hash-chained receipt.
  3. Slack briefing shows the RUNNER verdict + receipt fingerprint + verify command.
  4. On approval: open a real PR (if creds) with the receipt attached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from provenance.fix_loop import FixLoop, FixOutcome


# ── Slack Block Kit: the provenance briefing ──────────────────────────────────

def build_provenance_blocks(*, summary: str, commit: str, fix_summary: str,
                            entries: list, receipt_path: str, run_id: str) -> list[dict]:
    last = entries[-1]
    passed = last.verdict == "PASS"
    verdict_line = "✅ PASS" if passed else "❌ FAIL"
    chain_line = "  →  ".join(
        f"{'HEAD' if e.attempt == 0 else 'fix'}: {e.verdict} (exit {e.exit_code})"
        for e in entries
    )
    attempts_line = f"{len(entries)} signed, hash-chained entries"
    return [
        {"type": "header", "text": {"type": "plain_text",
         "text": "🚨  Anton — Autonomous Incident Response", "emoji": True}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*Bug*\n{summary}\n\n*Detected on commit* `{commit[:10]}` — no human triggered this."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Fix*\n{fix_summary}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text":
         f"*Test verdict (authored by the RUNNER, not the agent)*\n"
         f"{verdict_line}  ·  exit code `{last.exit_code}`  ·  {attempts_line}\n"
         f"`{chain_line}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
         f"*Execution-integrity receipt*\n"
         f"`{Path(receipt_path).name}`  ·  signed entry `{last.entry_hash[:16]}…`\n"
         f"Anyone can verify independently:  `python verify.py {Path(receipt_path).name}`"
         + ("\n⚠ this diff modified test code — review test quality." if last.diff_touches_tests else "")}},
        {"type": "divider"},
        {"type": "actions", "block_id": f"hitl_{run_id}", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅  Approve & Open PR", "emoji": True},
             "style": "primary", "value": f"approve|{run_id}", "action_id": "hitl_approve"},
            {"type": "button", "text": {"type": "plain_text", "text": "✏️  Request Changes", "emoji": True},
             "style": "danger", "value": f"reject|{run_id}", "action_id": "hitl_reject"},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "The verdict above was signed by a runner the agent cannot reach. "
                 "Approve to open a PR with the receipt attached for independent verification."}]},
    ]


# ── run container ─────────────────────────────────────────────────────────────

@dataclass
class ProvRun:
    run_id: str
    summary: str
    outcome: FixOutcome
    receipt_path: str
    commit: str
    slack_ts: str = ""
    pr_url: str = ""
    pr_number: int = 0
    merged: bool = False


class ProvenancePipeline:
    def __init__(self, repo_path: str, crate: str = "histogram", target_file: str = "src/lib.rs"):
        self.repo_path = Path(repo_path).resolve()
        self.crate = crate
        self.target_file = target_file

    async def run(self, *, summary: str, bug_report: str, run_id: str,
                  fix_summary: str = "Clamp the bucket index to the last valid bucket.") -> ProvRun:
        from integrations.slack_bot import get_slack_notifier

        fl = FixLoop(str(self.repo_path), target_file=self.target_file, crate=self.crate)

        # Entry 0: prove the bug is real — cargo test on the untouched HEAD (FAIL).
        base = fl.runner.run_baseline()
        print(f"  [runner] baseline HEAD: exit={base.exit_code} -> {base.verdict}")

        # Entry 1+: the live agent authors a real fix; runner tests + signs it.
        outcome = await fl.run_live(bug_report)

        # Receipt lives in the target repo so it can be committed/attached to the PR.
        receipt_path = self.repo_path / "receipt.json"
        fl.runner.write_receipt(receipt_path)

        run = ProvRun(
            run_id=run_id, summary=summary, outcome=outcome,
            receipt_path=str(receipt_path), commit=fl.runner.base_commit,
        )

        slack = get_slack_notifier()
        blocks = build_provenance_blocks(
            summary=summary, commit=run.commit, fix_summary=fix_summary,
            entries=fl.runner.entries, receipt_path=str(receipt_path), run_id=run_id,
        )
        run.slack_ts = await slack.post_briefing(blocks)
        return run

    async def approve(self, run: ProvRun, *, auto: bool = False, merge: bool = False) -> str:
        """Open a real PR with the receipt attached (mock if no GitHub creds).

        auto=True  -> approval came from Anton itself (full-autonomous mode), not a human.
        merge=True -> also merge the PR (only used in full-autonomous mode).
        """
        from integrations.github_client import get_github_client
        from integrations.slack_bot import get_slack_notifier, build_approved_blocks

        gh = get_github_client()
        branch = f"anton/fix-{run.run_id}"
        last = run.outcome.results[-1].entry
        approver = "Anton (auto-approved)" if auto else "human reviewer"
        body = (
            f"## Automated fix by Anton\n\n{run.summary}\n\n"
            f"Approved by: **{approver}**\n\n"
            f"### Execution-integrity receipt\n"
            f"- Verdict: **{last.verdict}** (exit {last.exit_code}) — signed by the runner, not the agent\n"
            f"- Signed entry hash: `{last.entry_hash}`\n"
            f"- Base commit: `{run.commit}`\n\n"
            f"Verify independently:\n```\npython verify.py receipt.json\n```\n"
        )
        try:
            gh.create_branch(branch)
            # commit the actual fix so the PR is mergeable
            if run.outcome.passed and run.outcome.final_file:
                gh.commit_file(run.outcome.target_file, run.outcome.final_file,
                               f"[anton] fix: {run.summary}", branch)
            # attach the execution-integrity receipt to the PR branch
            gh.commit_file("receipt.json", Path(run.receipt_path).read_text(),
                           f"[anton] execution-integrity receipt ({run.run_id})", branch)
            pr = gh.create_pull_request(title=f"[anton] {run.summary}", body=body,
                                        head=branch, base="main", labels=["anton"])
            run.pr_url = pr.url
            run.pr_number = pr.number
            if merge and run.outcome.passed:
                gh.merge_pull_request(pr.number)
                run.merged = True
        except Exception as e:
            run.pr_url = f"(PR step skipped: {e})"

        status = "Merged ✅" if run.merged else "PR opened"
        slack = get_slack_notifier()
        await slack.update_message(
            run.slack_ts,
            build_approved_blocks("BUG", run.pr_url, f"{run.summary} — {approver}"),
            text=f"{status}: {run.pr_url}",
        )
        return run.pr_url
