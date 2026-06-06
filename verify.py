#!/usr/bin/env python3
"""
verify.py — STANDALONE execution-integrity verifier.

    Usage:  python verify.py receipt.json

This script imports NOTHING from Anton. It trusts neither the agent that wrote
the code, nor the operator running the demo. It re-derives every hash from the
data embedded in the receipt, checks the SHA-256 hash chain, and verifies the
runner's Ed25519 signature against a PINNED public key hard-coded below.

What a green result proves:
    The tests in this receipt genuinely RAN and genuinely PASSED (cargo exit 0)
    against THIS EXACT diff, and the verdict was signed by the runner's key.

What it does NOT prove:
    That the tests are good, or that the bug is truly fixed. Nobody can prove
    correctness. This proves EXECUTION INTEGRITY — the floor everything else
    stands on.

Anyone can read this file top to bottom. There is no call back to Anton, no
network, no hidden trust. Only: recompute hashes, check the chain, check the
signature against the pinned key.
"""

import hashlib
import json
import shutil
import sys

# ── THE PINNED PUBLIC KEY ─────────────────────────────────────────────────────
# This is the runner's Ed25519 PUBLIC key. The matching PRIVATE key lives only
# inside the runner process; the agent has no tool to read it. A receipt whose
# signatures do not verify against THIS key is rejected — including any receipt
# that ships its own different public key. We ignore the receipt's "public_key"
# field on purpose; pinning is the whole point.
PINNED_PUBLIC_KEY_HEX = "8dff0afb62e4b6aacfed2d5a2260711dfa11c3f37bb3883207e6e784ad8775d1"

# Only third-party dependency: the same Ed25519 primitive any auditor would use.
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except Exception:  # pragma: no cover
    Ed25519PublicKey = None


# ── canonical hashing (mirrors provenance/receipt.py exactly) ─────────────────

def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(core):
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signed_core(e):
    """The exact subset of fields that the runner hashed and signed."""
    return {
        "index": e["index"],
        "attempt": e["attempt"],
        "timestamp": e["timestamp"],
        "crate": e["crate"],
        "base_commit": e["base_commit"],
        "diff_sha256": e["diff_sha256"],
        "test_output_sha256": e["test_output_sha256"],
        "exit_code": e["exit_code"],
        "verdict": e["verdict"],
        "prev_hash": e["prev_hash"],
        "diff_touches_tests": e["diff_touches_tests"],
    }


# ── billboard output ──────────────────────────────────────────────────────────

GREEN_BG = "\033[42m\033[30m"
RED_BG = "\033[41m\033[97m"
YELLOW_BG = "\033[43m\033[30m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"

# Chunky 5-row block font so the verdict is legible across a room.
_FONT = {
    "A": ["  ##  ", " #  # ", " #### ", " #  # ", " #  # "],
    "C": [" #### ", " #    ", " #    ", " #    ", " #### "],
    "D": [" ###  ", " #  # ", " #  # ", " #  # ", " ###  "],
    "E": [" #### ", " #    ", " ###  ", " #    ", " #### "],
    "F": [" #### ", " #    ", " ###  ", " #    ", " #    "],
    "I": [" ### ", "  #  ", "  #  ", "  #  ", " ### "],
    "L": [" #    ", " #    ", " #    ", " #    ", " #### "],
    "N": [" #  # ", " ## # ", " #### ", " # ## ", " #  # "],
    "O": [" #### ", " #  # ", " #  # ", " #  # ", " #### "],
    "P": [" ###  ", " #  # ", " ###  ", " #    ", " #    "],
    "R": [" ###  ", " #  # ", " ###  ", " # #  ", " #  # "],
    "T": [" #####", "   #  ", "   #  ", "   #  ", "   #  "],
    "M": [" #   #", " ## ##", " # # #", " #   #", " #   #"],
    "G": [" #### ", " #    ", " # ## ", " #  # ", " #### "],
    "V": [" #  # ", " #  # ", " #  # ", "  ##  ", "  ##  "],
    "U": [" #  # ", " #  # ", " #  # ", " #  # ", " #### "],
    "S": [" #### ", " #    ", " #### ", "    # ", " #### "],
    "B": [" ###  ", " #  # ", " ###  ", " #  # ", " ###  "],
    "Y": [" #  # ", " #  # ", "  ##  ", "  #   ", "  #   "],
    "H": [" #  # ", " #  # ", " #### ", " #  # ", " #  # "],
    " ": ["   ", "   ", "   ", "   ", "   "],
    "!": ["  #  ", "  #  ", "  #  ", "     ", "  #  "],
    "/": ["    #", "   # ", "  #  ", " #   ", "#    "],
    "X": [" #  # ", "  ##  ", "  ##  ", "  ##  ", " #  # "],
}


