# Handoff packet template

Emitted when `recovery-watchdog.sh check` trips `REPEAT`, `STALL` or `BLOCKED`.
Fill it in, hand it to the peer model, and record `handoff` in the ledger.

Keep it **short and self-contained**. This shape is copied from the briefing that
actually worked on 2026-07-29, when the owner told Sol in plain language that Claude had
failed to fix something — Sol then made progress within the hour. Long context dumps are
what made lanes re-read a large onboarding pack 23 times; the taker needs the objective,
the failures, and the constraint, not the history.

---

```markdown
# HANDOFF — <unit-id> — <from-model> → <to-model>

You are taking over this unit because <from-model> failed <N> attempts.
Do not repeat the approaches below.

## Objective (unchanged)
<one sentence: the done-condition>

## Proof (IMMUTABLE — you may not change this)
<exact command>
Passing = <exact expected result>

## Ground truth
- State Ledger: <path>
- Base SHA (immutable): <40-hex>
- Declared file set: <exact repo-relative paths, no globs>
- Worktree: <absolute path>   Mode: normal | recovery

## What was tried and why each failed
1. <approach> → <verbatim failing command> → <verbatim tail of the failure>
   Why it failed: <root cause, or "unknown — that is part of your job">
2. <approach> → …

## The blocking constraint
<the guard reject code + metrics, the missing dependency, the auth failure — or
"none identified: the approach was wrong, not the environment">

Block type: MODEL (approach was wrong) | ENVIRONMENT (precondition refused)
If ENVIRONMENT: you may reject this framing. If you can find safe work inside the
constraint, do it. If not, name the single specific thing the owner must free.

## Rules that still bind you
- One owner per unit: <from-model> is frozen off this unit. Under `assured`, it may
  become the cross-model reviewer; under `sprint`, there is no unit reviewer.
- Only the stuck role transferred. Commit/merge authority is unchanged:
  the executor never commits; the orchestrator commits under the active profile's
  check. Complete-diff review and proof rerun are `assured`-only.
- You may change the approach freely. You may not change the proof.
- `dispatch-guard.sh` must ADMIT before you launch anything.
- If you fail <FAIL_N> attempts, this hands back and the owner is asked. Say so plainly
  rather than grinding.

## Preserved state you must not destroy
<patch files / safety refs from Act 1 — list them>
```

---

## Recording it

```bash
WD="${FORGE_SKILLS:-$HOME/.claude/skills}/forge-recovery/recovery-watchdog.sh"   # FORGE_SKILLS: plugin installs
$WD handoff --run "$RUN" --unit R1 --detail "REPEAT: 2 fails; packet /tmp/handoff-R1.md"
```

The ledger swaps owner and peer, and resets the failure streak so the taker gets a full
window — it does **not** reset the handoff count, which is what eventually escalates.
