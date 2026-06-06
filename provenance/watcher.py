"""
Autonomous CI watcher (local-CI mode).

This is the REAL autonomy: no human types a command. The watcher polls the
target repo's test status; when a commit's `cargo test` fails, it fires the
pipeline itself. A real breaking commit landing is an external event Anton
observed — not an instruction we issued.

Deliberately cheap (the brief: build it real, build it fast). No queue, no
dashboard, one source. `cargo test` failing on HEAD is enough.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional


def head_commit(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def ci_failing(repo: Path) -> tuple[bool, str]:
    """Run the project's CI check (cargo test) on the current HEAD."""
    res = subprocess.run(["cargo", "test", "--quiet"], cwd=str(repo),
                         capture_output=True, text=True, timeout=300)
    return res.returncode != 0, (res.stdout + res.stderr)


class CIWatcher:
    def __init__(self, repo_path: str, poll_interval: float = 5.0):
        self.repo = Path(repo_path).resolve()
        self.poll_interval = poll_interval
        self._handled: set[str] = set()

    async def watch_once(self, on_failure: Callable[[str, str], Awaitable[None]]) -> bool:
        """Check HEAD once; if CI is failing on an unseen commit, fire. Returns True if fired."""
        commit = head_commit(self.repo)
        if commit in self._handled:
            return False
        failing, log = ci_failing(self.repo)
        if failing:
            print(f"\n🛰️  WATCHER: cargo test FAILED on commit {commit[:10]} — "
                  f"no human triggered this. Dispatching Anton.\n")
            self._handled.add(commit)
            await on_failure(commit, log)
            return True
        return False

    async def watch_forever(self, on_failure: Callable[[str, str], Awaitable[None]],
                            max_polls: Optional[int] = None) -> None:
        polls = 0
        while max_polls is None or polls < max_polls:
            fired = await self.watch_once(on_failure)
            if fired:
                return
            polls += 1
            time.sleep(self.poll_interval)
