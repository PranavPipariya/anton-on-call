"""
End-to-end demo orchestrator. Run from the `anton/` dir:

    ./.venv/bin/python -m provenance.demo

Produces, deterministically:
  - ../receipt.json            (honest, GREEN)
  - ../receipt_tampered.json   (diff altered after sealing, RED)
  - ../receipt_forged.json     (failed run relabeled PASS, RED)

and walks beats 1-2 (autonomy + recovery) live through the REAL runner, then
prints the exact two verifier commands for beats 3a/3b.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]          # …/anton (repo root)
load_dotenv(ROOT / ".env")                          # OPENROUTER_API_KEY for the live agent

from provenance.pipeline import ProvenancePipeline, ProvRun
from provenance.watcher import CIWatcher
from provenance import rig

REPO = ROOT / "target_repo"
TARGET_FILE = "src/lib.rs"

SUMMARY = "histogram::bucket_for returns an out-of-range index for the maximum value"
BUG_REPORT = (
    "bucket_for(value, min, max, n_buckets) must return an index in [0, n_buckets). "
    "For value == max it returns n_buckets, which is out of range and panics the "
    "histogram collector. CI failing: max_value_lands_in_last_bucket, "
    "histogram_never_indexes_out_of_range."
)


async def main():
    print("=" * 70)
    print("  ANTON — EXECUTION-INTEGRITY PROVENANCE DEMO")
    print("=" * 70)

    pipeline = ProvenancePipeline(str(REPO), crate="histogram", target_file=TARGET_FILE)
    watcher = CIWatcher(str(REPO))
    held: dict[str, ProvRun] = {}

    # ── Beats 1 + 2: autonomy + recovery ─────────────────────────────────────
    async def on_failure(commit: str, log: str):
        run = await pipeline.run(
            summary=SUMMARY, bug_report=BUG_REPORT, run_id="demo01",
        )
        held["run"] = run

    fired = await watcher.watch_once(on_failure)
    if not fired:
        print("Watcher saw green CI — nothing to do (did you revert the bug?).")
        return

    run = held["run"]
    print("\nRUNNER VERDICT (not the agent's claim):",
          run.outcome.results[-1].verdict, "| attempts:", len(run.outcome.results))

    # ── Honest receipt -> repo root for the judge's verifier run ──────────────
    honest = ROOT / "receipt.json"
    shutil.copyfile(run.receipt_path, honest)

    # ── Beat 3b artifacts: the rigged receipts ───────────────────────────────
    receipt = json.loads(honest.read_text())
    (ROOT / "receipt_tampered.json").write_text(json.dumps(rig.tampered_diff(receipt), indent=2))
    (ROOT / "receipt_forged.json").write_text(json.dumps(rig.forged_claim(receipt), indent=2))

    # ── Beat 3a: approve -> PR with receipt attached ─────────────────────────
    pr_url = await pipeline.approve(run)
    print("\nPR:", pr_url)

    print("\n" + "=" * 70)
    print("  STAGE SCRIPT — run these live from", ROOT)
    print("=" * 70)
    print("  3a (judge runs, GREEN):   python verify.py receipt.json")
    print('  3b ("now watch me cheat", RED):')
    print("        python verify.py receipt_tampered.json")
    print("        python verify.py receipt_forged.json")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