def _big(text):
    rows = ["", "", "", "", ""]
    for ch in text.upper():
        glyph = _FONT.get(ch, _FONT[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "
    return rows


def banner(lines, big_text, style):
    width = max(60, shutil.get_terminal_size((100, 20)).columns)
    bar = style + " " * width + RESET
    print()
    print(bar)
    print(bar)
    for row in _big(big_text):
        pad = (width - len(row)) // 2
        print(style + " " * pad + row + " " * (width - pad - len(row)) + RESET)
    print(bar)
    for line in lines:
        centered = line.center(width)
        print(style + centered + RESET)
    print(bar)
    print(bar)
    print()


def green(big, *lines):
    banner(list(lines), big, GREEN_BG)


def red(big, *lines):
    banner(list(lines), big, RED_BG)


def yellow(big, *lines):
    banner(list(lines), big, YELLOW_BG)


# ── the verification itself ───────────────────────────────────────────────────

class Rejected(Exception):
    """Raised the moment any integrity check fails — we fail closed."""


def _check(condition, message):
    if not condition:
        raise Rejected(message)


def verify(receipt):
    if Ed25519PublicKey is None:
        raise Rejected("cryptography library unavailable — cannot check signatures")

    pinned = Ed25519PublicKey.from_public_bytes(bytes.fromhex(PINNED_PUBLIC_KEY_HEX))

    entries = receipt.get("entries")
    _check(isinstance(entries, list) and entries, "receipt has no entries")

    audit = []
    prev = "GENESIS"
    for i, e in enumerate(entries):
        tag = f"entry #{e.get('index', i)} (attempt {e.get('attempt', '?')}, {e.get('verdict','?')})"

        # 1. diff binding — the recorded code IS the code that was tested
        recomputed_diff = sha256_hex(e["diff"])
        _check(recomputed_diff == e["diff_sha256"],
               f"{tag}: diff hash mismatch — the diff was altered after sealing")

        # 2. output binding — the recorded test output is the real output
        recomputed_out = sha256_hex(e["test_output"])
        _check(recomputed_out == e["test_output_sha256"],
               f"{tag}: test-output hash mismatch — output was altered after sealing")

        # 3. verdict must follow strictly from the exit code (no agent claims honored)
        expected = "PASS" if e["exit_code"] == 0 else "FAIL"
        _check(e["verdict"] == expected,
               f"{tag}: verdict '{e['verdict']}' contradicts exit code {e['exit_code']}")

        # 4. entry hash — recompute from the signed core
        recomputed_hash = sha256_hex(canonical_bytes(signed_core(e)))
        _check(recomputed_hash == e["entry_hash"],
               f"{tag}: entry hash mismatch — a signed field was tampered")

        # 5. hash chain — this entry links to the previous one
        _check(e["prev_hash"] == prev,
               f"{tag}: broken chain — prev_hash does not match the prior entry")

        # 6. signature — the runner's PINNED key signed this exact entry hash
        try:
            pinned.verify(bytes.fromhex(e["signature"]), bytes.fromhex(e["entry_hash"]))
        except Exception:
            raise Rejected(f"{tag}: SIGNATURE INVALID — not signed by the runner's key")

        audit.append(f"  [OK] {tag}: diff bound, output bound, hash chained, signature valid")
        if e["diff_touches_tests"]:
            audit.append(f"       ⚠ note: this diff modified TEST code "
                         f"(integrity holds; test quality is the reviewer's call)")
        prev = e["entry_hash"]

    return audit, entries[-1]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "receipt.json"

    # Everything below fails CLOSED: any error becomes a red billboard.
    try:
        with open(path) as f:
            receipt = json.load(f)
    except Exception as exc:
        red("VERIFICATION FAILED",
            "Could not read a valid receipt.",
            f"{type(exc).__name__}: {exc}")
        sys.exit(1)

    print(f"{DIM}verifying {path}{RESET}")
    print(f"{DIM}pinned runner key: {PINNED_PUBLIC_KEY_HEX[:24]}...{RESET}")
    crate = receipt.get("crate", "?")
    base = (receipt.get("base_commit") or "?")[:10]
    print(f"{DIM}crate: {crate}   base commit: {base}{RESET}\n")

    try:
        audit, last = verify(receipt)
    except Rejected as exc:
        print(f"{BOLD}{RED_BG} REJECTED {RESET} {exc}\n")
        red("TAMPERING DETECTED",
            "This receipt was altered, forged, or not signed by the runner.",
            "RESULT NOT VERIFIED.")
        sys.exit(1)
    except Exception as exc:
        # Absolutely no stack traces on stage.
        red("VERIFICATION FAILED",
            "The receipt could not be validated.",
            f"{type(exc).__name__}: {exc}")
        sys.exit(1)

    for line in audit:
        print(f"{BOLD}\033[32m{line}{RESET}")

    if last["verdict"] == "PASS":
        green("VERIFIED",
              "Execution integrity confirmed.",
              "The tests genuinely ran and PASSED against this exact diff.",
              "(This proves execution integrity, not correctness.)")
        sys.exit(0)
    else:
        yellow("VERIFIED  FAIL",
               "The receipt is authentic and untampered,",
               "but the recorded test result is a FAILURE (cargo did not exit 0).")
        sys.exit(2)


if __name__ == "__main__":
    main()
