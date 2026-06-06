"""
Anton — Execution-Integrity Provenance.

This package severs the agent's ability to author its own test result.

  - keys.py     : Ed25519 keypair the *runner* holds; the agent has no tool to read it.
  - receipt.py  : canonical hashing + hash-chained, runner-signed receipt entries.
  - runner.py   : the keyed runner — applies a diff to a CLEAN checkout, runs the
                  tests itself, hashes the diff + output itself, and signs the verdict.

The standalone verifier (../verify.py) imports NONE of this. It re-derives every
hash and checks the signature against a PINNED public key. It trusts no one.
"""
