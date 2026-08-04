---
name: forge-orchestrate
description: Use when an approved plan contains multiple bounded units that need dependency ordering, isolated worktrees, or parallel execution. Use forge-build for one work order and forge when the plan is not yet approved. Do not use for trivial or single-system work where orchestration overhead exceeds the task.
---

# FORGE-ORCHESTRATE — Split a Frozen Spec Across Models

Act 4 standalone: decompose an APPROVED spec into units, run independent ones
concurrently in isolated worktrees, integrate in dependency order. For a single-track
build use `/forge-build`; to plan first use `/forge`.

## Unique to this skill

0. **Route first.** Read [`../forge/SKILL.md`](../forge/SKILL.md), resolve the active
   profile, and use [`../forge/ENGINE.md`](../forge/ENGINE.md) for preflight before any
   launch. The steps below do not override that profile.
1. **Preconditions.** Plan APPROVED; clean base tree. If the repo has a protocol
   (`AGENTS.md` / `.ai/COLLAB_PROTOCOL.md`), follow its branch/worktree naming.
2. **Decompose** into the smallest units that each have a clear done-condition, a
   **proof** (declared `targeted` or `broad`), and a knowable file set. Classify each
   pair independent (disjoint files → may run in parallel) or dependent (shared files or
   ordering → serial). When unsure, serialize.
3. **Worktrees.** One isolated worktree per concurrent unit; **one writing agent per
   tree**. To widen a unit's file set mid-flight, use one **guarded re-admission**
   (`--admission-action expand`) — same lane, no restart, still rejects overlap.
4. **Execute** independent units together within the guard's caps.
5. **Integrate** passing units in dependency order, then run the profile's validation.
   **The driver merges and commits — never the executor.**

Tunables: `SPEC_FILE=PLAN.md` · `MAX_FIX_ROUNDS=2` · `LOG_FILE=PLAN-REVIEW-LOG.md`.
Concurrency is the guard's, not this file's: three lanes total, one HEAVY lease.

## Execution profile

Before product execution, read [`../forge/SKILL.md`](../forge/SKILL.md) and the one
selected file in [`../forge/profiles/`](../forge/profiles/). Those sources alone own
cadence, review, proof, routing, and integration. Do not reconstruct or add ceremony
here. Every lane still passes [`../forge/dispatch-guard.sh`](../forge/dispatch-guard.sh).

## Canonical references

[`../forge/ROUTING-POLICY.md`](../forge/ROUTING-POLICY.md) (routing) ·
[`../forge/profiles/`](../forge/profiles/) (cadence) ·
[`../forge/ENGINE.md`](../forge/ENGINE.md) (engine, templates, admission) ·
[`../forge/references/`](../forge/references/) (detail, on demand)

## What NOT to do

- Don't hand-wave concurrency — the guard decides, per lane.
- Don't restate Act-3/4 cadence here; the profile owns it.
- Don't wait at Gate 2 under `sprint` — apply the overlay/default and record one line.
- Don't call a same-model subagent review cross-model (and under `sprint` there is no
  per-unit review at all).
