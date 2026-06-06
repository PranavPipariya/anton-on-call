"""
The rigged lie-detection path (demo beat 3b).

Takes an HONEST receipt and produces tampered/forged variants that look green
to the naked eye but that the standalone verifier rejects. This is where we
prove the system refuses to be fooled.

Two independent attacks, each defeating a different wall:

  1. tampered-diff  : an honest GREEN receipt whose passing diff is altered after
                      sealing — "the code shipped is not the code that was tested".
                      -> verifier recomputes diff hash, mismatch, REJECT.

  2. forged-claim   : a run that actually FAILED, relabeled to PASS/exit 0 with a
                      faked "all tests passed" output — "the agent claims green".
                      -> verifier recomputes entry hash + checks signature, REJECT.

In neither case can the attacker re-sign: the runner's private key is not theirs.
That is the point — anyone can paint a green check; nobody can forge the signature.

    python -m provenance.rig honest.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def tampered_diff(receipt: dict) -> dict:
    """Alter the diff of the final PASS entry after it was sealed."""
    r = json.loads(json.dumps(receipt))  # deep copy
    # Find the last passing entry and corrupt one character of its diff.
    for e in reversed(r["entries"]):
        if e["verdict"] == "PASS":
            # Swap the real fix for a different value — the code "shipped" now
            # differs from the code that was actually tested and signed.
            e["diff"] = e["diff"].replace(".min(n_buckets - 1)", ".min(n_buckets + 1)")
            if e["diff"] == receipt["entries"][e["index"]]["diff"]:
                # Fallback: flip a byte so the hash always changes.
                e["diff"] = e["diff"] + "\n# tampered\n"
            break
    r["_attack"] = "tampered-diff: passing diff altered after sealing"
    return r


def forged_claim(receipt: dict) -> dict:
    """Relabel a FAILED run as a green pass with faked output (no re-signing)."""
    r = json.loads(json.dumps(receipt))
    fake_output = (
        "running 4 tests\n"
        "test min_value_lands_in_first_bucket ... ok\n"
        "test midpoint_lands_in_middle_bucket ... ok\n"
        "test max_value_lands_in_last_bucket ... ok\n"
        "test histogram_never_indexes_out_of_range ... ok\n\n"
        "test result: ok. 4 passed; 0 failed; 0 ignored\n"
    )
    target = None
    for e in r["entries"]:
        if e["verdict"] == "FAIL":
            target = e
            break
    if target is None:
        target = r["entries"][0]
    # The forger claims a pass without the signing key.
    target["verdict"] = "PASS"
    target["exit_code"] = 0
    target["test_output"] = fake_output
    target["test_output_sha256"] = __import__("hashlib").sha256(fake_output.encode()).hexdigest()
    r["entries"] = [target]
    r["final_verdict"] = "PASS"
    r["_attack"] = "forged-claim: failed run relabeled PASS with faked output"
    return r


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/receipt_honest.json")
    receipt = json.loads(src.read_text())

    out_dir = src.parent
    t = out_dir / "receipt_tampered.json"
    f = out_dir / "receipt_forged.json"
    t.write_text(json.dumps(tampered_diff(receipt), indent=2))
    f.write_text(json.dumps(forged_claim(receipt), indent=2))
    print(f"wrote {t}")
    print(f"wrote {f}")


if __name__ == "__main__":
    main()
