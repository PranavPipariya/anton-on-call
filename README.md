# Anton — Verifiable Autonomous On-Call Engineer

> Every autonomous coding agent tells you *"I fixed it, the tests pass."*
> You have no way to check. The agent authors its own result — it can type
> "tests passed" whether they did or not.
>
> **Anton severs that.** Tests are executed by a runner the agent cannot reach,
> the result is bound to the exact diff and **cryptographically signed**, and a
> **standalone verifier** anyone can run re-derives every hash and checks the
> signature. It trusts neither the agent nor us.

Anton is a 24×7 autonomous on-call engineer: it watches a repo's CI, and when a
build breaks it fixes the bug, **proves the fix really passed**, and opens (or
auto-merges) a pull request — with a tamper-evident **execution-integrity
receipt** attached.

---

## The one idea: Execution-Integrity Provenance

Two things people *want* an autonomous agent to prove:

1. **Correctness** — "the bug is truly fixed / the tests are good." **Nobody can
   prove this.**
2. **Execution integrity** — "the tests genuinely *ran* and *passed* against this
   exact diff." **The industry can't even prove this today** — the agent just
   asserts it in prose.

Anton builds **#2**. It's the floor everything else stands on. A green Anton
receipt proves the tests ran and passed against that diff and that the verdict
was signed by a runner the agent couldn't influence. It does **not** prove the
tests are good — and we say so, loudly.

> Tailwind: as agents move into high-risk codebases, *verifiable execution
> integrity becomes table stakes* — e.g. EU AI Act Article 12 (automatic,
> non-retrofittable event logging for high-risk AI, effective Aug 2 2026).

---

## Why the claim is real: the trust boundary

The whole thing lives or dies on one question: **can the agent fake a green?**

