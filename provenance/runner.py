"""
The keyed runner — the trust boundary.

The agent (the LLM with tools) produces ONE thing: a unified diff. It has no
shell, no write tool outside the crate, and no test-execution tool. It cannot
run cargo and cannot author a verdict.

This runner — a separate process holding the Ed25519 private key the agent
cannot read — does ALL of the following itself:

  1. Builds a CLEAN checkout of the crate at HEAD (via `git archive`), so the
     agent's working tree cannot influence what gets tested.
  2. Applies the agent's diff to that clean checkout.
  3. Runs `cargo test`, capturing the real exit code + full output.
  4. Hashes the diff and the output ITSELF (never trusts an agent-supplied hash).
  5. Derives the verdict STRICTLY from the exit code (0 == PASS, else FAIL).
  6. Signs a hash-chained receipt entry with the runner's private key.

There is no parameter, no flag, no message the agent can send that makes step 5
return PASS without `cargo test` actually exiting 0 against that exact diff.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provenance import keys
from provenance.receipt import (
    GENESIS,
    Entry,
    compute_entry_hash,
    now_iso,
    sha256_hex,
    signed_core,
)

PATCH_APPLY_FAILED_EXIT = 99  # runner sentinel: diff did not apply to clean HEAD


@dataclass
class RunResult:
    attempt: int
    exit_code: int
    verdict: str          # "PASS" | "FAIL"
    output: str           # full cargo test stdout+stderr (or apply error)
    entry: Entry


class Runner:
    """Owns the receipt chain and the signing key for one pipeline run."""

    def __init__(self, repo_path: str, crate: str | None = None, runner_id: str = "anton-runner"):
        self.repo_path = Path(repo_path).resolve()
        self.crate = crate or self.repo_path.name
        self.runner_id = runner_id
        self._key: Ed25519PrivateKey = keys.load_or_create_private_key()
        self.public_key_hex = keys.public_key_hex(self._key)
        self.base_commit = self._head_commit()
        self.entries: list[Entry] = []

    # ── git / checkout ──────────────────────────────────────────────────────

    def _head_commit(self) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        return out.stdout.strip() or "UNKNOWN"

    def _clean_checkout(self, dest: Path) -> None:
        """Materialize the crate at HEAD into `dest` — no working-tree leakage."""
        archive = subprocess.run(
            ["git", "-C", str(self.repo_path), "archive", "HEAD"],
            capture_output=True,
        )
        if archive.returncode != 0:
            raise RuntimeError(f"git archive failed: {archive.stderr.decode(errors='replace')}")
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)

    # ── the boundary ────────────────────────────────────────────────────────

    def run_baseline(self) -> RunResult:
        """Seal a signed entry for the UNMODIFIED HEAD — proof the bug is real.

        Runs `cargo test` on a clean checkout with no diff applied. On the buggy
        HEAD this is the FAIL that CI caught; it becomes entry 0 of the chain.
        """
        return self.run_attempt(diff="", attempt=0)

    def run_attempt(self, diff: str, attempt: int) -> RunResult:
        """Apply `diff` to a clean checkout, run the tests, sign the verdict."""
        with tempfile.TemporaryDirectory(prefix="anton-runner-") as tmp:
            work = Path(tmp) / "crate"
            self._clean_checkout(work)

            exit_code, output = self._apply_and_test(work, diff)

        verdict = "PASS" if exit_code == 0 else "FAIL"
        entry = self._seal(diff, output, exit_code, verdict, attempt)
        self.entries.append(entry)
        return RunResult(attempt=attempt, exit_code=exit_code, verdict=verdict, output=output, entry=entry)

    def _apply_and_test(self, work: Path, diff: str) -> tuple[int, str]:
        # Empty diff == baseline: test the unmodified HEAD as-is.
        if diff.strip():
            apply = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-p1", "-"],
                input=diff, text=True, cwd=str(work), capture_output=True,
            )
            if apply.returncode != 0:
                return PATCH_APPLY_FAILED_EXIT, (
                    "PATCH DID NOT APPLY to a clean checkout of HEAD.\n"
                    "--- git apply stderr ---\n" + apply.stderr
                )

        # Each checkout compiles its OWN source into its own ./target. No shared
        # cache — a stale cached binary could report a verdict that doesn't match
        # the source under test, which would defeat the entire point.
        env = os.environ.copy()
        env.pop("CARGO_TARGET_DIR", None)
        env["CARGO_TERM_COLOR"] = "never"
        test = subprocess.run(
            ["cargo", "test", "--quiet"],
            cwd=str(work), capture_output=True, text=True, env=env, timeout=300,
        )
        return test.returncode, (test.stdout + test.stderr)

    @staticmethod
    def _diff_touches_tests(diff: str) -> bool:
        """Flag (and sign) whether the agent's diff modified any test code.

        A green verdict on a diff that ALSO edits the tests proves only that the
        weakened tests passed — the receipt makes that visible instead of hiding
        it. This is the honest seam between execution integrity and correctness.
        """
        for line in diff.splitlines():
            if line.startswith(("+++ ", "--- ")):
                path = line[4:].strip()
                if path.startswith(("a/", "b/")):
                    path = path[2:]
                if "/tests/" in path or path.startswith("tests/") or path.endswith("_test.rs"):
                    return True
            # also catch added/removed #[test] or #[cfg(test)] inside src files
            if line.startswith(("+", "-")) and ("#[test]" in line or "#[cfg(test)]" in line):
                return True
        return False

    def _seal(self, diff: str, output: str, exit_code: int, verdict: str, attempt: int) -> Entry:
        """Build, hash-chain, and SIGN the entry. Runner-authored, agent-unreachable."""
        index = len(self.entries)
        prev_hash = self.entries[-1].entry_hash if self.entries else GENESIS
        timestamp = now_iso()
        diff_sha256 = sha256_hex(diff)
        output_sha256 = sha256_hex(output)
        touches_tests = self._diff_touches_tests(diff)

        core = signed_core(
            index=index,
            attempt=attempt,
            timestamp=timestamp,
            crate=self.crate,
            base_commit=self.base_commit,
            diff_sha256=diff_sha256,
            test_output_sha256=output_sha256,
            exit_code=exit_code,
            verdict=verdict,
            prev_hash=prev_hash,
            diff_touches_tests=touches_tests,
        )
        entry_hash = compute_entry_hash(core)
        signature = keys.sign(self._key, bytes.fromhex(entry_hash))

        return Entry(
            index=index,
            attempt=attempt,
            timestamp=timestamp,
            crate=self.crate,
            base_commit=self.base_commit,
            diff=diff,
            diff_sha256=diff_sha256,
            test_output=output,
            test_output_sha256=output_sha256,
            exit_code=exit_code,
            verdict=verdict,
            diff_touches_tests=touches_tests,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            signature=signature,
        )

    # ── output ──────────────────────────────────────────────────────────────

    def receipt_dict(self) -> dict:
        final = self.entries[-1].verdict if self.entries else "NONE"
        return {
            "version": "1",
            "crate": self.crate,
            "runner_id": self.runner_id,
            "base_commit": self.base_commit,
            "public_key": self.public_key_hex,  # informational; verify.py PINS its own
            "created_at": now_iso(),
            "final_verdict": final,
            "entries": [e.to_dict() for e in self.entries],
        }

    def write_receipt(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.receipt_dict(), indent=2))
        return path
