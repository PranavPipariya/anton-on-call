"""
Ed25519 key management for the runner.

THE TRUST PROPERTY THIS FILE ENFORCES
-------------------------------------
The private key lives in a directory OUTSIDE the target crate. The fix-agent's
file tools are jailed to the crate (see provenance/fix_loop.py), and the agent
has no shell / write / exec tool, so it has no code path to read this key.

The runner signs every verdict with this private key. The standalone verifier
checks signatures against the PINNED public key baked into verify.py. A green
verdict that was not signed by this exact key fails verification. The agent
therefore cannot manufacture a passing verdict it did not earn.

We cannot *prove* the key was never exfiltrated — but we remove every primitive
the agent could use to exfiltrate it. That is the honest boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Keys live next to the runner, NOT inside the target repo the agent can read.
_KEY_DIR = Path(__file__).resolve().parent / ".keys"
_PRIVATE_KEY_PATH = _KEY_DIR / "runner_ed25519.key"  # 32 raw bytes, hex-encoded


def _ensure_key_dir() -> None:
    _KEY_DIR.mkdir(mode=0o700, exist_ok=True)


def load_or_create_private_key() -> Ed25519PrivateKey:
    """Load the runner's private key, generating it once on first use."""
    _ensure_key_dir()
    if _PRIVATE_KEY_PATH.exists():
        raw = bytes.fromhex(_PRIVATE_KEY_PATH.read_text().strip())
        return Ed25519PrivateKey.from_private_bytes(raw)

    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes_raw()
    # Write 0600 so even other users on the box can't read it.
    fd = os.open(_PRIVATE_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(raw.hex())
    return key


def public_key_hex(private_key: Ed25519PrivateKey | None = None) -> str:
    """Return the runner's public key as hex (this is what verify.py pins)."""
    key = private_key or load_or_create_private_key()
    return key.public_key().public_bytes_raw().hex()


def sign(private_key: Ed25519PrivateKey, message: bytes) -> str:
    """Sign `message` and return a hex signature."""
    return private_key.sign(message).hex()


def verify(public_key_hex_str: str, signature_hex: str, message: bytes) -> bool:
    """Verify a hex signature against a hex public key. Used by tests, not the verifier."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex_str))
        pub.verify(bytes.fromhex(signature_hex), message)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # Convenience: print the public key to paste into verify.py's PINNED constant.
    print(public_key_hex())