The agent (an LLM with tools) produces exactly **one** thing — a unified diff. It
has **no shell, no write tool, no test runner**, **jailed read-only** access
scoped to the target crate (so it can't read the signing key), and **no path** to
the receipt or the verdict.

A separate **runner** — holding an **Ed25519 private key the agent cannot reach** —
does everything that matters itself:

1. builds a **clean `git archive HEAD` checkout** (agent's working tree ignored),
2. applies the agent's diff,
3. runs `cargo test`, captures the **real exit code + output**,
4. **hashes the diff and output itself** (never trusts an agent-supplied hash),
5. derives the verdict **strictly from the exit code** (`0` = PASS, else FAIL),
6. seals a hash-chained receipt entry and **signs it**.

No flag, parameter, or message makes step 5 return PASS without `cargo test`
actually exiting 0 against that exact diff.

| Attack | Why it fails |
|---|---|
| Agent edits its working tree to look fixed | Runner tests a clean `git archive HEAD` |
| Agent supplies its own diff/output hash | Runner hashes both itself |
| Agent writes a green entry / re-signs it | No private key → signature fails vs the **pinned** public key |
| Agent floods text "ALL TESTS PASSING" | Verdict comes only from the exit code; that text never enters the receipt |

**The one honest seam:** the agent's diff could *weaken the tests*, producing a
*truthful* green ("these weakened tests passed"). That's inside our claim —
integrity, not test quality — the diff is in the receipt, and the runner **signs
a `diff_touches_tests` flag** the verifier prints. We prove integrity, not test
quality.

---

## The receipt + the standalone verifier

Each entry binds `{diff, output, exit code, verdict, prev hash}` via SHA-256,
chains to the previous entry, and is **Ed25519-signed**. A real run is a 2-entry
chain:

```
entry 0   HEAD as-is        FAIL (exit 101)   <- proof the bug was real
entry 1   agent's live fix  PASS (exit 0)     <- proof the fix actually passed
          ^ both runner-signed, hash-chained
```

[`verify.py`](verify.py) **imports nothing from Anton**, makes no network calls,
re-derives every hash from the data *embedded in the receipt*, checks the chain,
and verifies signatures against a **pinned public key** baked into the file (it
ignores any key the receipt ships). It **fails closed** — any tampering or
malformed input → giant red `TAMPERING DETECTED` / `VERIFICATION FAILED`, non-zero
exit, never a stack trace. A passing receipt → giant green `VERIFIED`.

```bash
./verify receipt.json            # green VERIFIED (exit 0)
./verify receipt_tampered.json   # red TAMPERING DETECTED (exit 1)
./verify receipt_forged.json     # red TAMPERING DETECTED (exit 1)
```

The verifier never re-runs cargo, so the result is **deterministic and offline** —
a judge runs it and trusts only the math.

---

## 24×7 autonomy + two approval modes

- **Autonomy:** a watcher polls `cargo test` on HEAD. A real breaking commit →
  the pipeline fires itself. No human types a command.
- **Human-in-the-loop (default):** Anton posts a Slack briefing with **Approve /
  Request Changes** buttons (Slack **Socket Mode** — no public URL / no ngrok). A
  human clicks Approve → a real GitHub PR opens with the fix + receipt.
- **Full-autonomous:** `AUTO_APPROVE=1 AUTO_MERGE=1` → Anton approves *itself* and
  merges the PR. Zero humans.

The verdict shown in Slack and attached to the PR is the **runner's signed
verdict**, never the agent's claim.

---

## Architecture

```
   breaking commit   WATCHER  cargo test on HEAD -> FAIL        no human
   lands ──────────► (provenance/watcher.py)                    command
                                 │
                                 ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  FIX-LOOP (jailed agent)        │   RUNNER (holds Ed25519 key)      │
   │  - read-only, crate-jailed      │   - clean `git archive HEAD`      │
   │  - NO shell / write / cargo     │   - applies diff, runs cargo test │
   │  - emits a unified diff ────────────►  hashes diff + output         │
   │     ◄── runner's failure log ───│   - verdict from exit code        │
   │         (agent can self-revise) │   - SIGNS hash-chained receipt    │
   └────────────────────────────────────────────────┬─────────────────┘
                                                      ▼
              Slack briefing (Socket Mode, buttons) ── Approve ──►
              real GitHub PR  (fix + receipt.json)
                                                      ▼
              STANDALONE VERIFIER (verify.py, no Anton imports)
              re-derive hashes • check chain • check signature
              ─────────►  VERIFIED ✓   or   TAMPERING DETECTED ✗
```

---

## Quickstart

```bash
git clone https://github.com/PranavPipariya/anton-on-call
cd anton-on-call

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# the target crate Anton watches/fixes (separate repo)
git clone https://github.com/PranavPipariya/anton-histogram-demo target_repo

# credentials
cp .env.example .env   # OPENROUTER_API_KEY, SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                       # SLACK_CHANNEL, GITHUB_TOKEN, GITHUB_REPO

# (a) offline core demo: autonomy + live fix + receipts, then verify
./.venv/bin/python -m provenance.demo
./verify receipt.json           # green; then receipt_tampered.json -> red

# (b) live Slack demo (human-in-the-loop): briefing + Approve button + real PR
./.venv/bin/python -m provenance.socket_app

# (c) full autonomous: Anton approves + merges itself
AUTO_APPROVE=1 AUTO_MERGE=1 ./.venv/bin/python -m provenance.socket_app
```

LLM is OpenAI-compatible via OpenRouter; default model `openai/gpt-4o-mini`.
Override with `MODEL=` in `.env`.

---

## Layout

```
verify.py                 STANDALONE verifier (imports nothing from Anton)
verify                    launcher: ./verify receipt.json
provenance/
  runner.py               THE TRUST BOUNDARY: clean checkout, cargo, sign
  keys.py                 Ed25519 keypair (private key is gitignored)
  receipt.py              canonical hashing + hash chain
  fix_loop.py             jailed read-only agent; emits a diff only
  pipeline.py             run -> Slack briefing -> approve -> PR (+merge)
  watcher.py              autonomous local-CI watcher
  rig.py                  generates tampered/forged receipts (lie-detection)
  socket_app.py           live Slack (Socket Mode) + two approval modes
  demo.py                 offline end-to-end orchestrator
integrations/             slack_bot.py, github_client.py
agents/ agent/ tools/     the underlying Anton agent engine
```

## What this proves, and what it doesn't

- ✅ The tests genuinely ran and passed (or failed) against this exact diff.
- ✅ The verdict was signed by a runner the agent had no way to reach or forge.
- ✅ Any tampering with the diff, output, or verdict is detected by anyone.
- ❌ NOT that the tests are good, or the bug is truly fixed. Nobody can prove
  that. We built the floor — verifiable execution integrity — that every higher
  claim must stand on.
