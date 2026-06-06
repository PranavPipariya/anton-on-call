"""
Canonical hashing + hash-chained, runner-signed receipt entries.

CANONICAL FORM (must match verify.py exactly)
---------------------------------------------
The "signed core" of an entry is a JSON object with these keys, serialized with
sort_keys=True and separators=(",", ":"), UTF-8:

    {base_commit, crate, exit_code, index, prev_hash,
     diff_sha256, test_output_sha256, timestamp, verdict, attempt}

  entry_hash = sha256(canonical(core)).hexdigest()
  signature  = Ed25519_sign(private_key, bytes.fromhex(entry_hash))

The full diff and test_output are stored alongside but NOT in the core; the
core binds their *hashes*. To alter the diff you must alter diff_sha256 (caught
when the verifier recomputes it from the embedded diff), which alters entry_hash
(caught), which invalidates the signature (caught). Three independent walls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

GENESIS = "GENESIS"


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(core: dict) -> bytes:
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signed_core(
    *,
    index: int,
    attempt: int,
    timestamp: str,
    crate: str,
    base_commit: str,
    diff_sha256: str,
    test_output_sha256: str,
    exit_code: int,
    verdict: str,
    prev_hash: str,
    diff_touches_tests: bool,
) -> dict:
    """The exact subset of fields that is hashed and signed."""
    return {
        "index": index,
        "attempt": attempt,
        "timestamp": timestamp,
        "crate": crate,
        "base_commit": base_commit,
        "diff_sha256": diff_sha256,
        "test_output_sha256": test_output_sha256,
        "exit_code": exit_code,
        "verdict": verdict,
        "prev_hash": prev_hash,
        "diff_touches_tests": diff_touches_tests,
    }


def compute_entry_hash(core: dict) -> str:
    return sha256_hex(canonical_bytes(core))


@dataclass
class Entry:
    index: int
    attempt: int
    timestamp: str
    crate: str
    base_commit: str
    diff: str
    diff_sha256: str
    test_output: str
    test_output_sha256: str
    exit_code: int
    verdict: str
    diff_touches_tests: bool
    prev_hash: str
    entry_hash: str
    signature: str

    def to_dict(self) -> dict:
        return asdict(self)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
