---
name: forge-orchestrate
description: Use when an approved complex implementation plan has multiple dependency-aware units, isolated worktrees, or safe parallel ownership.
---

# Forge Orchestrate

This is standalone Act 4 for an approved complex plan. Codex remains driver,
routing arbiter, verifier, serial integrator, and sole commit/merge/push authority.
The executor never commits, merges, or pushes. Owner and repository policy override
defaults.

Read the [engine](../forge/ENGINE.md), [routing policy](../forge/ROUTING-POLICY.md),
selected [Sprint](../forge/profiles/sprint.md) or
[Assured](../forge/profiles/assured.md) profile, and the shared
[Acts 3–4](../forge/references/acts-3-4.md) contract before launch. Use
[admission](../forge/references/admission.md), the
[typed bridge](../forge/references/bridge-tools.md),
[liveness](../forge/references/liveness.md), and
[usage limits](../forge/references/usage-limits.md) as executable authorities, not
prose substitutes.

## Preconditions and dependency graph

Require a genuine Gate-1 `APPROVED`, the exact approved plan identity, a clean
immutable base, a resolved profile/source, and a positive per-unit
`MAX_FIX_ROUNDS`. Repeat the feasibility and capability check for every proposed
unit and edge: repository, tools, permissions, budgets, proof environment, route,
worktree, and predecessor artifacts must actually exist. An infeasible dependency
keeps its descendants unready; it never authorizes speculative work.

Turn the plan into a directed acyclic graph. Each unit records its acceptance
criteria, predecessors, successor list, exact files (including generated outputs),
immutable baseline, route/model/effort, proof argv/scope/weight, constraints,
budgets, work-log/report paths, report contract, and integration order. Compute
topological order and reject cycles before routing. An edge binds the exact
predecessor report-consumption receipt and
any code, schema, generated artifact, or decision the successor consumes.

Parallel-ready units need disjoint exact file sets. When two units propose a shared
file, add an edge or combine them; never give overlapping writers concurrent lanes.
Integration-owned files belong to a later serial unit. A successor baseline is
frozen only after its predecessors have been verified and integrated, never against
an imagined future state. Freeze every unit with the complete Act-3 work-order and
report contracts. Before admission, require all predecessor integrations and
consumption receipts, then refreeze the successor baseline; an unready unit does not
reach the scheduler.

## Route and Gate 2

Apply owner/project overlays exactly and never substitute a model without owner
approval. Sprint applies the binding route/default and logs one reason; Sprint never
waits at Gate 2. It has no routing debate.

Under Assured, both foundation-model families may make material route arguments and
Codex arbitrates using capability, context, risk, effort, dependencies, and review
independence. For Assured plus complex work, present the complete unit, dependency,
and route table at Gate 2 and wait for owner approval before any execution launch.
Gate 2 does not apply to Sprint or simple Assured work. A material approved-route
change returns to Gate 2.

## Admit isolated lanes

Create one isolated worktree per concurrent unit and enforce one writer per worktree.
Refresh one scheduler snapshot immediately before every launch. The single shared
`lane_reservations` ledger enforces a maximum of three active lanes total across
Codex and Claude, one machine-wide HEAVY lease, disjoint files, exact baselines,
machine headroom, and one active writer. Three is a cap, not a target.

Admission is an atomic admission plus durable reservation; prose is not admission.
Launch nothing until the executable guard returns a bound `ADMIT` from the fresh
scheduler snapshot.

- A Claude-owned unit first runs provider-free `forge_preflight`, then one
  role=`build` `forge_start` with exact cwd, model/effort, budget, idempotency key,
  prompt, and product admission. Persist its independent job ID and revision plus
  attempt/session evidence.
- A Codex-native unit uses
  [`scripts/forge-native-lane.py`](../../scripts/forge-native-lane.py) `reserve`
  before native delegation, with the private guard request and receipt. Use
  `heartbeat` while active and `release` only after subtree quiescence.

Either root may use same-family native subagents inside its admitted unit, but they
do not create extra lanes, writers, worktrees, files, or authority. On `REJECT`,
record the code, correct only the cause, refresh state, and seek a new admission.
Never share a writable worktree, invoke Claude directly, choose a recent session, or
let one root edit another lane.

## Monitor and repair

Track unit, owner, job/attempt or native-task identity, revision, worktree, baseline,
files, proof, reservation, work log, and last objective evidence. Claude lanes use
`forge_status` plus bounded `forge_wait`, then `forge_result`; native lanes use their
native task identity and shared reservation receipt. Measure objective or artifact
progress, not narration or heartbeats.

For STALLED or STOPPED work, preserve evidence and request a manual owner handoff;
automated recovery is disabled in this release, so never race a live writer. A provider
or policy pause is owner-gated preserved state, not a handoff or silent fallback.
Cancellation of a non-recovery job uses only the typed bridge and requires quiescence
evidence.

Each unit has its own `MAX_FIX_ROUNDS`. Repair only the failed unit and affected
descendants. A Claude fix uses `forge_resume` on the same explicit session with the
exact revision, exact fix prompt, fresh idempotency key, typed refreeze, and fresh
`ADMIT`; then repeat all profile-required evidence. A native fix is separately
quiesced, preserved, refrozen, and re-admitted. At fix-round exhaustion, pause the
unit and descendants and ask for manual handoff, re-planning, or cancellation of a
non-recovery job. No model or provider substitution occurs without owner approval.

## Verify and integrate

Validate every terminal execution receipt, admitted and actual paths, baseline,
route, proof/report artifacts, absence of executor Git authority, and predecessor
receipt consumption.

- Sprint accepts the executor's passing smallest relevant proof after path,
  overlap, and safety checks. It has no per-unit cross-model review and no Codex
  proof rerun. Queue immediate serial integration and run one comprehensive
  sprint-end gate after the graph lands.
- Assured requires a genuine other-family review receipt for every unit. A
  Claude-built unit receives native Codex review; a Codex-built unit receives
  read-only Claude review through the typed bridge; same-model review never
  qualifies. Codex reviews the complete diff, resolves findings, and Codex reruns
  the proof before integration and the integrated gate.

Only Codex integrates, one unit at a time, in topological dependency order. Refresh
the target HEAD, verify predecessor integrations, apply additively, record the new
HEAD, then refreeze successors. Concurrent completion never permits concurrent
integration. Codex may commit, merge, or push only after the profile and actual
owner/repository gates pass.

Close only after the graph, routes, Gate-2 receipt when applicable, work orders,
admissions, execution/review/consumption receipts, lease history, serial integration
HEADs, and final gate are recorded; all reservations are released; and no writer is
ambiguous. Label unrun live/provider checks as unrun.
