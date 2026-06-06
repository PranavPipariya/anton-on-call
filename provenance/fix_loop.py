"""
The fix-loop — deliberately the dumb part.

This is scaffold, not art. Its only job is to produce a REAL diff that goes to
the keyed runner. The agent here is locked down to make the trust boundary real:

  - It can READ files, but only inside the crate (jailed; no absolute paths, no
    `..` escape). It therefore cannot read the runner's signing key.
  - It has NO shell, NO write tool, NO test-execution tool. It cannot run cargo
    and cannot author a verdict.
  - Its single output is the full text of a fixed file. We compute the unified
    diff ourselves and hand it to the runner. The agent's only causal influence
    on the world is that diff.

Test results only ever reach the agent because the RUNNER hands them back. The
agent reads the runner's failure output and revises. It never observes a test
run it controls.

Two modes:
  - live   : a real LLM authors the fix (needs API_KEY/BASE_URL in env).
  - frozen : replay pre-captured agent outputs through the REAL runner, so the
             cargo run and signing are still 100% live. Used for deterministic
             stage rehearsal (the claim is execution integrity, not LLM luck).
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from provenance.runner import Runner, RunResult


# ── jailed, read-only view of the crate (the agent's entire world) ────────────

class JailedRepo:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"jail violation: {rel} resolves outside the crate")
        return p

    def head_file(self, rel: str) -> str:
        """Read a file as it exists at HEAD (the clean baseline the runner tests)."""
        self._safe(rel)  # jail check on the requested path
        out = subprocess.run(
            ["git", "-C", str(self.root), "show", f"HEAD:{rel}"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise FileNotFoundError(rel)
        return out.stdout

    def list_source(self) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.root), "ls-files"],
            capture_output=True, text=True,
        )
        return [f for f in out.stdout.splitlines() if f.endswith(".rs") or f.endswith(".toml")]


# ── diff construction (we own this, not the agent) ────────────────────────────

def make_diff(rel_path: str, old: str, new: str) -> str:
    """Unified diff the runner's `git apply -p1` will accept."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="\n",
    )
    return "".join(diff)


_FENCE = re.compile(r"```(?:rust)?\s*\n(.*?)```", re.DOTALL)


def extract_file(response: str) -> str:
    """Pull the full file body out of the model's fenced answer."""
    m = _FENCE.search(response)
    if m:
        return m.group(1)
    return response.strip() + "\n"


# ── the loop ──────────────────────────────────────────────────────────────────

@dataclass
class FixOutcome:
    runner: Runner
    results: list[RunResult]
    passed: bool
    target_file: str = ""
    final_file: str = ""   # full content of the passing fix (for a real PR commit)


SYSTEM_PROMPT = (
    "You are a Rust bug-fix agent. You are given a bug report, the current source "
    "of one file, and (if this is a retry) the exact test failure from a runner you "
    "do not control. Return the COMPLETE corrected contents of the target file and "
    "nothing else, inside a single ```rust code fence. Make the minimal change that "
    "fixes the bug. Do not weaken or delete tests."
)


class FixLoop:
    def __init__(self, repo_path: str, target_file: str = "src/lib.rs", crate: str = "histogram"):
        self.repo = JailedRepo(repo_path)
        self.target_file = target_file
        self.runner = Runner(repo_path, crate=crate)

    # --- live LLM author ---
    def _model(self) -> str:
        return os.environ.get("MODEL") or "openai/gpt-4o-mini"

    def _client(self):
        from openai import AsyncOpenAI
        api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("API_KEY")
        base_url = os.environ.get("BASE_URL") or "https://openrouter.ai/api/v1"
        if not api_key:
            raise RuntimeError("No OPENROUTER_API_KEY / API_KEY in environment for live mode")
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def _author_fix(self, bug_report: str, prior_failure: str | None,
                          attempt: int = 1, first_pass_directive: str | None = None) -> str:
        client = self._client()
        source = self.repo.head_file(self.target_file)
        user = (
            f"BUG REPORT:\n{bug_report}\n\n"
            f"FILE `{self.target_file}` (return the COMPLETE corrected version):\n"
            f"```rust\n{source}```\n"
        )
        if attempt == 1 and first_pass_directive and not prior_failure:
            user += f"\n{first_pass_directive}\n"
        if prior_failure:
            user += (
                f"\nYOUR PREVIOUS FIX DID NOT PASS. A runner you do not control ran "
                f"`cargo test` and reported:\n```\n{prior_failure[:3500]}\n```\n"
                f"Read the failure and correct the ACTUAL behavior. Return the full file again."
            )
        try:
            resp = await client.chat.completions.create(
                model=self._model(),
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user}],
                temperature=0,
            )
        finally:
            await client.close()
        content = extract_file(resp.choices[0].message.content or "")
        if "fn bucket_for" not in content:
            raise RuntimeError("model output did not contain the target function")
        return content

    async def run_live(self, bug_report: str, max_attempts: int = 3,
                       first_pass_directive: str | None = None) -> FixOutcome:
        results: list[RunResult] = []
        prior_failure = None
        baseline = self.repo.head_file(self.target_file)
        for attempt in range(1, max_attempts + 1):
            print(f"  [agent] authoring attempt {attempt} via {self._model()} ...")
            new_content = await self._author_fix(bug_report, prior_failure,
                                                 attempt=attempt,
                                                 first_pass_directive=first_pass_directive)
            diff = make_diff(self.target_file, baseline, new_content)
            if not diff.strip():
                prior_failure = "Your output was identical to the original file; no change was made."
                print("  [agent] no change proposed; retrying")
                continue
            res = self.runner.run_attempt(diff, attempt=attempt)
            print(f"  [runner] attempt {attempt}: exit={res.exit_code} -> {res.verdict}")
            results.append(res)
            if res.verdict == "PASS":
                return FixOutcome(self.runner, results, True,
                                  target_file=self.target_file, final_file=new_content)
            prior_failure = res.output
        return FixOutcome(self.runner, results, False, target_file=self.target_file)

    # --- frozen replay (deterministic stage path; runner/cargo still live) ---
    def run_frozen(self, full_file_attempts: list[str]) -> FixOutcome:
        """Replay captured agent file-outputs through the REAL runner."""
        results: list[RunResult] = []
        baseline = self.repo.head_file(self.target_file)
        for attempt, new_content in enumerate(full_file_attempts, start=1):
            diff = make_diff(self.target_file, baseline, new_content)
            res = self.runner.run_attempt(diff, attempt=attempt)
            results.append(res)
            if res.verdict == "PASS":
                return FixOutcome(self.runner, results, True)
        return FixOutcome(self.runner, results, results and results[-1].verdict == "PASS")
