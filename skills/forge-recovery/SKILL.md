---
name: forge-recovery
description: Use when an existing Forge run has actually crashed, stalled, or stranded work—for example stale lanes, dirty abandoned worktrees, unmerged work above the cap, no artifact progress, or product commits blocked behind REVISE. Do not load pre-emptively for a healthy run, a clean build, or plan review.
---

# FORGE-RECOVERY — Jump-Start a Wrecked Run, Then Resume the Plan

`/forge` assumes a clean start. This assumes **wreckage**: dirty trees, stranded
commits, unreceipted claims, a blocked machine, an orchestrator that is busy but not
progressing, and a control plane that permitted it.

**Load only when recovery is actually needed.** If the run is progressing, this skill
is noise — use `/forge`.

Recovery is for **a plan that existed and went wrong**. Healing it and getting back on
track is the job; closing an open review round is part of Act 4 only when one is
genuinely open.

## The acts

Act 0 is not a step — it is armed for the whole run and fires inside any other act.

| Act | Does | Objective metric (what the watchdog measures) |
|---|---|---|
| **0 WATCH** | Outcome-based stuck detection + role handoff | *(armed throughout)* |
| **1 HALT & PRESERVE** | Freeze dispatch; adjudicate live-vs-hung by the artifact clock; patch-preserve every dirty tree; name every detached tip | preservation receipts written |
| **2 RECONSTRUCT** | Rebuild ground truth from artifacts; void every unreceipted claim; SHA-dedupe the commit union | State Ledger sections completed |
| **3 REPAIR** | Fix the control plane that permitted the failure; add the refusal test; append the lesson | guard + watchdog self-tests passing |
| **4 RE-LEGALIZE** | Close any genuinely open review round; write the salvage ledger; get it adversarially approved | critique rounds closed |
| **5 DRAIN** | Land salvage units under the guard, cheapest proof first | units landed + gate state |
| **6 REJOIN** | Hand back to forge at its true act with a self-contained resume prompt | resume prompt written |

Full mechanics, gates and per-act procedure:
**[`references/acts-detail.md`](./references/acts-detail.md)**

## Act 0 — the watchdog, in brief

`./recovery-watchdog.sh` measures **outcomes, not activity**. `sol-liveness.sh` answers
"is this process alive?"; on 2026-07-29 the answer was *yes* for twelve hours while
nothing landed.

```bash
WD="${FORGE_SKILLS:-$HOME/.claude/skills}/forge-recovery/recovery-watchdog.sh"   # FORGE_SKILLS: plugin installs
$WD init     --run "$RUN" --unit R1 --owner <side> --peer <side> --objective "units landed"
$WD attempt  --run "$RUN" --unit R1 --result pass|fail --detail "<verbatim failure>"
$WD progress --run "$RUN" --unit R1 --evidence "<what objectively moved>"
$WD check    --run "$RUN" --unit R1   # 0 OK | 10 handoff | 20 escalate | 30 unknown
```

| Trip | Action |
|---|---|
| `REPEAT` — 2 consecutive failed attempts under one owner | hand the stuck role to an eligible peer |
| `STALL` — no objective progress for 20 min | hand over |
| `BLOCKED` — a precondition refusing for an hour | hand over, block declared |
| `PINGPONG` — every eligible owner has had a real tenure | escalate: one named ask **plus the default action you will take anyway** |

Handoff rules: one owner per unit ever · **only the stuck role transfers** · the taker
may change approach but **the proof command is immutable** · the active owner overlay
still binds (under Codex-only, product execution transfers to another Codex executor).
Template: [`HANDOFF-PACKET.md`](./HANDOFF-PACKET.md).

## Cadence

Recovery honours the active **[execution profile](../forge/profiles/)** exactly as
`/forge` does.

- **`sprint`** — one smallest relevant proof per unit; **no unit review, no complete-diff
  review, no orchestrator proof rerun, no fresh-database rebuild, no full unit gate**.
  Commit check is: changed paths match the admitted set · no overlap/safety breach ·
  executor proof exited 0. A red targeted proof is **fixed and re-run** — it does not by
  itself select `assured`. Overlay/default executor is applied automatically, one line
  recorded, no routing debate and no Gate-2 wait.
- **`assured`** — per-unit cross-model review with receipts, complete-diff review, the
  orchestrator's own proof rerun, the integrated gate, argued routing and Gate 2.

Recovery mode relaxes **only** the clean-tree precondition — never receipts required at
the active profile, roles, caps, leases, machine admission, or the rule that the
executor never commits.

## Safety invariants — binding at every profile

- **Owner instructions win**, always.
- **Preserve before anything.** No `reset`, `clean`, destructive `restore`, branch
  deletion, force-push, or rebase of landed commits without explicit owner approval —
  and never before Act 1's receipts exist.
- **No lane launches without `dispatch-guard.sh` printing `ADMIT:`.** But a refusing
  guard must route to safe work and, past `ENV_MAX`, hand over — never idle.
- **Three lanes total; one heavy lease; disjoint declared file sets; one writer per
  worktree.**
- **The executor never commits or merges.** The orchestrator does.
- **Additive integration only.** No secrets in diffs, logs, commits or reports.
- **Claims are not facts** — no receipt, no credit.

## Reference

| Need | File |
|---|---|
| Per-act mechanics and gates | [`references/acts-detail.md`](./references/acts-detail.md) |
| What actually happened, with numbers | [`references/incident-2026-07-28.md`](./references/incident-2026-07-28.md) · [`../forge/references/incidents.md`](../forge/references/incidents.md) |
| Loadable orchestration rules | [`PLAYBOOK.md`](./PLAYBOOK.md) |
| Handoff template | [`HANDOFF-PACKET.md`](./HANDOFF-PACKET.md) |
| Routing | [`../forge/ROUTING-POLICY.md`](../forge/ROUTING-POLICY.md) |
