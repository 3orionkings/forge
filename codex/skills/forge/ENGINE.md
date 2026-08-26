# Forge engine and authority

This file defines who may decide, execute, verify, and integrate. Routing is governed
by [ROUTING-POLICY.md](ROUTING-POLICY.md); cadence is governed by the active
[Sprint](profiles/sprint.md) or [Assured](profiles/assured.md) profile. Owner and
repository instructions override defaults.

## Authority map

Codex is the **initiating driver**, **requirements interviewer**, **plan arbiter**,
**verifier**, **routing arbiter**, and **integrator**. Codex is the sole commit, merge, and push authority, subject to all owner and repository gates. This authority
does not invent an owner sign-off requirement that the owner or repository did not
set.

Claude/Fable 5 at max is the default external plan reviewer and default external executor. Sol 5.6 at ultra is the default Codex-side route. Either foundation model may own routed units and use its own native subagents. Model ownership of a unit does
not transfer driver or integration authority.

The executor never commits, merges, or pushes. Same-model review is never called
cross-model. A review receipt must identify a reviewer from the other foundation-model
family.

## Control boundary

The bridge controls Claude only, through the nine tools documented in
[bridge-tools.md](references/bridge-tools.md). Codex-native lanes use native Codex subagents and never round-trip through the Claude bridge. Never reconstruct a provider
command from prose.

| Activity | Authority | Mechanism |
|---|---|---|
| Requirements grill | Codex | owner dialogue plus repository exploration |
| Plan decision | Codex | `PLAN.md` and `PLAN-REVIEW-LOG.md` |
| External plan challenge | Fable 5 at max | typed read-only bridge review |
| Claude-owned build unit | Claude root, optionally native Claude subagents | admitted typed bridge build |
| Codex-owned build unit | Codex native subagent | native Codex delegation |
| Route/finding decision | Codex | logged reason; Gate 2 only where required |
| Verification/integration | Codex | active-profile contract |
| Commit/merge/push | Codex only | owner and repository policy |

## Four-act state

1. Act 1 locks requirements. Codex asks one question at a time, inspects discoverable
   facts, and writes the plan and review log. No product code.
2. Act 2 obtains a read-only adversarial external verdict. Codex accepts or rejects
   findings with reasons. Gate 1 is exact approval or bounded owner deadlock. No
   product code.
3. Act 3 executes one frozen, admitted unit and returns a receipt for Codex to verify
   and integrate.
4. Act 4 schedules dependency-aware frozen units in isolated worktrees, then Codex
   integrates them serially.

The detailed procedures are in [Acts 1–2](references/acts-1-2.md) and
[Acts 3–4](references/acts-3-4.md).

## Profiles change proof cadence, not safety

Both profiles retain Gate 1, exact baselines and file sets, executable admission, a
three-lane total cap, one machine-wide heavy lease, one writer per worktree, disjoint
parallel file ownership, receipts, and Codex integration authority.

- Sprint trusts the executor's passing smallest relevant proof after Codex checks
  paths, overlap, and safety. It has no per-unit cross-model review and no Codex proof
  rerun. Codex integrates immediately in serial order, then runs the sprint-end gate.
- Assured requires a genuine cross-model unit receipt, Codex complete-diff review and
  proof rerun, and the integrated gate. Complex Assured routing waits at owner Gate 2.

No invented owner commit-approval gate exists. Destructive or otherwise irreversible
actions still require whatever approval the owner, repository, or governing safety
policy actually requires.

## Admission and evidence

For Claude work, a start request is also an admission request. The bridge derives
repository identity and scheduler state, executes `scripts/dispatch-guard.sh`, and
publishes the job reservation atomically only after `ADMIT`. Read
[admission.md](references/admission.md) for the structured fields. Prose is not the
enforcement.

Codex-owned units must honor the same declared-unit invariants through native lane
control. Do not use the Claude bridge to manufacture a Codex lane or claim a bridge
receipt for work it did not execute.

## Provider and liveness boundary

Every live external job carries owner-supplied bounds and explicit model/effort. Use
[usage-limits.md](references/usage-limits.md) for cutoff behavior and
[liveness.md](references/liveness.md) for observation. A provider or policy pause is
preserved state, not failure permission, automatic substitution, or handoff evidence.
Cancellation is requested only through the typed bridge cancellation tool.

## Red flags

- Treating Claude as driver, routing arbiter, integrator, or committer.
- Treating a same-model subagent check as cross-model review.
- Invoking Claude directly, selecting a latest session, or exposing arbitrary shell.
- Starting from a drifting baseline or widening files without fresh admission.
- Applying Assured per-unit ceremony to Sprint.
- Promoting Sprint because a proof is red rather than because wider risk was found.
- Launching a fourth lane or a second heavy lease.
- Substituting a model or provider without owner approval.
